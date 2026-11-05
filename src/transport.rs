//! Bounded HTTP/1 and HTTP/2 transport with explicit connection and
//! slow-header limits.

#[cfg(test)]
use axum::Router;
use axum::{
    body::Body,
    extract::ConnectInfo,
    http::{HeaderValue, Request, StatusCode, Uri, Version, header, uri::Authority},
    response::Response,
};
use bytes::BytesMut;
use hyper::{
    server::conn::{http1, http2},
    service::service_fn,
};
use hyper_util::rt::{TokioExecutor, TokioIo, TokioTimer};
use std::{
    io,
    sync::{
        Arc,
        atomic::{AtomicU64, AtomicUsize, Ordering},
    },
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite},
    net::TcpListener,
    sync::{Notify, Semaphore, watch},
    task::JoinSet,
};
#[cfg(test)]
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
    /// Serve HTTP/2: negotiated by ALPN over TLS, or by prior knowledge
    /// (the client's connection preface) on plaintext connections.
    pub http2: bool,
}

/// The client connection preface that opens every HTTP/2 connection.
const HTTP2_PREFACE: &[u8] = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n";
/// Concurrent streams per HTTP/2 connection. Admission into the application
/// is still bounded by the dispatcher's in-flight limits.
const HTTP2_MAX_STREAMS: u32 = 100;
/// Streams a client may reset before the server reads them (rapid-reset
/// protection, CVE-2023-44487), and streams the server may reset for client
/// protocol errors before it closes the connection.
const HTTP2_MAX_PENDING_RESETS: usize = 20;
const HTTP2_MAX_ERROR_RESETS: usize = 1024;

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

/// Marks a request that arrived over a TLS connection this server terminated.
#[derive(Clone, Copy, Debug)]
pub(crate) struct Secure;

/// What answers requests once the transport's checks pass.
#[derive(Clone)]
pub(crate) enum Application {
    Native(Arc<crate::router::Dispatcher>),
    /// A plain axum router, for transport tests.
    #[cfg(test)]
    Router(Router),
}

/// The application behind the transport's own protocol checks.
#[derive(Clone)]
pub(crate) struct ProtocolChecked {
    pub(crate) app: Application,
    pub(crate) target_limit: usize,
    pub(crate) secure: bool,
}

impl ProtocolChecked {
    pub(crate) async fn call(&self, mut request: Request<Body>) -> Response {
        if let Some(response) = protocol_rejection(&request, self.target_limit) {
            return response;
        }
        if self.secure {
            request.extensions_mut().insert(Secure);
        }
        match &self.app {
            Application::Native(dispatcher) => dispatcher.call(request).await,
            #[cfg(test)]
            Application::Router(router) => router
                .clone()
                .oneshot(request)
                .await
                .expect("Axum Router is infallible"),
        }
    }
}

/// Serve a connection with hyper. Used for requests that upgrade the protocol,
/// with the bytes already read replayed first.
async fn serve_hyper<S>(
    io: crate::http1::Rewind<S>,
    peer: std::net::SocketAddr,
    checked: ProtocolChecked,
    limits: Limits,
    completed: Arc<AtomicU64>,
    recycle: Arc<Notify>,
    context: crate::websocket::ConnectionContext,
) where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send + 'static,
{
    let mut stopped = context.stop.clone();
    let service = service_fn(move |request: Request<hyper::body::Incoming>| {
        let checked = checked.clone();
        let context = context.clone();
        let completed = completed.clone();
        let recycle = recycle.clone();
        let max_requests = limits.max_requests;
        async move {
            let mut request = request.map(Body::new);
            request.extensions_mut().insert(ConnectInfo(peer));
            request.extensions_mut().insert(context);
            let response = checked.call(request).await;
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
    let connection = builder
        .serve_connection(TokioIo::new(io), service)
        .with_upgrades();
    tokio::pin!(connection);
    tokio::select! {
        result = &mut connection => { let _ = result; }
        _ = stopped.changed() => {
            connection.as_mut().graceful_shutdown();
            let _ = connection.await;
        }
    }
}

/// What ends the accept loop: process signals for a served application, or
/// a shutdown socket (data or close) for an embedded one.
pub enum Stop {
    Signals,
    #[cfg(unix)]
    Socket(std::os::unix::net::UnixStream),
    #[cfg(not(unix))]
    Socket(std::net::TcpStream),
}

type StopFuture = std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send>>;

/// Registers the stop source now, so a signal that arrives before the accept
/// loop first polls is not lost.
fn stop_requested(stop: Stop) -> io::Result<StopFuture> {
    match stop {
        Stop::Signals => {
            #[cfg(unix)]
            let mut terminate =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
            Ok(Box::pin(async move {
                #[cfg(unix)]
                tokio::select! {
                    _ = tokio::signal::ctrl_c() => {}
                    _ = terminate.recv() => {}
                }
                #[cfg(not(unix))]
                let _ = tokio::signal::ctrl_c().await;
            }))
        }
        #[cfg(unix)]
        Stop::Socket(socket) => {
            let socket = tokio::net::UnixStream::from_std(socket)?;
            Ok(Box::pin(async move {
                let mut byte = [0u8; 1];
                loop {
                    if socket.readable().await.is_err() {
                        return;
                    }
                    match socket.try_read(&mut byte) {
                        Err(error) if error.kind() == io::ErrorKind::WouldBlock => continue,
                        // Data, end of stream or an error all mean stop.
                        _ => return,
                    }
                }
            }))
        }
        #[cfg(not(unix))]
        Stop::Socket(_) => Ok(Box::pin(std::future::pending())),
    }
}

/// Read just enough of a plaintext connection to tell an HTTP/2 preface from
/// an HTTP/1 request line. The bytes read are replayed to whichever protocol
/// serves the connection. `None` means it closed, stalled past the header
/// deadline or the server is stopping.
async fn sniff_http2<S: AsyncRead + Unpin>(
    mut stream: S,
    timeout: Duration,
    stop: &mut watch::Receiver<bool>,
) -> Option<(bool, crate::http1::Rewind<S>)> {
    let mut buffer = BytesMut::with_capacity(1024);
    let deadline = tokio::time::sleep(timeout);
    tokio::pin!(deadline);
    loop {
        let common = buffer.len().min(HTTP2_PREFACE.len());
        if buffer[..common] != HTTP2_PREFACE[..common] || common == HTTP2_PREFACE.len() {
            let http2 = common == HTTP2_PREFACE.len() && buffer[..common] == *HTTP2_PREFACE;
            return Some((http2, crate::http1::Rewind::new(buffer.freeze(), stream)));
        }
        tokio::select! {
            read = stream.read_buf(&mut buffer) => match read {
                Ok(0) | Err(_) => return None,
                Ok(_) => {}
            },
            _ = &mut deadline => return None,
            _ = stop.wait_for(|stopped| *stopped) => return None,
        }
    }
}

/// HTTP/2 carries the target as `:scheme`, `:authority` and `:path`. Reduce it
/// to the origin form and `Host` header that HTTP/1 requests have, so the
/// same checks and routing apply to both.
fn http2_origin_form(request: &mut Request<Body>, max_headers: usize) -> Option<Response> {
    let reject = |status, detail| Some(crate::error(status, detail));
    if request.headers().len() > max_headers {
        return reject(
            StatusCode::REQUEST_HEADER_FIELDS_TOO_LARGE,
            "Too many request headers",
        );
    }
    let Some(path) = request.uri().path_and_query().cloned() else {
        return reject(StatusCode::BAD_REQUEST, "HTTP/2 requests need a path");
    };
    if let Some(authority) = request.uri().authority().cloned() {
        match request.headers().get(header::HOST) {
            Some(host) if host.as_bytes() != authority.as_str().as_bytes() => {
                return reject(
                    StatusCode::BAD_REQUEST,
                    "Host header does not match :authority",
                );
            }
            Some(_) => {}
            None => {
                let Ok(value) = HeaderValue::from_str(authority.as_str()) else {
                    return reject(StatusCode::BAD_REQUEST, "Invalid Host header");
                };
                request.headers_mut().insert(header::HOST, value);
            }
        }
    }
    *request.uri_mut() = Uri::from(path);
    None
}

/// Counts a connection's open streams, so an idle HTTP/2 connection is closed
/// after the header deadline instead of holding its slot.
#[derive(Default)]
struct Http2Activity {
    active: AtomicUsize,
    started: AtomicU64,
}

struct ActiveStream(Arc<Http2Activity>);

impl ActiveStream {
    fn new(activity: Arc<Http2Activity>) -> Self {
        activity.started.fetch_add(1, Ordering::Relaxed);
        activity.active.fetch_add(1, Ordering::Relaxed);
        Self(activity)
    }
}

impl Drop for ActiveStream {
    fn drop(&mut self) {
        self.0.active.fetch_sub(1, Ordering::Relaxed);
    }
}

/// Serve one HTTP/2 connection with hyper, under the transport's limits.
async fn serve_http2<S, F>(
    io: S,
    peer: std::net::SocketAddr,
    checked: ProtocolChecked,
    limits: Limits,
    on_response: F,
    mut stopped: watch::Receiver<bool>,
) where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    F: Fn() + Send + Sync + 'static,
{
    let activity = Arc::new(Http2Activity::default());
    let on_response = Arc::new(on_response);
    let max_headers = limits.max_headers;
    let service = {
        let activity = activity.clone();
        service_fn(move |request: Request<hyper::body::Incoming>| {
            let checked = checked.clone();
            let on_response = on_response.clone();
            // Dropped with the stream, including when the client resets it.
            let stream = ActiveStream::new(activity.clone());
            async move {
                let _stream = stream;
                let mut request = request.map(Body::new);
                let response = match http2_origin_form(&mut request, max_headers) {
                    Some(rejection) => rejection,
                    None => {
                        request.extensions_mut().insert(ConnectInfo(peer));
                        checked.call(request).await
                    }
                };
                on_response();
                Ok::<_, std::convert::Infallible>(response)
            }
        })
    };
    let mut builder = http2::Builder::new(TokioExecutor::new());
    builder
        .timer(TokioTimer::new())
        .max_concurrent_streams(HTTP2_MAX_STREAMS)
        .max_header_list_size(u32::try_from(limits.max_header_size).unwrap_or(u32::MAX))
        .max_pending_accept_reset_streams(HTTP2_MAX_PENDING_RESETS)
        .max_local_error_reset_streams(HTTP2_MAX_ERROR_RESETS);
    let connection = builder.serve_connection(TokioIo::new(io), service);
    tokio::pin!(connection);
    let mut idle = tokio::time::interval(limits.header_timeout);
    idle.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    idle.tick().await;
    let mut seen = activity.started.load(Ordering::Relaxed);
    loop {
        tokio::select! {
            _ = &mut connection => return,
            _ = stopped.wait_for(|stopped| *stopped) => break,
            _ = idle.tick() => {
                let started = activity.started.load(Ordering::Relaxed);
                if started == seen && activity.active.load(Ordering::Relaxed) == 0 {
                    break;
                }
                seen = started;
            }
        }
    }
    // GOAWAY: open streams finish, new ones are refused.
    connection.as_mut().graceful_shutdown();
    let _ = connection.await;
}

/// Serve one HTTP/1 connection, handing protocol upgrades to hyper.
#[allow(clippy::too_many_arguments)]
async fn serve_http1<S, F>(
    stream: S,
    peer: std::net::SocketAddr,
    checked: ProtocolChecked,
    limits: Limits,
    on_response: F,
    stopped: watch::Receiver<bool>,
    completed: Arc<AtomicU64>,
    recycle: Arc<Notify>,
    context: crate::websocket::ConnectionContext,
) where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    F: Fn(),
{
    let config = crate::http1::Config {
        header_timeout: limits.header_timeout,
        max_headers: limits.max_headers,
        max_header_size: limits.max_header_size,
    };
    crate::http1::serve(
        stream,
        peer,
        checked,
        config,
        stopped,
        on_response,
        async move |io, app| {
            serve_hyper(io, peer, app, limits, completed, recycle, context).await;
        },
    )
    .await;
}

pub async fn run(
    listener: TcpListener,
    app: Application,
    limits: Limits,
    ready_fd: Option<i32>,
    stop: Stop,
    tls: Option<Arc<rustls::ServerConfig>>,
) -> io::Result<()> {
    let acceptor = tls.map(tokio_rustls::TlsAcceptor::from);
    let permits = Arc::new(Semaphore::new(limits.max_connections));
    let completed = Arc::new(AtomicU64::new(0));
    let recycle = Arc::new(Notify::new());
    let (stop_sender, stop_receiver) = watch::channel(false);
    let mut connections = JoinSet::new();
    // Register signals before announcing readiness to the supervisor.
    let mut stopped_by = stop_requested(stop)?;
    if let Some(fd) = ready_fd {
        bessemer_ffi::signal_ready(fd)?;
    }
    loop {
        let accepted = tokio::select! {
            _ = &mut stopped_by => break,
            _ = recycle.notified() => break,
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
                // A TLS client could not read a plaintext answer, so it is only closed.
                if acceptor.is_none() {
                    let _ = stream.try_write(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\nRetry-After: 1\r\n\r\n");
                }
                continue;
            }
        };
        stream.set_nodelay(true)?;
        let app = app.clone();
        let completed = completed.clone();
        let recycle = recycle.clone();
        let limits = limits.clone();
        let stopped = stop_receiver.clone();
        let acceptor = acceptor.clone();
        // Upgraded WebSocket sessions keep a clone of this connection slot.
        let context = crate::websocket::ConnectionContext {
            _slot: Arc::new(permit),
            stop: stop_receiver.clone(),
        };
        connections.spawn(async move {
            let count = {
                let completed = completed.clone();
                let recycle = recycle.clone();
                let max_requests = limits.max_requests;
                move || {
                    let count = completed.fetch_add(1, Ordering::Relaxed) + 1;
                    if max_requests > 0 && count >= max_requests {
                        recycle.notify_one();
                    }
                }
            };
            let protected = ProtocolChecked {
                app,
                target_limit: limits.max_request_target_size,
                secure: acceptor.is_some(),
            };
            let mut stop = stopped.clone();
            let Some(acceptor) = acceptor else {
                if !limits.http2 {
                    serve_http1(
                        stream, peer, protected, limits, count, stopped, completed, recycle,
                        context,
                    )
                    .await;
                    return;
                }
                match sniff_http2(stream, limits.header_timeout, &mut stop).await {
                    Some((true, stream)) => {
                        let _slot = context;
                        serve_http2(stream, peer, protected, limits, count, stopped).await;
                    }
                    Some((false, stream)) => {
                        serve_http1(
                            stream, peer, protected, limits, count, stopped, completed,
                            recycle, context,
                        )
                        .await;
                    }
                    None => {}
                }
                return;
            };
            // The handshake shares the header deadline, so a client that
            // stalls it cannot hold the connection slot, and runs in this
            // connection's task so it never delays the accept loop.
            let handshake = tokio::select! {
                result = tokio::time::timeout(limits.header_timeout, acceptor.accept(stream)) => result,
                _ = stop.wait_for(|stopped| *stopped) => return,
            };
            let Ok(Ok(stream)) = handshake else {
                return;
            };
            if stream.get_ref().1.alpn_protocol() == Some(b"h2") {
                let _slot = context;
                serve_http2(stream, peer, protected, limits, count, stopped).await;
                return;
            }
            serve_http1(
                stream, peer, protected, limits, count, stopped, completed, recycle, context,
            )
            .await;
        });
    }
    if let Some(fd) = ready_fd {
        bessemer_ffi::signal_draining(fd)?;
    }
    drop(listener);
    stop_sender.send_replace(true);
    let drain = async {
        while connections.join_next().await.is_some() {}
        // Upgraded WebSocket sessions outlive their HTTP connection task but
        // hold its slot until they end, so every slot returning means drained.
        let all = u32::try_from(limits.max_connections).unwrap_or(u32::MAX);
        let _ = permits.acquire_many(all).await;
    };
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
    use axum::routing::get;
    use http_body_util::BodyExt;
    use tokio::io::AsyncWriteExt;

    async fn start(
        http2: bool,
    ) -> (
        std::net::SocketAddr,
        std::os::unix::net::UnixStream,
        tokio::task::JoinHandle<io::Result<()>>,
    ) {
        let app = Router::new().route(
            "/echo",
            get(|request: Request<Body>| async move {
                let host = request
                    .headers()
                    .get(header::HOST)
                    .map(|value| value.to_str().unwrap().to_owned())
                    .unwrap_or_default();
                format!("{:?} {} {}", request.version(), host, request.uri())
            }),
        );
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let (stop, server_end) = std::os::unix::net::UnixStream::pair().unwrap();
        server_end.set_nonblocking(true).unwrap();
        let limits = Limits {
            max_connections: 8,
            header_timeout: Duration::from_millis(300),
            max_headers: 8,
            max_header_size: 8192,
            max_request_target_size: 8192,
            graceful_timeout: Duration::from_secs(5),
            max_requests: 0,
            http2,
        };
        let server = tokio::spawn(run(
            listener,
            Application::Router(app),
            limits,
            None,
            Stop::Socket(server_end),
            None,
        ));
        (address, stop, server)
    }

    async fn http2_client(
        address: std::net::SocketAddr,
    ) -> (
        hyper::client::conn::http2::SendRequest<Body>,
        tokio::task::JoinHandle<hyper::Result<()>>,
    ) {
        let stream = tokio::net::TcpStream::connect(address).await.unwrap();
        let (sender, connection) =
            hyper::client::conn::http2::handshake(TokioExecutor::new(), TokioIo::new(stream))
                .await
                .unwrap();
        (sender, tokio::spawn(connection))
    }

    async fn send(
        sender: &mut hyper::client::conn::http2::SendRequest<Body>,
        request: Request<Body>,
    ) -> (StatusCode, String) {
        let response = sender.send_request(request).await.unwrap();
        let status = response.status();
        let body = response.into_body().collect().await.unwrap().to_bytes();
        (status, String::from_utf8(body.to_vec()).unwrap())
    }

    #[tokio::test]
    async fn plaintext_http2_by_prior_knowledge_shares_the_port() {
        let (address, stop, server) = start(true).await;
        let (mut sender, connection) = http2_client(address).await;
        let request = Request::get("http://example.test:8000/echo?x=1")
            .body(Body::empty())
            .unwrap();
        assert_eq!(
            send(&mut sender, request).await,
            (
                StatusCode::OK,
                "HTTP/2.0 example.test:8000 /echo?x=1".to_owned()
            )
        );
        // An explicit Host must agree with :authority.
        let request = Request::get("http://example.test/echo")
            .header(header::HOST, "other.test")
            .body(Body::empty())
            .unwrap();
        assert_eq!(send(&mut sender, request).await.0, StatusCode::BAD_REQUEST);
        // Header count is bounded as on HTTP/1.
        let mut request = Request::get("http://example.test/echo");
        for index in 0..9 {
            request = request.header(format!("x-{index}"), "1");
        }
        let request = request.body(Body::empty()).unwrap();
        assert_eq!(
            send(&mut sender, request).await.0,
            StatusCode::REQUEST_HEADER_FIELDS_TOO_LARGE
        );
        // An idle connection is closed after the header deadline.
        let closed = tokio::time::timeout(Duration::from_secs(3), connection).await;
        assert!(closed.is_ok(), "idle HTTP/2 connection was kept open");

        // HTTP/1 still works on the same port, including methods that start
        // like the preface.
        let mut plain = tokio::net::TcpStream::connect(address).await.unwrap();
        plain
            .write_all(b"PUT /echo HTTP/1.1\r\nHost: h1.test\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            .await
            .unwrap();
        let mut response = Vec::new();
        plain.read_to_end(&mut response).await.unwrap();
        assert!(
            response.starts_with(b"HTTP/1.1 405"),
            "{}",
            String::from_utf8_lossy(&response)
        );
        let mut plain = tokio::net::TcpStream::connect(address).await.unwrap();
        plain
            .write_all(b"GET /echo HTTP/1.1\r\nHost: h1.test\r\nConnection: close\r\n\r\n")
            .await
            .unwrap();
        let mut response = Vec::new();
        plain.read_to_end(&mut response).await.unwrap();
        let response = String::from_utf8(response).unwrap();
        assert!(response.ends_with("HTTP/1.1 h1.test /echo"), "{response}");
        drop(stop);
        server.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn a_stalled_preface_is_dropped_at_the_header_deadline() {
        let (address, stop, server) = start(true).await;
        let mut stalled = tokio::net::TcpStream::connect(address).await.unwrap();
        stalled.write_all(b"PRI * HTTP/2.0").await.unwrap();
        let mut byte = [0u8; 1];
        let closed = tokio::time::timeout(Duration::from_secs(3), stalled.read(&mut byte)).await;
        assert!(matches!(closed, Ok(Ok(0)) | Ok(Err(_))), "{closed:?}");
        drop(stop);
        server.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn http2_can_be_turned_off() {
        let (address, stop, server) = start(false).await;
        let mut stream = tokio::net::TcpStream::connect(address).await.unwrap();
        stream.write_all(HTTP2_PREFACE).await.unwrap();
        let mut response = Vec::new();
        let _ = stream.read_to_end(&mut response).await;
        assert!(!response.starts_with(&HTTP2_PREFACE[..3]));
        assert!(
            response.is_empty() || response.starts_with(b"HTTP/1.1 4"),
            "{}",
            String::from_utf8_lossy(&response)
        );
        drop(stop);
        server.await.unwrap().unwrap();
    }

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
