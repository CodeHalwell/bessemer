//! Bounded HTTP/1 transport with explicit connection and slow-header limits.

use axum::{
    Router,
    body::Body,
    extract::ConnectInfo,
    http::{Request, StatusCode, Version, header, uri::Authority},
    response::Response,
};
use hyper::{server::conn::http1, service::service_fn};
use hyper_util::rt::{TokioIo, TokioTimer};
use std::{
    io,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};
use tokio::{
    net::TcpListener,
    sync::{Notify, Semaphore, watch},
    task::JoinSet,
};
use tower::ServiceExt;

#[derive(Clone)]
pub struct Limits {
    pub max_connections: usize,
    pub header_timeout: Duration,
    pub max_headers: usize,
    pub max_header_size: usize,
    pub max_request_target_size: usize,
    pub graceful_timeout: Duration,
    pub max_requests: u64,
}

pub(crate) fn normalize_host(value: &str) -> Option<String> {
    if value.is_empty() || value.contains(['@', '/', '#', '?', ',', ' ', '\t', '\\']) {
        return None;
    }
    let authority = value.parse::<Authority>().ok()?;
    let host = authority.host();
    if host.is_empty() {
        return None;
    }
    let normalized = if let Some(inner) = host
        .strip_prefix('[')
        .and_then(|value| value.strip_suffix(']'))
    {
        inner.parse::<std::net::Ipv6Addr>().ok()?.to_string()
    } else {
        let host = host.strip_suffix('.').unwrap_or(host);
        if host.is_empty() || host.len() > 253 {
            return None;
        }
        if host.split('.').any(|label| {
            label.is_empty()
                || label.len() > 63
                || !label
                    .bytes()
                    .all(|value| value.is_ascii_alphanumeric() || value == b'-')
                || label.starts_with('-')
                || label.ends_with('-')
        }) {
            return None;
        }
        host.to_ascii_lowercase()
    };
    let suffix = &value[host.len()..];
    if !suffix.is_empty() {
        let port = suffix.strip_prefix(':')?;
        if port.is_empty()
            || !port.bytes().all(|value| value.is_ascii_digit())
            || port.parse::<u16>().is_err()
        {
            return None;
        }
    }
    Some(normalized)
}

fn protocol_rejection(request: &Request<Body>, target_limit: usize) -> Option<Response> {
    let reject = |status, detail| {
        let mut response = crate::error(status, detail);
        // A rejected request may still carry unread bytes; prevent downstream
        // interpretation of those bytes as another request on this connection.
        response
            .headers_mut()
            .insert(header::CONNECTION, "close".parse().unwrap());
        response
    };
    if request.uri().to_string().len() > target_limit {
        return Some(reject(
            StatusCode::URI_TOO_LONG,
            "Request target exceeds the configured limit",
        ));
    }
    if request.uri().scheme().is_some() || request.uri().authority().is_some() {
        return Some(reject(
            StatusCode::BAD_REQUEST,
            "This server requires origin-form request targets",
        ));
    }
    let hosts: Vec<_> = request.headers().get_all(header::HOST).iter().collect();
    if hosts.len() > 1 || (request.version() == Version::HTTP_11 && hosts.len() != 1) {
        return Some(reject(
            StatusCode::BAD_REQUEST,
            "HTTP/1.1 requires exactly one Host header",
        ));
    }
    if let Some(value) = hosts.first() {
        let valid = value.to_str().ok().and_then(normalize_host);
        if valid.is_none() {
            return Some(reject(StatusCode::BAD_REQUEST, "Invalid Host header"));
        }
    }
    if request.headers().contains_key(header::TRANSFER_ENCODING)
        && request.headers().contains_key(header::CONTENT_LENGTH)
    {
        return Some(reject(StatusCode::BAD_REQUEST, "Ambiguous request framing"));
    }
    let transfers: Vec<_> = request
        .headers()
        .get_all(header::TRANSFER_ENCODING)
        .iter()
        .collect();
    if transfers.len() > 1
        || transfers.first().is_some_and(|value| {
            !value
                .to_str()
                .is_ok_and(|value| value.trim().eq_ignore_ascii_case("chunked"))
        })
    {
        return Some(reject(
            StatusCode::BAD_REQUEST,
            "Only a single chunked transfer coding is supported",
        ));
    }
    let content_types: Vec<_> = request
        .headers()
        .get_all(header::CONTENT_TYPE)
        .iter()
        .collect();
    if content_types.len() > 1
        || content_types.first().is_some_and(|value| {
            !value
                .to_str()
                .is_ok_and(|value| value.parse::<mime::Mime>().is_ok())
        })
    {
        return Some(reject(
            StatusCode::BAD_REQUEST,
            "Content-Type must be a single valid media type",
        ));
    }
    None
}

pub async fn run(
    listener: TcpListener,
    app: Router,
    limits: Limits,
    ready_fd: Option<i32>,
) -> io::Result<()> {
    let permits = Arc::new(Semaphore::new(limits.max_connections));
    let completed = Arc::new(AtomicU64::new(0));
    let recycle = Arc::new(Notify::new());
    let (stop_sender, stop_receiver) = watch::channel(false);
    let mut connections = JoinSet::new();
    // Register signals before announcing readiness to the supervisor.
    #[cfg(unix)]
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let interrupt = tokio::signal::ctrl_c();
    tokio::pin!(interrupt);
    if let Some(fd) = ready_fd {
        bessemer_ffi::signal_ready(fd)?;
    }
    loop {
        let accepted = tokio::select! {
            _ = &mut interrupt => break,
            _ = recycle.notified() => break,
            _ = async {
                #[cfg(unix)] { terminate.recv().await; }
                #[cfg(not(unix))] { std::future::pending::<()>().await; }
            } => break,
            _ = connections.join_next(), if !connections.is_empty() => continue,
            result = listener.accept() => result,
        };
        let (stream, peer) = match accepted {
            Ok(value) => value,
            Err(error) => {
                if matches!(
                    error.kind(),
                    io::ErrorKind::ConnectionAborted | io::ErrorKind::Interrupted
                ) {
                    continue;
                }
                return Err(error);
            }
        };
        let permit = match permits.clone().try_acquire_owned() {
            Ok(permit) => permit,
            Err(_) => {
                // Best-effort, nonblocking rejection. Never spawn another task when the
                // connection bound is exhausted, or slow clients could bypass the bound.
                let _ = stream.try_write(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\nRetry-After: 1\r\n\r\n");
                continue;
            }
        };
        stream.set_nodelay(true)?;
        let app = app.clone();
        let completed = completed.clone();
        let recycle = recycle.clone();
        let limits = limits.clone();
        let mut stopped = stop_receiver.clone();
        connections.spawn(async move {
            let _permit = permit;
            let service = service_fn(move |request: Request<hyper::body::Incoming>| {
                let app = app.clone();
                let completed = completed.clone();
                let recycle = recycle.clone();
                let target_limit = limits.max_request_target_size;
                let max_requests = limits.max_requests;
                async move {
                    let mut request = request.map(Body::new);
                    request.extensions_mut().insert(ConnectInfo(peer));
                    let response =
                        if let Some(response) = protocol_rejection(&request, target_limit) {
                            response
                        } else {
                            app.oneshot(request)
                                .await
                                .expect("Axum Router is infallible")
                        };
                    let count = completed.fetch_add(1, Ordering::Relaxed) + 1;
                    if max_requests > 0 && count >= max_requests {
                        recycle.notify_one();
                    }
                    Ok::<_, std::convert::Infallible>(response)
                }
            });
            let mut builder = http1::Builder::new();
            builder
                .timer(TokioTimer::new())
                .header_read_timeout(limits.header_timeout)
                .max_headers(limits.max_headers)
                .max_buf_size(limits.max_header_size)
                .half_close(false);
            let connection = builder.serve_connection(TokioIo::new(stream), service);
            tokio::pin!(connection);
            tokio::select! {
                result = &mut connection => { let _ = result; }
                _ = stopped.changed() => {
                    connection.as_mut().graceful_shutdown();
                    let _ = connection.await;
                }
            }
        });
    }
    drop(listener);
    stop_sender.send_replace(true);
    let drain = async { while connections.join_next().await.is_some() {} };
    if tokio::time::timeout(limits.graceful_timeout, drain)
        .await
        .is_err()
    {
        eprintln!("Bessemer native drain deadline exceeded; cancelling remaining connections");
        connections.abort_all();
        while connections.join_next().await.is_some() {}
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn origin_host_and_framing_are_validated() {
        let request = Request::builder().uri("/").body(Body::empty()).unwrap();
        assert_eq!(
            protocol_rejection(&request, 8192).unwrap().status(),
            StatusCode::BAD_REQUEST
        );
        let request = Request::builder()
            .uri("/")
            .header("host", "localhost")
            .body(Body::empty())
            .unwrap();
        assert!(protocol_rejection(&request, 8192).is_none());
        let request = Request::builder()
            .uri("/")
            .header("host", "localhost")
            .header("host", "evil.test")
            .body(Body::empty())
            .unwrap();
        assert_eq!(
            protocol_rejection(&request, 8192).unwrap().status(),
            StatusCode::BAD_REQUEST
        );
        let request = Request::builder()
            .uri("http://evil.test/")
            .header("host", "localhost")
            .body(Body::empty())
            .unwrap();
        assert!(protocol_rejection(&request, 8192).is_some());
        let request = Request::builder()
            .uri("/too-long")
            .header("host", "localhost")
            .body(Body::empty())
            .unwrap();
        assert_eq!(
            protocol_rejection(&request, 2).unwrap().status(),
            StatusCode::URI_TOO_LONG
        );
    }
}
