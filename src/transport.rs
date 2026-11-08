//! Bounded HTTP/1 and HTTP/2 transport with explicit connection and
//! slow-header limits.

#[cfg(test)]
use axum::Router;
use axum::{
    body::Body,
    extract::ConnectInfo,
    http::{HeaderValue, Request, StatusCode, Uri, Version, header},
    response::Response,
};
use bytes::{Bytes, BytesMut};
use hyper::{
    server::conn::{http1, http2},
    service::service_fn,
};
use hyper_util::rt::{TokioExecutor, TokioIo, TokioTimer};
use std::{
    io,
    sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicU64, AtomicUsize, Ordering},
    },
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite},
    net::TcpListener,
    sync::{Notify, Semaphore, watch},
    task::JoinSet,
    time::Instant,
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
    /// How long a response write may make no progress before the
    /// connection is closed (see [`crate::http1::SendDeadline`]).
    pub send_timeout: Duration,
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
const HTTP2_CHUNK: usize = 16 * 1024;

pub(crate) fn normalize_host(value: &str) -> Option<String> {
    Some(match checked_host(value)? {
        Host::Ipv6(address) => address.to_string(),
        Host::Name(name) => name.to_ascii_lowercase(),
    })
}

enum Host<'a> {
    Ipv6(std::net::Ipv6Addr),
    /// A DNS name as written, without a trailing dot.
    Name(&'a str),
}

/// Validate a `Host` value (host and optional port) without allocating.
///
/// Accepts a bracketed IPv6 address or a DNS name of letters, digits and
/// hyphens (one trailing dot allowed), each optionally followed by `:port`
/// with a port that fits in `u16`. Everything else is refused.
fn checked_host(value: &str) -> Option<Host<'_>> {
    let (checked, host_length) = if let Some(rest) = value.strip_prefix('[') {
        let close = rest.find(']')?;
        let address = rest[..close].parse::<std::net::Ipv6Addr>().ok()?;
        (Host::Ipv6(address), close + 2)
    } else {
        let bytes = value.as_bytes();
        let host_length = memchr::memchr(b':', bytes).unwrap_or(bytes.len());
        let host = &bytes[..host_length];
        let name = host.strip_suffix(b".").unwrap_or(host);
        if name.is_empty() || name.len() > 253 || !valid_labels(name) {
            return None;
        }
        // Only ASCII was accepted, so this slice is on a char boundary.
        (Host::Name(&value[..name.len()]), host_length)
    };
    let suffix = &value[host_length..];
    if !suffix.is_empty() {
        let port = suffix.strip_prefix(':')?;
        if port.is_empty()
            || !port.bytes().all(|value| value.is_ascii_digit())
            || port.parse::<u16>().is_err()
        {
            return None;
        }
    }
    Some(checked)
}

/// Every dot-separated label is 1 to 63 letters, digits or hyphens, and does
/// not start or end with a hyphen.
fn valid_labels(name: &[u8]) -> bool {
    let mut start = 0;
    for (index, &byte) in name.iter().enumerate() {
        if byte == b'.' {
            if !valid_label(&name[start..index]) {
                return false;
            }
            start = index + 1;
        } else if !(byte.is_ascii_alphanumeric() || byte == b'-') {
            return false;
        }
    }
    valid_label(&name[start..])
}

fn valid_label(label: &[u8]) -> bool {
    !label.is_empty() && label.len() <= 63 && label[0] != b'-' && label[label.len() - 1] != b'-'
}

/// The length of the request target as `Uri`'s `Display` writes it.
fn target_length(uri: &axum::http::Uri) -> usize {
    uri.scheme_str().map_or(0, |scheme| scheme.len() + 3)
        + uri
            .authority()
            .map_or(0, |authority| authority.as_str().len())
        + uri.path().len()
        + uri.query().map_or(0, |query| query.len() + 1)
}

/// The first value of a header and whether it has more than one.
fn single(
    headers: &axum::http::HeaderMap,
    name: header::HeaderName,
) -> (Option<&HeaderValue>, bool) {
    let mut values = headers.get_all(name).iter();
    let first = values.next();
    (first, values.next().is_some())
}

/// Whether `value` is a `Host` header value `checked_host` accepts.
pub(crate) fn host_valid(value: &HeaderValue) -> bool {
    value.to_str().ok().and_then(checked_host).is_some()
}

/// Whether `value` is a media type `mime::Mime` accepts. The spellings
/// clients send almost always are matched byte for byte; anything else is
/// parsed.
pub(crate) fn media_type_valid(value: &[u8]) -> bool {
    const COMMON: [&[u8]; 6] = [
        b"application/json",
        b"application/json; charset=utf-8",
        b"text/plain",
        b"text/plain; charset=utf-8",
        b"application/x-www-form-urlencoded",
        b"text/html; charset=utf-8",
    ];
    // `HeaderValue::to_str` takes visible ASCII (and tab) only.
    COMMON.contains(&value)
        || value
            .iter()
            .all(|&byte| byte == b'\t' || (32..127).contains(&byte))
            && std::str::from_utf8(value).is_ok_and(|value| value.parse::<mime::Mime>().is_ok())
}

/// `head_checked` is set for a request from the native HTTP/1 parser that
/// already found its Host, Transfer-Encoding and Content-Type headers valid;
/// the rest is checked here for every request.
fn protocol_rejection(
    request: &Request<Body>,
    target_limit: usize,
    head_checked: bool,
) -> Option<Response> {
    let reject = |status, detail| {
        let mut response = crate::error(status, detail);
        // A rejected request may still carry unread bytes; prevent downstream
        // interpretation of those bytes as another request on this connection.
        response
            .headers_mut()
            .insert(header::CONNECTION, "close".parse().unwrap());
        response
    };
    if target_length(request.uri()) > target_limit {
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
    if head_checked {
        return None;
    }
    let (host, hosts) = single(request.headers(), header::HOST);
    if hosts || (request.version() == Version::HTTP_11 && host.is_none()) {
        return Some(reject(
            StatusCode::BAD_REQUEST,
            "HTTP/1.1 requires exactly one Host header",
        ));
    }
    if let Some(value) = host {
        let valid = value.to_str().ok().and_then(checked_host);
        if valid.is_none() {
            return Some(reject(StatusCode::BAD_REQUEST, "Invalid Host header"));
        }
    }
    if request.headers().contains_key(header::TRANSFER_ENCODING)
        && request.headers().contains_key(header::CONTENT_LENGTH)
    {
        return Some(reject(StatusCode::BAD_REQUEST, "Ambiguous request framing"));
    }
    let (transfer, transfers) = single(request.headers(), header::TRANSFER_ENCODING);
    if transfers
        || transfer.is_some_and(|value| {
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
    let (content_type, content_types) = single(request.headers(), header::CONTENT_TYPE);
    if content_types || content_type.is_some_and(|value| !media_type_valid(value.as_bytes())) {
        return Some(reject(
            StatusCode::BAD_REQUEST,
            "Content-Type must be a single valid media type",
        ));
    }
    None
}

/// Marks a request that arrived over a TLS connection this server terminated,
/// with the client certificate the connection verified, if any.
#[derive(Clone, Debug, Default)]
pub(crate) struct Secure {
    pub(crate) client_certificate: Option<Arc<[u8]>>,
}

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
    /// Set once a TLS handshake completes on this connection.
    pub(crate) tls: Option<Secure>,
}

impl ProtocolChecked {
    /// Serve one request. `peer` is the client address when the caller has it
    /// at hand; otherwise the router reads it from the request's extensions.
    /// `direct` is for the HTTP/1 writer (see `controls::DirectHeaders`).
    pub(crate) async fn call(
        &self,
        mut request: Request<Body>,
        peer: Option<std::net::SocketAddr>,
        direct: Option<&mut crate::controls::DirectHeaders>,
    ) -> Response {
        let head_checked = direct.as_ref().is_some_and(|direct| direct.head_checked);
        if let Some(response) = protocol_rejection(&request, self.target_limit, head_checked) {
            return response;
        }
        if let Some(secure) = &self.tls {
            request.extensions_mut().insert(secure.clone());
        }
        match &self.app {
            Application::Native(dispatcher) => dispatcher.call(request, peer, direct).await,
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
            let response = checked.call(request, None, None).await;
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

/// The listening socket while the accept loop may be using it, so a stop can
/// wake an accept that Windows has left blocked. Cleared before the listener
/// is closed, so a late stop never touches a reused handle.
#[cfg(windows)]
type Accepting = Arc<std::sync::Mutex<Option<std::os::windows::io::RawSocket>>>;
#[cfg(not(windows))]
type Accepting = ();

/// Cancel any accept blocked on the listening socket.
///
/// Workers share one listening socket. When a connection arrives each is told
/// it can accept; one takes it and, now and then, the other's nonblocking
/// accept blocks inside Winsock until the next connection rather than
/// returning "would block". Idle, that is forever, so a stop would never be
/// seen. Cancelling the socket's pending I/O ends that accept as interrupted.
#[cfg(windows)]
fn wake_accept(accepting: &Accepting) {
    // The lock keeps the accept loop from closing the socket meanwhile.
    let socket = accepting
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if let Some(raw) = *socket {
        bessemer_ffi::cancel_pending_io(raw);
    }
}

/// Clears the recorded socket when dropped. A local of `run`, it drops before
/// that function's `listener` argument on any early return, so the record is
/// always cleared before the socket closes.
#[cfg(windows)]
struct ForgetOnDrop(Accepting);

#[cfg(windows)]
impl Drop for ForgetOnDrop {
    fn drop(&mut self) {
        self.0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .take();
    }
}

/// Resolves when the supervisor closes a Windows worker's standard input.
///
/// The supervisor keeps that pipe open after the start byte (see the start
/// gate in `bessemer.cli`) and closes it to have the worker drain, or the
/// pipe closes because the supervisor died. An unsupervised server never
/// resolves it.
#[cfg(windows)]
fn supervisor_stop(supervised: bool, accepting: Accepting) -> io::Result<StopFuture> {
    if !supervised {
        return Ok(Box::pin(std::future::pending()));
    }
    let (sender, receiver) = tokio::sync::oneshot::channel::<()>();
    std::thread::Builder::new()
        .name("bessemer-supervisor-pipe".into())
        .spawn(move || {
            use std::io::Read;
            let mut byte = [0u8; 1];
            let mut input = std::io::stdin().lock();
            loop {
                match input.read(&mut byte) {
                    Ok(0) => break,
                    Ok(_) => {}
                    Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
                    Err(_) => break,
                }
            }
            let _ = sender.send(());
            wake_accept(&accepting);
        })?;
    Ok(Box::pin(async move {
        let _ = receiver.await;
    }))
}

/// Registers the stop source now, so a signal that arrives before the accept
/// loop first polls is not lost.
fn stop_requested(stop: Stop, supervised: bool, accepting: Accepting) -> io::Result<StopFuture> {
    match stop {
        Stop::Signals => {
            #[cfg(unix)]
            let mut terminate =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
            // Windows has no SIGTERM. A console CTRL_BREAK still drains, but
            // the supervisor asks by closing the worker's standard input,
            // which needs no console and cannot be lost.
            #[cfg(windows)]
            let mut terminate = tokio::signal::windows::ctrl_break()?;
            #[cfg(windows)]
            let supervisor = supervisor_stop(supervised, accepting)?;
            #[cfg(unix)]
            let supervisor = {
                let _ = (supervised, accepting);
                std::future::pending::<()>()
            };
            Ok(Box::pin(async move {
                tokio::select! {
                    _ = tokio::signal::ctrl_c() => {}
                    _ = terminate.recv() => {}
                    _ = supervisor => {}
                }
            }))
        }
        Stop::Socket(socket) => {
            // Tokio needs the descriptor non-blocking.
            socket.set_nonblocking(true)?;
            #[cfg(unix)]
            let socket = tokio::net::UnixStream::from_std(socket)?;
            // Windows hands over one end of a loopback TCP pair.
            #[cfg(not(unix))]
            let socket = tokio::net::TcpStream::from_std(socket)?;
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

/// An HTTP/2 extended CONNECT (RFC 8441) that opens a WebSocket.
fn is_websocket_connect<B>(request: &Request<B>) -> bool {
    request.method() == hyper::Method::CONNECT
        && request
            .extensions()
            .get::<hyper::ext::Protocol>()
            .is_some_and(|protocol| protocol.as_str().eq_ignore_ascii_case("websocket"))
}

/// Counts a connection's open streams, so an idle HTTP/2 connection is closed
/// after the header deadline instead of holding its slot.
#[derive(Default)]
struct Http2Activity {
    active: AtomicUsize,
    started: AtomicU64,
    streams: Mutex<Vec<Weak<StreamProgress>>>,
    changed: Notify,
}

struct ActiveStream(Arc<Http2Activity>);

/// A response body that keeps its stream counted as active until it has been
/// sent or dropped, so a long streamed response is never taken for idle.
struct TrackedBody {
    body: Body,
    _stream: ActiveStream,
    pending: Option<Bytes>,
    progress: Arc<StreamProgress>,
}

struct StreamProgress {
    retained: Mutex<(usize, Option<Instant>)>,
    activity: Arc<Http2Activity>,
}

struct TrackedChunk {
    data: Bytes,
    progress: Arc<StreamProgress>,
}

impl AsRef<[u8]> for TrackedChunk {
    fn as_ref(&self) -> &[u8] {
        &self.data
    }
}

impl Drop for TrackedChunk {
    fn drop(&mut self) {
        let mut retained = self.progress.retained.lock().unwrap();
        retained.0 -= 1;
        retained.1 = (retained.0 > 0).then(Instant::now);
    }
}

impl hyper::body::Body for TrackedBody {
    type Data = bytes::Bytes;
    type Error = axum::Error;

    fn poll_frame(
        mut self: std::pin::Pin<&mut Self>,
        context: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Option<Result<hyper::body::Frame<Self::Data>, Self::Error>>> {
        let mut data = match self.pending.take() {
            Some(data) => data,
            None => match std::pin::Pin::new(&mut self.body).poll_frame(context) {
                std::task::Poll::Ready(Some(Ok(frame))) => match frame.into_data() {
                    Ok(data) => data,
                    Err(frame) => return std::task::Poll::Ready(Some(Ok(frame))),
                },
                other => return other,
            },
        };
        let chunk = data.split_to(data.len().min(HTTP2_CHUNK));
        if !data.is_empty() {
            self.pending = Some(data);
        }
        let chunk = if chunk.is_empty() {
            chunk
        } else {
            self.progress.track(chunk)
        };
        std::task::Poll::Ready(Some(Ok(hyper::body::Frame::data(chunk))))
    }

    fn is_end_stream(&self) -> bool {
        self.pending.is_none() && self.body.is_end_stream()
    }

    fn size_hint(&self) -> hyper::body::SizeHint {
        let hint = self.body.size_hint();
        let pending = self.pending.as_ref().map_or(0, |data| data.len() as u64);
        let mut combined = hyper::body::SizeHint::new();
        combined.set_lower(hint.lower().saturating_add(pending));
        if let Some(upper) = hint.upper() {
            combined.set_upper(upper.saturating_add(pending));
        }
        combined
    }
}

impl ActiveStream {
    fn new(activity: Arc<Http2Activity>) -> Self {
        activity.started.fetch_add(1, Ordering::Relaxed);
        activity.active.fetch_add(1, Ordering::Relaxed);
        Self(activity)
    }
}

impl Http2Activity {
    fn track_stream(self: &Arc<Self>) -> Arc<StreamProgress> {
        let progress = Arc::new(StreamProgress {
            retained: Mutex::new((0, None)),
            activity: self.clone(),
        });
        let mut streams = self.streams.lock().unwrap();
        if streams.len() >= 256 {
            streams.retain(|stream| stream.strong_count() > 0);
        }
        streams.push(Arc::downgrade(&progress));
        progress
    }

    fn earliest(&self) -> Option<Instant> {
        let mut streams = self.streams.lock().unwrap();
        streams.retain(|stream| stream.strong_count() > 0);
        streams
            .iter()
            .filter_map(Weak::upgrade)
            .filter_map(|stream| stream.retained.lock().unwrap().1)
            .min()
    }
}

impl StreamProgress {
    fn track(self: &Arc<Self>, data: Bytes) -> Bytes {
        let mut retained = self.retained.lock().unwrap();
        if retained.0 == 0 {
            retained.1 = Some(Instant::now());
        }
        retained.0 += 1;
        drop(retained);
        self.activity.changed.notify_one();
        Bytes::from_owner(TrackedChunk {
            data,
            progress: self.clone(),
        })
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
    context: crate::websocket::ConnectionContext,
) where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    F: Fn() + Send + Sync + 'static,
{
    let io = crate::http1::SendDeadline::new(io, limits.send_timeout);
    let activity = Arc::new(Http2Activity::default());
    // Every stream of a connection shares its slot, so WebSocket handlers are
    // admitted per connection instead: a permit is held from the handshake
    // until the handler has finished, including after a refused or abandoned
    // handshake whose stream has already ended.
    let sessions = Arc::new(Semaphore::new(HTTP2_MAX_STREAMS as usize));
    let on_response = Arc::new(on_response);
    let max_headers = limits.max_headers;
    let service = {
        let activity = activity.clone();
        service_fn(move |request: Request<hyper::body::Incoming>| {
            let checked = checked.clone();
            let on_response = on_response.clone();
            let context = context.clone();
            let activity = activity.clone();
            let sessions = sessions.clone();
            // Dropped with the stream, including when the client resets it.
            let stream = ActiveStream::new(activity.clone());
            async move {
                let mut request = request.map(Body::new);
                let response = match http2_origin_form(&mut request, max_headers) {
                    Some(rejection) => rejection,
                    None => {
                        request.extensions_mut().insert(ConnectInfo(peer));
                        if !is_websocket_connect(&request) {
                            checked.call(request, None, None).await
                        } else if let Ok(permit) = sessions.try_acquire_owned() {
                            // RFC 8441: route it like the HTTP/1.1 GET
                            // handshake. The handler holds the permit, and the
                            // stream counted as open, until it has finished.
                            *request.method_mut() = hyper::Method::GET;
                            let extensions = request.extensions_mut();
                            extensions.insert(crate::websocket::Http2Connect);
                            extensions.insert(crate::websocket::ConnectionContext {
                                _stream: Some(Arc::new((
                                    ActiveStream::new(activity.clone()),
                                    permit,
                                ))),
                                ..context
                            });
                            checked.call(request, None, None).await
                        } else {
                            crate::error(
                                StatusCode::SERVICE_UNAVAILABLE,
                                "Too many WebSocket sessions on this connection",
                            )
                        }
                    }
                };
                on_response();
                let progress = activity.track_stream();
                Ok::<_, std::convert::Infallible>(response.map(|body| TrackedBody {
                    body,
                    _stream: stream,
                    pending: None,
                    progress,
                }))
            }
        })
    };
    let mut builder = http2::Builder::new(TokioExecutor::new());
    builder
        .timer(TokioTimer::new())
        .enable_connect_protocol()
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
    let deadline = loop {
        let retained_until = activity.earliest().map(|since| since + limits.send_timeout);
        tokio::select! {
            _ = &mut connection => return,
            // Open streams may finish within the drain deadline.
            _ = stopped.wait_for(|stopped| *stopped) => break limits.graceful_timeout,
            _ = activity.changed.notified() => {}
            _ = async {
                match retained_until {
                    Some(deadline) => tokio::time::sleep_until(deadline).await,
                    None => std::future::pending().await,
                }
            } => {
                if activity.earliest().is_some_and(|since| since.elapsed() >= limits.send_timeout) {
                    return;
                }
            }
            _ = idle.tick() => {
                let started = activity.started.load(Ordering::Relaxed);
                if started == seen && activity.active.load(Ordering::Relaxed) == 0 {
                    // Nothing is open, so the close only waits on the peer.
                    break limits.header_timeout;
                }
                seen = started;
            }
        }
    };
    // GOAWAY: open streams finish, new ones are refused. A peer that never
    // completes the handshake cannot finish a graceful close, so the wait is
    // bounded and the connection is then dropped.
    connection.as_mut().graceful_shutdown();
    let _ = tokio::time::timeout(deadline, connection).await;
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
    let stream = crate::http1::SendDeadline::new(stream, limits.send_timeout);
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

/// Check the TLS files every reload interval and swap in a changed
/// configuration. File reads and parsing run off the async workers.
async fn reload_certificates(tls: Arc<crate::tls::Reloadable>, mut stopped: watch::Receiver<bool>) {
    let mut ticks = tokio::time::interval(tls.reload_interval());
    ticks.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    ticks.tick().await;
    loop {
        tokio::select! {
            _ = ticks.tick() => {}
            _ = stopped.wait_for(|stopped| *stopped) => return,
        }
        let check = tls.clone();
        match tokio::task::spawn_blocking(move || check.reload_if_changed()).await {
            Ok(Ok(true)) => diagnostic!("Bessemer TLS: reloaded the certificate files"),
            Ok(Ok(false)) => {}
            Ok(Err(error)) => {
                diagnostic!("Bessemer TLS: keeping the current certificate; reload failed: {error}")
            }
            Err(_) => return,
        }
    }
}

/// Where connections come from: Tokio's epoll-driven listener, or (with the
/// Rust event loop on Linux, `io_backend="io_uring"`) the loop thread's
/// io_uring ring.
pub enum Listener {
    Epoll(TcpListener),
    #[cfg(target_os = "linux")]
    Uring(bessemer_loop::uring::Listener),
}

impl Listener {
    /// Serve `listener` through io_uring when `io_uring` is set and the ring
    /// can be set up on this thread, else through Tokio's epoll reactor. Must
    /// run on the thread and inside the runtime that will serve it.
    pub fn new(listener: TcpListener, io_uring: bool) -> io::Result<Self> {
        if !io_uring {
            return Ok(Self::Epoll(listener));
        }
        #[cfg(target_os = "linux")]
        {
            match bessemer_loop::uring::Listener::new(listener.into_std()?) {
                Ok(listener) => {
                    diagnostic!("Bessemer: serving connections through io_uring");
                    Ok(Self::Uring(listener))
                }
                Err((listener, error)) => {
                    diagnostic!("Bessemer: io_uring is unavailable ({error}); serving on epoll");
                    Ok(Self::Epoll(TcpListener::from_std(listener)?))
                }
            }
        }
        #[cfg(not(target_os = "linux"))]
        {
            diagnostic!("Bessemer: io_uring is only available on Linux; serving on epoll");
            Ok(Self::Epoll(listener))
        }
    }

    #[cfg(windows)]
    fn raw_socket(&self) -> std::os::windows::io::RawSocket {
        use std::os::windows::io::AsRawSocket;
        match self {
            Self::Epoll(listener) => listener.as_raw_socket(),
        }
    }

    async fn accept(&self) -> io::Result<(Connection, std::net::SocketAddr)> {
        match self {
            Self::Epoll(listener) => listener
                .accept()
                .await
                .map(|(stream, peer)| (Connection::Epoll(stream), peer)),
            #[cfg(target_os = "linux")]
            Self::Uring(listener) => listener
                .accept()
                .await
                .map(|(stream, peer)| (Connection::Uring(stream), peer)),
        }
    }
}

/// An accepted connection of either [`Listener`].
enum Connection {
    Epoll(tokio::net::TcpStream),
    #[cfg(target_os = "linux")]
    Uring(bessemer_loop::uring::Stream),
}

impl Connection {
    fn set_nodelay(&self, nodelay: bool) -> io::Result<()> {
        match self {
            Self::Epoll(stream) => stream.set_nodelay(nodelay),
            #[cfg(target_os = "linux")]
            Self::Uring(stream) => stream.set_nodelay(nodelay),
        }
    }

    fn try_write(&self, buf: &[u8]) -> io::Result<usize> {
        match self {
            Self::Epoll(stream) => stream.try_write(buf),
            #[cfg(target_os = "linux")]
            Self::Uring(stream) => stream.try_write(buf),
        }
    }
}

macro_rules! connection_io {
    ($self:ident, $stream:ident => $call:expr) => {
        match $self.get_mut() {
            Connection::Epoll($stream) => $call,
            #[cfg(target_os = "linux")]
            Connection::Uring($stream) => $call,
        }
    };
}

impl AsyncRead for Connection {
    fn poll_read(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        buf: &mut tokio::io::ReadBuf<'_>,
    ) -> std::task::Poll<io::Result<()>> {
        connection_io!(self, stream => std::pin::Pin::new(stream).poll_read(cx, buf))
    }
}

impl AsyncWrite for Connection {
    fn poll_write(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        buf: &[u8],
    ) -> std::task::Poll<io::Result<usize>> {
        connection_io!(self, stream => std::pin::Pin::new(stream).poll_write(cx, buf))
    }

    fn poll_write_vectored(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        bufs: &[io::IoSlice<'_>],
    ) -> std::task::Poll<io::Result<usize>> {
        connection_io!(self, stream => std::pin::Pin::new(stream).poll_write_vectored(cx, bufs))
    }

    fn is_write_vectored(&self) -> bool {
        match self {
            Self::Epoll(stream) => stream.is_write_vectored(),
            #[cfg(target_os = "linux")]
            Self::Uring(stream) => stream.is_write_vectored(),
        }
    }

    fn poll_flush(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<io::Result<()>> {
        connection_io!(self, stream => std::pin::Pin::new(stream).poll_flush(cx))
    }

    fn poll_shutdown(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<io::Result<()>> {
        connection_io!(self, stream => std::pin::Pin::new(stream).poll_shutdown(cx))
    }
}

pub async fn run(
    listener: Listener,
    app: Application,
    limits: Limits,
    ready_fd: Option<i64>,
    stop: Stop,
    tls: Option<Arc<crate::tls::Reloadable>>,
) -> io::Result<()> {
    let permits = Arc::new(Semaphore::new(limits.max_connections));
    let completed = Arc::new(AtomicU64::new(0));
    let recycle = Arc::new(Notify::new());
    let (stop_sender, stop_receiver) = watch::channel(false);
    let mut connections = JoinSet::new();
    let reloader = tls
        .clone()
        .filter(|tls| !tls.reload_interval().is_zero())
        .map(|tls| tokio::spawn(reload_certificates(tls, stop_receiver.clone())));
    // Register signals before announcing readiness to the supervisor.
    // Forgets the socket before it closes, on every way out of this function.
    #[cfg(windows)]
    let accepting = ForgetOnDrop(Arc::new(std::sync::Mutex::new(Some(listener.raw_socket()))));
    #[cfg(windows)]
    let wakes: Accepting = accepting.0.clone();
    #[cfg(not(windows))]
    let wakes: Accepting = ();
    let mut stopped_by = stop_requested(stop, ready_fd.is_some(), wakes)?;
    if let Some(fd) = ready_fd {
        bessemer_ffi::signal_ready(fd)?;
    }
    loop {
        let accepted = tokio::select! {
            // A stop is seen before another accept is tried.
            biased;
            _ = &mut stopped_by => break,
            _ = recycle.notified() => break,
            _ = connections.join_next(), if !connections.is_empty() => continue,
            result = listener.accept() => result,
        };
        let (stream, peer) = match accepted {
            Ok(value) => value,
            Err(error) => {
                // A cancelled accept (see `wake_accept`) reports WSAEINTR or
                // ERROR_OPERATION_ABORTED; the next turn sees the stop.
                if matches!(
                    error.kind(),
                    io::ErrorKind::ConnectionAborted | io::ErrorKind::Interrupted
                ) || (cfg!(windows) && error.raw_os_error() == Some(995))
                {
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
                if tls.is_none() {
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
        // Each connection takes the configuration current when it arrives.
        let acceptor = tls
            .as_ref()
            .map(|tls| tokio_rustls::TlsAcceptor::from(tls.current()));
        // Upgraded WebSocket sessions keep a clone of this connection slot.
        let context = crate::websocket::ConnectionContext {
            _slot: Arc::new(permit),
            stop: stop_receiver.clone(),
            _stream: None,
        };
        // A dropped io_uring stream keeps sending what it was given; its slot
        // stays taken until that ends, so lingering connections count against
        // max_connections and the graceful drain waits for them.
        #[cfg(target_os = "linux")]
        if let Connection::Uring(stream) = &stream {
            stream.hold_until_closed(Box::new(context._slot.clone()));
        }
        // Boxed: the connection's future (HTTP/1 serving, upgrades and the
        // request futures inside them) is many kilobytes, and the runtime
        // moves a spawned future several times before it first runs.
        connections.spawn(Box::pin(async move {
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
            let mut protected = ProtocolChecked {
                app,
                target_limit: limits.max_request_target_size,
                tls: None,
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
                        serve_http2(stream, peer, protected, limits, count, stopped, context)
                            .await;
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
            protected.tls = Some(Secure {
                client_certificate: crate::tls::client_certificate(stream.get_ref().1),
            });
            if stream.get_ref().1.alpn_protocol() == Some(b"h2") {
                serve_http2(stream, peer, protected, limits, count, stopped, context).await;
                return;
            }
            serve_http1(
                stream, peer, protected, limits, count, stopped, completed, recycle, context,
            )
            .await;
        }));
    }
    if let Some(reloader) = reloader {
        reloader.abort();
    }
    if let Some(fd) = ready_fd {
        bessemer_ffi::signal_draining(fd)?;
    }
    #[cfg(windows)]
    drop(accepting);
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
        diagnostic!("Bessemer native drain deadline exceeded; cancelling remaining connections");
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

    /// A body that yields one chunk every 200 ms, ten times.
    struct Slow {
        remaining: u8,
        sleep: std::pin::Pin<Box<tokio::time::Sleep>>,
    }

    impl hyper::body::Body for Slow {
        type Data = bytes::Bytes;
        type Error = std::convert::Infallible;

        fn poll_frame(
            mut self: std::pin::Pin<&mut Self>,
            context: &mut std::task::Context<'_>,
        ) -> std::task::Poll<Option<Result<hyper::body::Frame<Self::Data>, Self::Error>>> {
            if self.remaining == 0 {
                return std::task::Poll::Ready(None);
            }
            std::task::ready!(self.sleep.as_mut().poll(context));
            self.remaining -= 1;
            let next = tokio::time::Instant::now() + Duration::from_millis(200);
            self.sleep.as_mut().reset(next);
            std::task::Poll::Ready(Some(Ok(hyper::body::Frame::data(
                bytes::Bytes::from_static(b"x"),
            ))))
        }
    }

    async fn start(
        http2: bool,
    ) -> (
        std::net::SocketAddr,
        std::os::unix::net::UnixStream,
        tokio::task::JoinHandle<io::Result<()>>,
    ) {
        start_with_limits(http2, Duration::from_secs(30), Duration::from_millis(300)).await
    }

    async fn start_with_limits(
        http2: bool,
        send_timeout: Duration,
        header_timeout: Duration,
    ) -> (
        std::net::SocketAddr,
        std::os::unix::net::UnixStream,
        tokio::task::JoinHandle<io::Result<()>>,
    ) {
        let app = Router::new()
            .route(
                "/large",
                get(|| async { Body::from(vec![b'x'; 256 * 1024]) }),
            )
            .route(
                "/slow",
                get(|| async {
                    Body::new(Slow {
                        remaining: 10,
                        sleep: Box::pin(tokio::time::sleep(Duration::ZERO)),
                    })
                }),
            )
            .route(
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
            header_timeout,
            max_headers: 8,
            max_header_size: 8192,
            max_request_target_size: 8192,
            graceful_timeout: Duration::from_secs(5),
            send_timeout,
            max_requests: 0,
            http2,
        };
        let server = tokio::spawn(run(
            Listener::Epoll(listener),
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
    async fn bytes_that_are_not_http_are_closed_without_an_http_answer() {
        let (address, stop, server) = start(true).await;
        // h2spec 3.5/2: an invalid preface must not get an HTTP/1 reply.
        let mut invalid = tokio::net::TcpStream::connect(address).await.unwrap();
        invalid
            .write_all(b"INVALID CONNECTION PREFACE\r\n\r\n")
            .await
            .unwrap();
        let mut answer = Vec::new();
        let _ = invalid.read_to_end(&mut answer).await;
        assert!(answer.is_empty(), "{}", String::from_utf8_lossy(&answer));
        // A malformed request that is still HTTP gets its error response.
        let mut malformed = tokio::net::TcpStream::connect(address).await.unwrap();
        malformed
            .write_all(b"GET /echo HTTP/9\r\nHost: h\r\n\r\n")
            .await
            .unwrap();
        let mut answer = Vec::new();
        let _ = malformed.read_to_end(&mut answer).await;
        assert!(
            answer.starts_with(b"HTTP/1.1 400"),
            "{}",
            String::from_utf8_lossy(&answer)
        );
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
    async fn a_streamed_response_outlasting_the_idle_period_completes() {
        let (address, stop, server) = start(true).await;
        let (mut sender, _connection) = http2_client(address).await;
        let request = Request::get("http://example.test/slow")
            .body(Body::empty())
            .unwrap();
        // Two seconds of streaming against a 300 ms idle deadline.
        assert_eq!(
            send(&mut sender, request).await,
            (StatusCode::OK, "x".repeat(10))
        );
        drop(stop);
        server.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn stalled_http2_flow_control_closes_the_connection() {
        let (address, stop, server) =
            start_with_limits(true, Duration::from_millis(100), Duration::from_secs(5)).await;
        let mut stream = tokio::net::TcpStream::connect(address).await.unwrap();
        let mut request = HTTP2_PREFACE.to_vec();
        request.extend_from_slice(&[0, 0, 6, 4, 0, 0, 0, 0, 0, 0, 4, 0, 0, 0, 0]);
        let mut headers = vec![0x82, 0x86, 0x01, 12];
        headers.extend_from_slice(b"example.test");
        headers.extend_from_slice(&[0x04, 6]);
        headers.extend_from_slice(b"/large");
        request.extend_from_slice(&[0, 0, headers.len() as u8, 1, 5, 0, 0, 0, 1]);
        request.extend_from_slice(&headers);
        stream.write_all(&request).await.unwrap();
        let mut answer = Vec::new();
        let closed =
            tokio::time::timeout(Duration::from_secs(2), stream.read_to_end(&mut answer)).await;
        assert!(
            closed.is_ok(),
            "an HTTP/2 stream held response bytes without WINDOW_UPDATE"
        );
        let mut offset = 0;
        let mut answered = false;
        while answer.len() >= offset + 9 {
            let size = ((answer[offset] as usize) << 16)
                | ((answer[offset + 1] as usize) << 8)
                | answer[offset + 2] as usize;
            if answer.len() < offset + 9 + size {
                break;
            }
            let stream_id = u32::from_be_bytes(answer[offset + 5..offset + 9].try_into().unwrap());
            answered |= answer[offset + 3] == 1 && stream_id == 1;
            offset += 9 + size;
        }
        assert!(answered, "the server did not answer the request");
        drop(stop);
        server.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn an_idle_http2_stream_does_not_trigger_the_send_timeout() {
        let (address, stop, server) =
            start_with_limits(true, Duration::from_millis(100), Duration::from_secs(5)).await;
        let (mut sender, _connection) = http2_client(address).await;
        let request = Request::get("http://example.test/slow")
            .body(Body::empty())
            .unwrap();
        assert_eq!(send(&mut sender, request).await.1, "x".repeat(10));
        drop(stop);
        server.await.unwrap().unwrap();
    }

    #[tokio::test(start_paused = true)]
    async fn http2_stream_timeout_tracks_completed_chunks() {
        let activity = Arc::new(Http2Activity::default());
        let progress = activity.track_stream();
        let first = progress.track(Bytes::from_static(b"first"));
        let second = progress.track(Bytes::from_static(b"second"));
        let started = activity.earliest().unwrap();
        tokio::time::advance(Duration::from_millis(80)).await;
        drop(first);
        assert!(activity.earliest().unwrap() > started);
        tokio::time::advance(Duration::from_millis(80)).await;
        assert!(activity.earliest().unwrap().elapsed() < Duration::from_millis(100));
        drop(second);
        assert!(activity.earliest().is_none());
    }

    #[tokio::test]
    async fn a_client_that_stalls_after_the_preface_is_closed() {
        let (address, stop, server) = start(true).await;
        let mut stalled = tokio::net::TcpStream::connect(address).await.unwrap();
        stalled.write_all(HTTP2_PREFACE).await.unwrap();
        // Never send SETTINGS; the server's own frames are read and ignored
        // until it closes the connection.
        let closed = tokio::time::timeout(Duration::from_secs(3), async {
            let mut sink = Vec::new();
            stalled.read_to_end(&mut sink).await
        })
        .await;
        assert!(closed.is_ok(), "a stalled HTTP/2 handshake held its slot");
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
    fn host_values_are_checked_without_a_uri_parser() {
        let accepted = [
            ("localhost", "localhost"),
            ("Example.COM", "example.com"),
            ("example.com.", "example.com"),
            ("example.com:8080", "example.com"),
            ("a-b.c1:0", "a-b.c1"),
            ("127.0.0.1:65535", "127.0.0.1"),
            ("[::1]", "::1"),
            ("[0:0::1]:443", "::1"),
        ];
        for (value, normalized) in accepted {
            assert_eq!(
                normalize_host(value).as_deref(),
                Some(normalized),
                "{value}"
            );
        }
        let refused = [
            "",
            ":80",
            ".",
            "example..com",
            "-a.com",
            "a-.com",
            "a_b.com",
            "a b",
            "user@host",
            "host/path",
            "host?q",
            "host#f",
            "a,b",
            "a\\b",
            "host:",
            "host:65536",
            "host:+1",
            "host:1:2",
            "[::1",
            "[::1]x",
            "[::1]:",
            "[fe80::1%25eth0]",
            "[::1]]",
            "ex%41mple.com",
            "\tlocalhost",
        ];
        for value in refused {
            assert_eq!(normalize_host(value), None, "{value:?}");
        }
        assert_eq!(normalize_host(&"a".repeat(64)), None);
        assert!(normalize_host(&format!("{}.com", "a".repeat(63))).is_some());
    }

    #[test]
    fn request_target_length_matches_its_text() {
        for target in [
            "/",
            "/a/b?x=1&y",
            "http://example.com:80/p?q",
            "example.com:443",
            "*",
        ] {
            let uri: Uri = target.parse().unwrap();
            assert_eq!(target_length(&uri), uri.to_string().len(), "{target}");
        }
    }

    #[test]
    fn origin_host_and_framing_are_validated() {
        let request = Request::builder().uri("/").body(Body::empty()).unwrap();
        assert_eq!(
            protocol_rejection(&request, 8192, false).unwrap().status(),
            StatusCode::BAD_REQUEST
        );
        let request = Request::builder()
            .uri("/")
            .header("host", "localhost")
            .body(Body::empty())
            .unwrap();
        assert!(protocol_rejection(&request, 8192, false).is_none());
        let request = Request::builder()
            .uri("/")
            .header("host", "localhost")
            .header("host", "evil.test")
            .body(Body::empty())
            .unwrap();
        assert_eq!(
            protocol_rejection(&request, 8192, false).unwrap().status(),
            StatusCode::BAD_REQUEST
        );
        let request = Request::builder()
            .uri("http://evil.test/")
            .header("host", "localhost")
            .body(Body::empty())
            .unwrap();
        assert!(protocol_rejection(&request, 8192, false).is_some());
        let request = Request::builder()
            .uri("/too-long")
            .header("host", "localhost")
            .body(Body::empty())
            .unwrap();
        assert_eq!(
            protocol_rejection(&request, 2, false).unwrap().status(),
            StatusCode::URI_TOO_LONG
        );
    }
}
