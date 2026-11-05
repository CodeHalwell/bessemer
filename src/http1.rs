//! Purpose-built HTTP/1.1 server connection.
//!
//! Hyper's general-purpose connection machinery (dispatcher, buffered I/O,
//! per-request header timers, boxed service futures, encoder state) cost several
//! times more CPU per request than parsing and writing the bytes themselves.
//! This module owns the connection loop directly: `httparse` parses each request
//! head out of one reusable buffer, request bodies are framed here and streamed
//! to the application only when it reads them, and the response head and an
//! in-memory body leave in a single write.
//!
//! Wire behaviour follows hyper's server role, which it replaces: the same parse
//! error statuses (400, 414, 431), header-section limits and header read
//! deadline, Content-Length and chunked request framing rules, `Expect:
//! 100-continue`, HTTP/1.0 keep-alive negotiation, automatic `date`,
//! `content-length` and `transfer-encoding: chunked` headers, HEAD/204/304 body
//! rules, and `connection: close` on the last response of a connection. Requests
//! that ask for a protocol upgrade (including CONNECT) are handed, with every
//! byte already read, to hyper, which keeps supporting upgrades unchanged.
//!
//! One deliberate difference: a request carrying both `Transfer-Encoding` and
//! `Content-Length` is refused with 400 instead of being read as chunked.

use axum::{
    body::{Body, Bytes},
    extract::ConnectInfo,
    http::{
        HeaderMap, HeaderName, HeaderValue, Method, Request, Response, StatusCode, Uri, Version,
        header,
    },
};
use bytes::{Buf, BytesMut};
use http_body::{Body as _, Frame, SizeHint};
use http_body_util::BodyExt;
use std::{
    cell::RefCell,
    io,
    mem::MaybeUninit,
    net::SocketAddr,
    pin::Pin,
    task::{Context, Poll},
    time::{Duration, SystemTime},
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, ReadBuf},
    sync::{mpsc, watch},
    time::{Instant, Sleep},
};

/// Connection limits, mirroring the hyper builder settings they replace.
#[derive(Clone, Copy)]
pub(crate) struct Config {
    pub header_timeout: Duration,
    pub max_headers: usize,
    pub max_header_size: usize,
}

/// Longest chunk-size line (size, extensions and CRLF) accepted in a chunked body.
const MAX_CHUNK_LINE: usize = 4096;
/// Largest response body copied next to its head for a single plain write.
const INLINE_BODY: usize = 16 * 1024;
/// Largest exact-length body gathered before writing. Exact length does not
/// mean in memory (files stream from disk), so longer bodies are written frame
/// by frame and per-connection memory stays bounded.
const GATHER_LIMIT: u64 = 256 * 1024;
/// Initial read buffer capacity; it grows only for large heads or bodies.
const READ_CAPACITY: usize = 8192;

/// What the connection task should do after a request/response exchange.
enum Next {
    KeepAlive,
    Close,
}

/// The result of reading one request head.
enum Head {
    Request(Box<Parsed>),
    /// The peer closed an idle connection.
    Closed,
    /// A protocol error that has already been answered (or needs no answer).
    Failed,
    /// Shutdown was requested while the connection was idle.
    Stopped,
}

struct Parsed {
    request: Request<()>,
    framing: Framing,
    keep_alive: bool,
    expect_continue: bool,
    /// The raw head of a request that asks for a protocol upgrade, kept so it
    /// can be replayed to hyper.
    upgrade: Option<Bytes>,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Framing {
    Empty,
    Length(u64),
    Chunked,
}

/// Serve requests on one connection until it closes, fails, or must stop.
///
/// `on_response` runs once per response written, for request-count recycling.
pub(crate) async fn serve<S, F>(
    mut stream: S,
    peer: SocketAddr,
    app: crate::transport::ProtocolChecked,
    config: Config,
    mut stop: watch::Receiver<bool>,
    on_response: F,
    upgrade: impl AsyncFnOnce(Rewind<S>, crate::transport::ProtocolChecked),
) where
    S: AsyncRead + AsyncWrite + Unpin,
    F: Fn(),
{
    let mut buffer = BytesMut::with_capacity(READ_CAPACITY);
    let mut deadline = Box::pin(tokio::time::sleep(config.header_timeout));
    loop {
        let parsed =
            match read_head(&mut stream, &mut buffer, &config, &mut deadline, &mut stop).await {
                Head::Request(parsed) => parsed,
                Head::Closed | Head::Failed | Head::Stopped => return,
            };
        let Parsed {
            request,
            framing,
            keep_alive,
            expect_continue,
            upgrade: wants_upgrade,
        } = *parsed;
        if let Some(head) = wants_upgrade {
            // Hand the whole connection to hyper: rewind the parsed head and any
            // bytes after it so hyper reads the request exactly as sent.
            drop(request);
            let mut prefix = BytesMut::with_capacity(head.len() + buffer.len());
            prefix.extend_from_slice(&head);
            prefix.extend_from_slice(&buffer);
            let rewind = Rewind {
                prefix: prefix.freeze(),
                stream,
            };
            upgrade(rewind, app).await;
            return;
        }
        let next = exchange(
            &mut stream,
            &mut buffer,
            &app,
            peer,
            request,
            framing,
            expect_continue,
            keep_alive && !*stop.borrow(),
            config.max_header_size,
        )
        .await;
        on_response();
        match next {
            Ok(Next::KeepAlive) if !*stop.borrow() => continue,
            Ok(_) | Err(_) => {
                let _ = stream.shutdown().await;
                return;
            }
        }
    }
}

/// Read and parse the next request head, enforcing the header deadline.
async fn read_head(
    stream: &mut (impl AsyncRead + AsyncWrite + Unpin),
    buffer: &mut BytesMut,
    config: &Config,
    deadline: &mut Pin<Box<Sleep>>,
    stop: &mut watch::Receiver<bool>,
) -> Head {
    // Like hyper, the header deadline runs from when the server starts waiting
    // for a head, which includes idle keep-alive time.
    deadline
        .as_mut()
        .reset(Instant::now() + config.header_timeout);
    loop {
        // The deadline bounds the whole head, so check it before accepting any
        // more input: a peer that keeps the socket readable (for example with
        // endless blank lines) must not outrun it.
        if deadline.is_elapsed() {
            return Head::Failed;
        }
        // Tolerate empty lines before a request line (RFC 9112 section 2.2).
        while buffer.starts_with(b"\r\n") {
            buffer.advance(2);
        }
        if buffer.first() == Some(&b'\n') {
            buffer.advance(1);
            continue;
        }
        if !buffer.is_empty() {
            match parse_head(buffer, config) {
                Ok(Some(parsed)) => return Head::Request(parsed),
                Ok(None) => {
                    if buffer.len() >= config.max_header_size {
                        let _ =
                            write_error(stream, StatusCode::REQUEST_HEADER_FIELDS_TOO_LARGE).await;
                        return Head::Failed;
                    }
                }
                Err(status) => {
                    // Bytes whose request line names no HTTP version are
                    // another protocol (such as a bad HTTP/2 preface on an
                    // auto-detecting port): an HTTP answer would only be
                    // misread, so the connection is closed instead.
                    if names_http_version(buffer) {
                        let _ = write_error(stream, status).await;
                    }
                    return Head::Failed;
                }
            }
        }
        let idle = buffer.is_empty();
        if buffer.capacity() - buffer.len() < 1024 {
            buffer.reserve(READ_CAPACITY);
        }
        tokio::select! {
            biased;
            _ = deadline.as_mut() => {
                // Hyper closes without a response when the header deadline passes.
                return Head::Failed;
            }
            read = stream.read_buf(buffer) => match read {
                Ok(0) => return if idle { Head::Closed } else {
                    // EOF in the middle of a head is a parse failure.
                    let _ = write_error(stream, StatusCode::BAD_REQUEST).await;
                    Head::Failed
                },
                Ok(_) => {}
                Err(_) => return Head::Failed,
            },
            _ = stop.changed(), if idle => return Head::Stopped,
        }
    }
}

/// Parse a complete head from the front of `buffer`, consuming it.
///
/// Returns `Ok(None)` when more bytes are needed, or the status of the
/// automatic error response hyper sends for the same failure.
fn parse_head(buffer: &mut BytesMut, config: &Config) -> Result<Option<Box<Parsed>>, StatusCode> {
    // Header slots live on the stack for the usual limit; larger limits spill.
    const STACK_HEADERS: usize = 128;
    if config.max_headers <= STACK_HEADERS {
        let scanned = {
            let mut slots =
                [const { MaybeUninit::<httparse::Header<'_>>::uninit() }; STACK_HEADERS];
            scan(&buffer[..], config, &mut slots[..config.max_headers])?
        };
        scanned.map(|scanned| build(buffer, scanned)).transpose()
    } else {
        let scanned = {
            let mut slots: Vec<MaybeUninit<httparse::Header<'_>>> = (0..config.max_headers)
                .map(|_| MaybeUninit::uninit())
                .collect();
            scan(&buffer[..], config, &mut slots)?
        };
        scanned.map(|scanned| build(buffer, scanned)).transpose()
    }
}

/// Whether a request line that failed to parse still names an HTTP version
/// (or is too short to say), so an HTTP error response is meaningful.
fn names_http_version(buffer: &[u8]) -> bool {
    let line = buffer.split(|&byte| byte == b'\n').next().unwrap_or(buffer);
    let line = line.strip_suffix(b"\r").unwrap_or(line);
    let mut tokens = line
        .split(|&byte| byte == b' ')
        .filter(|token| !token.is_empty());
    match (tokens.next(), tokens.next(), tokens.next()) {
        (Some(_), Some(_), Some(version)) => version.starts_with(b"HTTP/"),
        _ => true,
    }
}

/// Byte offsets of a parsed head, so it can be split off the buffer zero-copy.
struct Scanned {
    length: usize,
    method: Method,
    path: (usize, usize),
    version: Version,
    headers: Vec<(usize, usize, usize, usize)>,
}

fn scan<'b>(
    bytes: &'b [u8],
    config: &Config,
    slots: &mut [MaybeUninit<httparse::Header<'b>>],
) -> Result<Option<Scanned>, StatusCode> {
    let mut indices: Vec<(usize, usize, usize, usize)> = Vec::new();
    let (length, method, path, version) = {
        let mut request = httparse::Request::new(&mut []);
        let length = match request.parse_with_uninit_headers(bytes, slots) {
            Ok(httparse::Status::Complete(length)) => length,
            Ok(httparse::Status::Partial) => return Ok(None),
            Err(httparse::Error::TooManyHeaders) => {
                return Err(StatusCode::REQUEST_HEADER_FIELDS_TOO_LARGE);
            }
            Err(_) => return Err(StatusCode::BAD_REQUEST),
        };
        if length > config.max_header_size {
            return Err(StatusCode::REQUEST_HEADER_FIELDS_TOO_LARGE);
        }
        indices.reserve(request.headers.len());
        let base = bytes.as_ptr() as usize;
        let offset = |slice: &[u8]| slice.as_ptr() as usize - base;
        for item in request.headers.iter() {
            let name = offset(item.name.as_bytes());
            let value = offset(item.value);
            indices.push((
                name,
                name + item.name.len(),
                value,
                value + item.value.len(),
            ));
        }
        let method = Method::from_bytes(request.method.unwrap_or("").as_bytes())
            .map_err(|_| StatusCode::BAD_REQUEST)?;
        let path = request.path.unwrap_or("");
        let path = (
            offset(path.as_bytes()),
            offset(path.as_bytes()) + path.len(),
        );
        let version = if request.version == Some(1) {
            Version::HTTP_11
        } else {
            Version::HTTP_10
        };
        (length, method, path, version)
    };
    Ok(Some(Scanned {
        length,
        method,
        path,
        version,
        headers: indices,
    }))
}

fn build(buffer: &mut BytesMut, scanned: Scanned) -> Result<Box<Parsed>, StatusCode> {
    let Scanned {
        length,
        method,
        path,
        version,
        headers: indices,
    } = scanned;
    let head = buffer.split_to(length).freeze();
    if path.1 - path.0 > MAX_URI_LENGTH {
        return Err(StatusCode::URI_TOO_LONG);
    }
    let uri =
        Uri::from_maybe_shared(head.slice(path.0..path.1)).map_err(|_| StatusCode::BAD_REQUEST)?;
    let http11 = version == Version::HTTP_11;
    let mut keep_alive = http11;
    let mut saw_close = false;
    let mut framing = Framing::Empty;
    let mut content_length: Option<u64> = None;
    let mut has_length = false;
    let mut has_transfer_encoding = false;
    let mut chunked = false;
    let mut expect_continue = false;
    let mut upgrade = method == Method::CONNECT;
    let mut headers = HeaderMap::with_capacity(indices.len());
    for (name_start, name_end, value_start, value_end) in indices {
        let name = HeaderName::from_bytes(&head[name_start..name_end])
            .map_err(|_| StatusCode::BAD_REQUEST)?;
        let value = HeaderValue::from_maybe_shared(head.slice(value_start..value_end))
            .map_err(|_| StatusCode::BAD_REQUEST)?;
        match name {
            header::TRANSFER_ENCODING => {
                if !http11 {
                    return Err(StatusCode::BAD_REQUEST);
                }
                // A message with both framings is a request-smuggling vector;
                // refuse it outright rather than pick one.
                if has_length {
                    return Err(StatusCode::BAD_REQUEST);
                }
                has_transfer_encoding = true;
                chunked = is_chunked(&value);
                framing = if chunked {
                    Framing::Chunked
                } else {
                    Framing::Empty
                };
            }
            header::CONTENT_LENGTH => {
                if has_transfer_encoding {
                    return Err(StatusCode::BAD_REQUEST);
                }
                has_length = true;
                let length = parse_content_length(&value).ok_or(StatusCode::BAD_REQUEST)?;
                if let Some(previous) = content_length {
                    if previous != length {
                        return Err(StatusCode::BAD_REQUEST);
                    }
                    continue;
                }
                content_length = Some(length);
                framing = if length == 0 {
                    Framing::Empty
                } else {
                    Framing::Length(length)
                };
            }
            header::CONNECTION => {
                if saw_close || has_token(&value, "close") {
                    saw_close = true;
                    keep_alive = false;
                } else if !keep_alive {
                    keep_alive = has_token(&value, "keep-alive");
                }
            }
            header::EXPECT => {
                expect_continue = value.as_bytes().eq_ignore_ascii_case(b"100-continue");
            }
            header::UPGRADE => upgrade = http11,
            _ => {}
        }
        headers.append(name, value);
    }
    if has_transfer_encoding && !chunked {
        return Err(StatusCode::BAD_REQUEST);
    }
    let mut request = Request::new(());
    *request.method_mut() = method;
    *request.uri_mut() = uri;
    *request.version_mut() = version;
    *request.headers_mut() = headers;
    Ok(Box::new(Parsed {
        request,
        framing,
        keep_alive: keep_alive && !upgrade,
        expect_continue: expect_continue && http11,
        upgrade: upgrade.then_some(head),
    }))
}

/// Hyper's own request-target ceiling, applied before the configured one.
const MAX_URI_LENGTH: usize = (u16::MAX - 1) as usize;

fn is_chunked(value: &HeaderValue) -> bool {
    // `chunked` must be the final coding.
    value
        .to_str()
        .ok()
        .and_then(|text| text.rsplit(',').next())
        .is_some_and(|last| last.trim().eq_ignore_ascii_case("chunked"))
}

fn has_token(value: &HeaderValue, token: &str) -> bool {
    value.to_str().is_ok_and(|text| {
        text.split(',')
            .any(|item| item.trim().eq_ignore_ascii_case(token))
    })
}

fn parse_content_length(value: &HeaderValue) -> Option<u64> {
    // Hyper accepts a comma-separated list of identical values.
    let mut result = None;
    for item in value.to_str().ok()?.split(',') {
        let item = item.trim();
        if item.is_empty() || !item.bytes().all(|byte| byte.is_ascii_digit()) {
            return None;
        }
        let length: u64 = item.parse().ok()?;
        if result.is_some_and(|previous| previous != length) {
            return None;
        }
        result = Some(length);
    }
    result
}

/// Run one request through the application and write its response.
#[allow(clippy::too_many_arguments)]
async fn exchange(
    stream: &mut (impl AsyncRead + AsyncWrite + Unpin),
    buffer: &mut BytesMut,
    app: &crate::transport::ProtocolChecked,
    peer: SocketAddr,
    request: Request<()>,
    framing: Framing,
    expect_continue: bool,
    keep_alive: bool,
    read_limit: usize,
) -> io::Result<Next> {
    let method = request.method().clone();
    let version = request.version();
    let (mut parts, ()) = request.into_parts();
    parts.extensions.insert(ConnectInfo(peer));
    let mut body_complete = framing == Framing::Empty;
    let response = if framing == Framing::Empty {
        let request = Request::from_parts(parts, Body::empty());
        let call = app.call(request);
        tokio::pin!(call);
        watch_peer(call, stream, buffer, read_limit).await?
    } else {
        let (sender, receiver) = mpsc::channel(1);
        let (demand_sender, demand_receiver) = mpsc::channel::<()>(1);
        let size = match framing {
            Framing::Length(length) => Some(length),
            _ => None,
        };
        let body = Body::new(StreamedBody {
            receiver,
            demand: demand_sender,
            size,
            done: false,
        });
        let request = Request::from_parts(parts, body);
        let call = app.call(request);
        tokio::pin!(call);
        let early = {
            let pump = pump_body(
                stream,
                buffer,
                framing,
                sender,
                demand_receiver,
                expect_continue,
            );
            tokio::pin!(pump);
            tokio::select! {
                biased;
                response = &mut call => Some(response),
                finished = &mut pump => {
                    body_complete = finished;
                    None
                }
            }
        };
        match early {
            Some(response) => response,
            // The body was read in full: watch for the peer leaving while the
            // application works, as hyper does.
            None if body_complete => watch_peer(call, stream, buffer, read_limit).await?,
            None => call.await,
        }
    };
    let keep_alive = keep_alive && body_complete;
    write_response(stream, response, &method, version, keep_alive).await
}

/// Await the application's response while reading ahead on the connection.
///
/// Bytes of a pipelined next request are buffered (up to `limit`), and a peer
/// that closes or resets the connection cancels the application by dropping
/// its future, so abandoned requests stop work instead of running on.
async fn watch_peer<F: std::future::Future<Output = Response<Body>>>(
    mut call: Pin<&mut F>,
    stream: &mut (impl AsyncRead + AsyncWrite + Unpin),
    buffer: &mut BytesMut,
    limit: usize,
) -> io::Result<Response<Body>> {
    loop {
        if buffer.capacity() - buffer.len() < 1024 {
            buffer.reserve(READ_CAPACITY);
        }
        tokio::select! {
            biased;
            response = &mut call => return Ok(response),
            read = stream.read_buf(buffer), if buffer.len() < limit => match read {
                Ok(0) => return Err(io::ErrorKind::ConnectionAborted.into()),
                Ok(_) => {}
                Err(error) => return Err(error),
            },
        }
    }
}

/// Frame the request body from the connection into the application's body.
///
/// Reads nothing until the application asks for body data, then forwards one
/// frame per request, so request bytes are only buffered on demand (and the
/// `100 Continue` interim response is only sent when the body is wanted).
/// Returns true when the whole body was framed successfully.
async fn pump_body(
    stream: &mut (impl AsyncRead + AsyncWrite + Unpin),
    buffer: &mut BytesMut,
    framing: Framing,
    sender: mpsc::Sender<io::Result<Bytes>>,
    mut demand: mpsc::Receiver<()>,
    expect_continue: bool,
) -> bool {
    if demand.recv().await.is_none() {
        return false;
    }
    if expect_continue
        && buffer.is_empty()
        && stream
            .write_all(b"HTTP/1.1 100 Continue\r\n\r\n")
            .await
            .is_err()
    {
        return false;
    }
    let mut decoder = match framing {
        Framing::Length(length) => Decoder::Length(length),
        Framing::Chunked => Decoder::Chunked(ChunkState::Size),
        Framing::Empty => return true,
    };
    loop {
        match decoder.decode(buffer) {
            Ok(Some(data)) => {
                if sender.send(Ok(data)).await.is_err() {
                    return false;
                }
                if decoder.finished() {
                    return true;
                }
                if demand.recv().await.is_none() {
                    return false;
                }
                continue;
            }
            Ok(None) if decoder.finished() => {
                // Chunked bodies end with an empty last chunk and no data.
                drop(sender);
                return true;
            }
            Ok(None) => {}
            Err(error) => {
                let _ = sender.send(Err(error)).await;
                return false;
            }
        }
        if buffer.capacity() - buffer.len() < 1024 {
            buffer.reserve(READ_CAPACITY);
        }
        match stream.read_buf(buffer).await {
            Ok(0) => {
                let _ = sender
                    .send(Err(io::Error::new(
                        io::ErrorKind::UnexpectedEof,
                        "request body ended early",
                    )))
                    .await;
                return false;
            }
            Ok(_) => {}
            Err(error) => {
                let _ = sender.send(Err(error)).await;
                return false;
            }
        }
    }
}

enum Decoder {
    Length(u64),
    Chunked(ChunkState),
}

#[derive(Clone, Copy)]
enum ChunkState {
    Size,
    Data(u64),
    DataEnd,
    Trailers(usize),
    Done,
}

fn invalid(detail: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, detail)
}

impl Decoder {
    fn finished(&self) -> bool {
        matches!(
            self,
            Decoder::Length(0) | Decoder::Chunked(ChunkState::Done)
        )
    }

    /// Take the next body frame available in `buffer`, if any.
    fn decode(&mut self, buffer: &mut BytesMut) -> io::Result<Option<Bytes>> {
        match self {
            Decoder::Length(remaining) => {
                if *remaining == 0 || buffer.is_empty() {
                    return Ok(None);
                }
                let take = (*remaining).min(buffer.len() as u64) as usize;
                *remaining -= take as u64;
                Ok(Some(buffer.split_to(take).freeze()))
            }
            Decoder::Chunked(state) => loop {
                match *state {
                    ChunkState::Size => {
                        let Some(end) = find_line(buffer) else {
                            if buffer.len() > MAX_CHUNK_LINE {
                                return Err(invalid("chunk size line too long"));
                            }
                            return Ok(None);
                        };
                        if end > MAX_CHUNK_LINE {
                            return Err(invalid("chunk size line too long"));
                        }
                        let line = buffer.split_to(end + 1);
                        let size = parse_chunk_size(&line)?;
                        *state = if size == 0 {
                            ChunkState::Trailers(0)
                        } else {
                            ChunkState::Data(size)
                        };
                    }
                    ChunkState::Data(remaining) => {
                        if buffer.is_empty() {
                            return Ok(None);
                        }
                        let take = remaining.min(buffer.len() as u64) as usize;
                        let left = remaining - take as u64;
                        *state = if left == 0 {
                            ChunkState::DataEnd
                        } else {
                            ChunkState::Data(left)
                        };
                        return Ok(Some(buffer.split_to(take).freeze()));
                    }
                    ChunkState::DataEnd => {
                        if buffer.len() < 2 {
                            if buffer.first().is_some_and(|byte| *byte != b'\r') {
                                return Err(invalid("chunk data not followed by CRLF"));
                            }
                            return Ok(None);
                        }
                        if &buffer[..2] != b"\r\n" {
                            return Err(invalid("chunk data not followed by CRLF"));
                        }
                        buffer.advance(2);
                        *state = ChunkState::Size;
                    }
                    ChunkState::Trailers(seen) => {
                        let Some(end) = find_line(buffer) else {
                            if seen + buffer.len() > MAX_CHUNK_LINE {
                                return Err(invalid("chunked trailers too large"));
                            }
                            return Ok(None);
                        };
                        if seen + end > MAX_CHUNK_LINE {
                            return Err(invalid("chunked trailers too large"));
                        }
                        let line = buffer.split_to(end + 1);
                        // Trailer fields are read and discarded; an empty line ends them.
                        if line.as_ref() == b"\r\n" {
                            *state = ChunkState::Done;
                            return Ok(None);
                        }
                        if !line.ends_with(b"\r\n") || !line.contains(&b':') {
                            return Err(invalid("invalid chunked trailer"));
                        }
                        *state = ChunkState::Trailers(seen + line.len());
                    }
                    ChunkState::Done => return Ok(None),
                }
            },
        }
    }
}

fn find_line(buffer: &[u8]) -> Option<usize> {
    buffer.iter().position(|byte| *byte == b'\n')
}

fn parse_chunk_size(line: &[u8]) -> io::Result<u64> {
    let line = line
        .strip_suffix(b"\r\n")
        .ok_or_else(|| invalid("chunk size line must end with CRLF"))?;
    // Extensions after ';' are permitted and ignored.
    let size = line.split(|byte| *byte == b';').next().unwrap_or(b"");
    let size = size.strip_suffix(b" ").unwrap_or(size);
    if size.is_empty() || size.len() > 16 {
        return Err(invalid("invalid chunk size"));
    }
    let mut value: u64 = 0;
    for &byte in size {
        let digit = match byte {
            b'0'..=b'9' => byte - b'0',
            b'a'..=b'f' => byte - b'a' + 10,
            b'A'..=b'F' => byte - b'A' + 10,
            _ => return Err(invalid("invalid chunk size")),
        };
        value = value
            .checked_mul(16)
            .and_then(|value| value.checked_add(u64::from(digit)))
            .ok_or_else(|| invalid("chunk size overflow"))?;
    }
    Ok(value)
}

/// Request body handed to the application; frames arrive from [`pump_body`].
struct StreamedBody {
    receiver: mpsc::Receiver<io::Result<Bytes>>,
    demand: mpsc::Sender<()>,
    size: Option<u64>,
    done: bool,
}

impl http_body::Body for StreamedBody {
    type Data = Bytes;
    type Error = io::Error;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, io::Error>>> {
        if self.done {
            return Poll::Ready(None);
        }
        // Ask the connection for the next frame; at most one request is queued.
        let _ = self.demand.try_send(());
        match self.receiver.poll_recv(context) {
            Poll::Ready(Some(Ok(data))) => Poll::Ready(Some(Ok(Frame::data(data)))),
            Poll::Ready(Some(Err(error))) => {
                self.done = true;
                Poll::Ready(Some(Err(error)))
            }
            Poll::Ready(None) => {
                self.done = true;
                Poll::Ready(None)
            }
            Poll::Pending => Poll::Pending,
        }
    }

    fn size_hint(&self) -> SizeHint {
        match self.size {
            Some(size) => SizeHint::with_exact(size),
            None => SizeHint::default(),
        }
    }
}

// ---------------------------------------------------------------------------
// Responses
// ---------------------------------------------------------------------------

thread_local! {
    static DATE: RefCell<(u64, [u8; 29])> = const { RefCell::new((u64::MAX, [0; 29])) };
}

fn write_date(out: &mut Vec<u8>) {
    let now = SystemTime::now();
    let second = now
        .duration_since(SystemTime::UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_secs());
    DATE.with(|cell| {
        let mut cached = cell.borrow_mut();
        if cached.0 != second {
            let text = httpdate::fmt_http_date(now);
            cached.1.copy_from_slice(text.as_bytes());
            cached.0 = second;
        }
        out.extend_from_slice(b"date: ");
        out.extend_from_slice(&cached.1);
        out.extend_from_slice(b"\r\n");
    });
}

async fn write_error(
    stream: &mut (impl AsyncRead + AsyncWrite + Unpin),
    status: StatusCode,
) -> io::Result<()> {
    let mut out = Vec::with_capacity(128);
    write_status(&mut out, Version::HTTP_11, status);
    out.extend_from_slice(b"connection: close\r\ncontent-length: 0\r\n");
    write_date(&mut out);
    out.extend_from_slice(b"\r\n");
    stream.write_all(&out).await?;
    stream.shutdown().await
}

fn write_status(out: &mut Vec<u8>, version: Version, status: StatusCode) {
    if version == Version::HTTP_11 && status == StatusCode::OK {
        out.extend_from_slice(b"HTTP/1.1 200 OK\r\n");
        return;
    }
    out.extend_from_slice(if version == Version::HTTP_10 {
        b"HTTP/1.0 "
    } else {
        b"HTTP/1.1 "
    });
    out.extend_from_slice(status.as_str().as_bytes());
    out.push(b' ');
    out.extend_from_slice(status.canonical_reason().unwrap_or("<none>").as_bytes());
    out.extend_from_slice(b"\r\n");
}

#[derive(PartialEq, Eq)]
enum Encoding {
    None,
    Length,
    Chunked,
    CloseDelimited,
}

async fn write_response(
    stream: &mut (impl AsyncRead + AsyncWrite + Unpin),
    response: Response<Body>,
    method: &Method,
    request_version: Version,
    mut keep_alive: bool,
) -> io::Result<Next> {
    let (mut parts, body) = response.into_parts();
    if parts.status == StatusCode::SWITCHING_PROTOCOLS || parts.status.is_informational() {
        // Upgrades are served by hyper; a 1xx final response is a server error,
        // as it is in hyper.
        parts = Response::new(()).into_parts().0;
        parts.status = StatusCode::INTERNAL_SERVER_ERROR;
        keep_alive = false;
        return write_parts(
            stream,
            parts,
            Body::empty(),
            method,
            request_version,
            keep_alive,
        )
        .await;
    }
    write_parts(stream, parts, body, method, request_version, keep_alive).await
}

async fn write_parts(
    stream: &mut (impl AsyncRead + AsyncWrite + Unpin),
    mut parts: axum::http::response::Parts,
    mut body: Body,
    method: &Method,
    request_version: Version,
    mut keep_alive: bool,
) -> io::Result<Next> {
    let status = parts.status;
    let is_head = *method == Method::HEAD;
    let connect_success = *method == Method::CONNECT && status.is_success();
    let can_chunk = !(is_head
        || connect_success
        || status.is_informational()
        || matches!(status, StatusCode::NO_CONTENT | StatusCode::NOT_MODIFIED));
    let can_have_length = !(status.is_informational()
        || connect_success
        || matches!(status, StatusCode::NO_CONTENT | StatusCode::NOT_MODIFIED));
    // HTTP/1.0 peers get an HTTP/1.0 response, kept alive only on request.
    let version = if request_version == Version::HTTP_10 {
        let response_keep_alive = parts
            .headers
            .get_all(header::CONNECTION)
            .iter()
            .any(|value| has_token(value, "keep-alive"));
        if !response_keep_alive {
            if keep_alive {
                parts
                    .headers
                    .append(header::CONNECTION, HeaderValue::from_static("keep-alive"));
            } else {
                keep_alive = false;
            }
        }
        Version::HTTP_10
    } else {
        if !keep_alive {
            match parts.headers.get(header::CONNECTION) {
                Some(value) if has_token(value, "close") => {}
                Some(_) => {
                    parts
                        .headers
                        .append(header::CONNECTION, HeaderValue::from_static("close"));
                }
                None => {
                    parts
                        .headers
                        .insert(header::CONNECTION, HeaderValue::from_static("close"));
                }
            }
        }
        Version::HTTP_11
    };
    if parts
        .headers
        .get_all(header::CONNECTION)
        .iter()
        .any(|value| has_token(value, "close"))
    {
        keep_alive = false;
    }

    let hint = body.size_hint();
    let exact = hint.exact();
    let mut out = Vec::with_capacity(256 + exact.unwrap_or(0).min(64 * 1024) as usize);
    write_status(&mut out, version, status);
    let mut wrote_length = false;
    let mut wrote_date = false;
    let mut encoding = Encoding::None;
    let mut user_length: Option<u64> = None;
    let mut name: Option<HeaderName> = None;
    for (key, value) in parts.headers.drain() {
        if let Some(key) = key {
            name = Some(key);
        }
        let name = name.as_ref().expect("header name");
        match *name {
            header::CONTENT_LENGTH => {
                if exact == Some(0) && !is_head && hint.upper() == Some(0) && body.is_end_stream() {
                    // Hyper drops a declared length when the body is empty.
                    continue;
                }
                if !can_have_length || wrote_length {
                    continue;
                }
                let Some(length) = parse_content_length(&value) else {
                    return Err(invalid("invalid response content-length"));
                };
                user_length = Some(length);
                wrote_length = true;
                encoding = Encoding::Length;
            }
            header::TRANSFER_ENCODING => {
                if version == Version::HTTP_10 || !can_chunk || wrote_length {
                    continue;
                }
                wrote_length = true;
                encoding = Encoding::Chunked;
                if !is_chunked(&value) {
                    out.extend_from_slice(b"transfer-encoding: ");
                    out.extend_from_slice(value.as_bytes());
                    out.extend_from_slice(b", chunked\r\n");
                    continue;
                }
            }
            header::DATE => wrote_date = true,
            header::TRAILER => continue,
            _ => {}
        }
        out.extend_from_slice(name.as_str().as_bytes());
        out.extend_from_slice(b": ");
        out.extend_from_slice(value.as_bytes());
        out.extend_from_slice(b"\r\n");
    }
    if !wrote_length {
        encoding = match exact {
            Some(0) => {
                if can_have_length && !is_head {
                    out.extend_from_slice(b"content-length: 0\r\n");
                }
                Encoding::None
            }
            Some(length) => {
                if can_have_length {
                    out.extend_from_slice(b"content-length: ");
                    out.extend_from_slice(itoa::Buffer::new().format(length).as_bytes());
                    out.extend_from_slice(b"\r\n");
                    user_length = Some(length);
                    Encoding::Length
                } else {
                    Encoding::None
                }
            }
            None => {
                if version == Version::HTTP_10 || !can_chunk {
                    if can_chunk {
                        keep_alive = false;
                        Encoding::CloseDelimited
                    } else {
                        Encoding::None
                    }
                } else {
                    out.extend_from_slice(b"transfer-encoding: chunked\r\n");
                    Encoding::Chunked
                }
            }
        };
    }
    if !wrote_date {
        write_date(&mut out);
    }
    out.extend_from_slice(b"\r\n");

    let send_body = !is_head && (can_chunk || encoding == Encoding::Length);
    if !send_body || encoding == Encoding::None {
        stream.write_all(&out).await?;
        return Ok(if keep_alive {
            Next::KeepAlive
        } else {
            Next::Close
        });
    }
    let mut remaining = user_length;
    if exact.is_some_and(|size| size <= GATHER_LIMIT) {
        // Small exact-length body: gather every frame, then write head and body at once.
        // Frames stay alive until the write completes, because their owners
        // can hold byte-budget admission for the response.
        let mut frames = Vec::new();
        while let Some(frame) = body.frame().await {
            let frame = frame.map_err(|_| invalid("response body failed"))?;
            if let Ok(data) = frame.into_data()
                && !data.is_empty()
            {
                frames.push(data);
            }
        }
        let size: usize = frames.iter().map(Bytes::len).sum();
        if encoding == Encoding::Length && frames.len() == 1 && size > INLINE_BODY {
            // Large single-frame bodies go out with a vectored write, uncopied.
            let left = remaining.as_mut().expect("length encoding has a length");
            if size as u64 > *left {
                return Err(invalid("response body longer than its content-length"));
            }
            *left -= size as u64;
            finish_body(&mut out, &encoding, remaining)?;
            let mut chained = Buf::chain(&out[..], &frames[0][..]);
            stream.write_all_buf(&mut chained).await?;
        } else {
            for data in &frames {
                append_body(&mut out, data, &encoding, &mut remaining)?;
            }
            finish_body(&mut out, &encoding, remaining)?;
            stream.write_all(&out).await?;
        }
        drop(frames);
    } else {
        // Streaming or large body: send the head now and each frame as it arrives.
        stream.write_all(&out).await?;
        out.clear();
        while let Some(frame) = body.frame().await {
            let frame = match frame {
                Ok(frame) => frame,
                // Hyper aborts the connection when a response body fails.
                Err(_) => return Ok(Next::Close),
            };
            if let Ok(data) = frame.into_data() {
                if data.is_empty() {
                    continue;
                }
                if encoding == Encoding::Length {
                    // Length framing adds nothing around the data: write the
                    // frame itself rather than copying it.
                    let left = remaining.as_mut().expect("length encoding has a length");
                    if data.len() as u64 > *left {
                        return Err(invalid("response body longer than its content-length"));
                    }
                    *left -= data.len() as u64;
                    stream.write_all(&data).await?;
                    continue;
                }
                append_body(&mut out, &data, &encoding, &mut remaining)?;
                stream.write_all(&out).await?;
                out.clear();
            }
        }
        finish_body(&mut out, &encoding, remaining)?;
        if !out.is_empty() {
            stream.write_all(&out).await?;
        }
    }
    Ok(if keep_alive && encoding != Encoding::CloseDelimited {
        Next::KeepAlive
    } else {
        Next::Close
    })
}

fn append_body(
    out: &mut Vec<u8>,
    data: &[u8],
    encoding: &Encoding,
    remaining: &mut Option<u64>,
) -> io::Result<()> {
    match encoding {
        Encoding::Length => {
            let left = remaining.as_mut().expect("length encoding has a length");
            if data.len() as u64 > *left {
                return Err(invalid("response body longer than its content-length"));
            }
            *left -= data.len() as u64;
            out.extend_from_slice(data);
        }
        Encoding::Chunked => {
            if !data.is_empty() {
                write_hex(out, data.len());
                out.extend_from_slice(b"\r\n");
                out.extend_from_slice(data);
                out.extend_from_slice(b"\r\n");
            }
        }
        Encoding::CloseDelimited => out.extend_from_slice(data),
        Encoding::None => {}
    }
    Ok(())
}

fn finish_body(out: &mut Vec<u8>, encoding: &Encoding, remaining: Option<u64>) -> io::Result<()> {
    match encoding {
        Encoding::Length if remaining != Some(0) => {
            Err(invalid("response body shorter than its content-length"))
        }
        Encoding::Chunked => {
            out.extend_from_slice(b"0\r\n\r\n");
            Ok(())
        }
        _ => Ok(()),
    }
}

fn write_hex(out: &mut Vec<u8>, mut value: usize) {
    let mut digits = [0u8; 16];
    let mut index = digits.len();
    loop {
        index -= 1;
        digits[index] = b"0123456789abcdef"[value & 0xF];
        value >>= 4;
        if value == 0 {
            break;
        }
    }
    out.extend_from_slice(&digits[index..]);
}

// ---------------------------------------------------------------------------
// Upgrade hand-off
// ---------------------------------------------------------------------------

/// A connection with already-read bytes replayed before the socket's own.
pub(crate) struct Rewind<S> {
    prefix: Bytes,
    stream: S,
}

impl<S> Rewind<S> {
    pub(crate) fn new(prefix: Bytes, stream: S) -> Self {
        Self { prefix, stream }
    }
}

impl<S: AsyncRead + Unpin> AsyncRead for Rewind<S> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        if !self.prefix.is_empty() {
            let take = self.prefix.len().min(buffer.remaining());
            buffer.put_slice(&self.prefix[..take]);
            self.prefix.advance(take);
            return Poll::Ready(Ok(()));
        }
        Pin::new(&mut self.stream).poll_read(context, buffer)
    }
}

impl<S: AsyncWrite + Unpin> AsyncWrite for Rewind<S> {
    fn poll_write(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        data: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.stream).poll_write(context, data)
    }

    fn poll_flush(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stream).poll_flush(context)
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stream).poll_shutdown(context)
    }

    fn poll_write_vectored(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffers: &[io::IoSlice<'_>],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.stream).poll_write_vectored(context, buffers)
    }

    fn is_write_vectored(&self) -> bool {
        self.stream.is_write_vectored()
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn request_lines_naming_no_http_version_are_recognised() {
        use super::names_http_version;
        assert!(names_http_version(b"GET / HTTP/1.1\r\n"));
        assert!(names_http_version(b"GET / HTTP/9\r\n"));
        assert!(names_http_version(b"GET /\r\n"));
        assert!(names_http_version(b"\x01\x02"));
        assert!(!names_http_version(b"INVALID CONNECTION PREFACE\r\n\r\n"));
        assert!(!names_http_version(b"SSH-2.0 x y\r\n"));
    }

    use super::*;
    use axum::{Router, routing::get, routing::post};
    use std::collections::VecDeque;

    /// A body of unknown length, so the response must be chunked.
    struct Pieces(VecDeque<&'static str>);

    impl http_body::Body for Pieces {
        type Data = Bytes;
        type Error = std::convert::Infallible;

        fn poll_frame(
            mut self: Pin<&mut Self>,
            _: &mut Context<'_>,
        ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
            Poll::Ready(
                self.0
                    .pop_front()
                    .map(|piece| Ok(Frame::data(Bytes::from(piece)))),
            )
        }
    }

    fn app() -> crate::transport::ProtocolChecked {
        let app = Router::new()
            .route("/", get(|| async { "hello" }))
            .route("/echo", post(|body: Bytes| async move { body }))
            .route(
                "/stream",
                get(|| async { Body::new(Pieces(VecDeque::from(["ab", "cde"]))) }),
            );
        crate::transport::ProtocolChecked {
            app: crate::transport::Application::Router(app),
            target_limit: 8192,
            tls: None,
        }
    }

    fn config() -> Config {
        Config {
            header_timeout: Duration::from_secs(5),
            max_headers: 100,
            max_header_size: 1024,
        }
    }

    /// Send `input`, close the client's write side and return everything the
    /// server wrote, plus what the upgrade hand-off received (if it ran).
    async fn exchange_bytes(input: &[u8]) -> (String, Option<Vec<u8>>) {
        let (mut client, server) = tokio::io::duplex(1 << 16);
        let (_stop, stop) = watch::channel(false);
        let peer = SocketAddr::from(([127, 0, 0, 1], 5000));
        let upgraded = std::sync::Arc::new(std::sync::Mutex::new(None));
        let handed = upgraded.clone();
        let task = tokio::spawn(async move {
            serve(
                server,
                peer,
                app(),
                config(),
                stop,
                || {},
                async move |mut io, _| {
                    let mut seen = Vec::new();
                    let _ = io.read_to_end(&mut seen).await;
                    *handed.lock().unwrap() = Some(seen);
                },
            )
            .await;
        });
        client.write_all(input).await.unwrap();
        client.shutdown().await.unwrap();
        let mut output = Vec::new();
        client.read_to_end(&mut output).await.unwrap();
        task.await.unwrap();
        let upgraded = upgraded.lock().unwrap().take();
        (String::from_utf8(output).unwrap(), upgraded)
    }

    async fn respond(input: &str) -> String {
        exchange_bytes(input.as_bytes()).await.0
    }

    /// An exact-length body that yields one frame and then never finishes,
    /// like a file whose next read is still in flight.
    struct Stalled(Option<Bytes>, u64);

    impl http_body::Body for Stalled {
        type Data = Bytes;
        type Error = std::convert::Infallible;

        fn poll_frame(
            mut self: Pin<&mut Self>,
            _: &mut Context<'_>,
        ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
            match self.0.take() {
                Some(data) => Poll::Ready(Some(Ok(Frame::data(data)))),
                None => Poll::Pending,
            }
        }

        fn size_hint(&self) -> SizeHint {
            SizeHint::with_exact(self.1)
        }
    }

    #[tokio::test]
    async fn large_exact_length_bodies_are_written_as_frames_arrive() {
        let (mut client, mut server) = tokio::io::duplex(1 << 20);
        let first = Bytes::from(vec![b'x'; 64 * 1024]);
        let body = Body::new(Stalled(Some(first.clone()), 4 * 1024 * 1024));
        let writer = tokio::spawn(async move {
            let _ = write_response(
                &mut server,
                Response::new(body),
                &Method::GET,
                Version::HTTP_11,
                true,
            )
            .await;
        });
        let mut received = Vec::new();
        let mut buffer = vec![0; 1 << 16];
        let read = async {
            while !received.ends_with(&first[..]) {
                let read = client.read(&mut buffer).await.unwrap();
                assert!(read > 0);
                received.extend_from_slice(&buffer[..read]);
            }
        };
        tokio::time::timeout(Duration::from_secs(5), read)
            .await
            .expect("the first frame arrives before the body completes");
        let text = String::from_utf8_lossy(&received);
        assert!(text.contains("content-length: 4194304\r\n"), "{text}");
        writer.abort();
    }

    #[tokio::test]
    async fn pipelined_requests_are_answered_in_order_on_one_connection() {
        let output = respond(
            "GET / HTTP/1.1\r\nhost: a\r\n\r\nPOST /echo HTTP/1.1\r\nhost: a\r\ncontent-length: 3\r\n\r\nxyz",
        )
        .await;
        let first = output.find("hello").unwrap();
        let second = output.find("\r\n\r\nxyz").unwrap();
        assert!(first < second);
        assert_eq!(output.matches("HTTP/1.1 200 OK\r\n").count(), 2);
        assert!(!output.contains("connection: close"));
    }

    #[tokio::test]
    async fn chunked_request_bodies_are_decoded() {
        let output = respond(
            "POST /echo HTTP/1.1\r\nhost: a\r\ntransfer-encoding: chunked\r\n\r\n3;ext=1\r\nabc\r\n2\r\nde\r\n0\r\ntrailer: x\r\n\r\n",
        )
        .await;
        assert!(output.starts_with("HTTP/1.1 200 OK\r\n"), "{output}");
        assert!(output.contains("content-length: 5\r\n"));
        assert!(output.ends_with("\r\n\r\nabcde"));
    }

    #[tokio::test]
    async fn bodies_of_unknown_length_are_sent_chunked() {
        let output = respond("GET /stream HTTP/1.1\r\nhost: a\r\n\r\n").await;
        assert!(
            output.contains("transfer-encoding: chunked\r\n"),
            "{output}"
        );
        assert!(output.ends_with("\r\n\r\n2\r\nab\r\n3\r\ncde\r\n0\r\n\r\n"));
        // HTTP/1.0 cannot chunk, so the body is delimited by closing instead.
        let output = respond("GET /stream HTTP/1.0\r\n\r\n").await;
        assert!(!output.contains("transfer-encoding"), "{output}");
        assert!(output.ends_with("\r\n\r\nabcde"));
    }

    #[tokio::test]
    async fn close_and_http10_requests_end_the_connection() {
        let output = respond(
            "GET / HTTP/1.1\r\nhost: a\r\nconnection: close\r\n\r\nGET / HTTP/1.1\r\nhost: a\r\n\r\n",
        )
        .await;
        assert_eq!(output.matches("HTTP/1.1 200 OK").count(), 1);
        assert!(output.contains("connection: close\r\n"));
        let output = respond("GET / HTTP/1.0\r\n\r\nGET / HTTP/1.0\r\n\r\n").await;
        assert_eq!(output.matches("200 OK").count(), 1);
        let output =
            respond("GET / HTTP/1.0\r\nconnection: keep-alive\r\n\r\nGET / HTTP/1.0\r\n\r\n").await;
        assert_eq!(output.matches("200 OK").count(), 2);
        assert!(output.contains("connection: keep-alive\r\n"));
    }

    #[tokio::test]
    async fn head_responses_keep_the_length_and_drop_the_body() {
        let output = respond("HEAD / HTTP/1.1\r\nhost: a\r\n\r\n").await;
        assert!(output.contains("content-length: 5\r\n"), "{output}");
        assert!(output.ends_with("\r\n\r\n"));
    }

    #[tokio::test]
    async fn expect_continue_is_answered_only_when_the_body_is_awaited() {
        // The body already arrived with the head, so no interim response is due.
        let output = respond(
            "POST /echo HTTP/1.1\r\nhost: a\r\ncontent-length: 2\r\nexpect: 100-continue\r\n\r\nok",
        )
        .await;
        assert!(output.starts_with("HTTP/1.1 200 OK"), "{output}");
        // A client that waits for permission gets it before sending the body.
        let (mut client, server) = tokio::io::duplex(1 << 16);
        let (_stop, stop) = watch::channel(false);
        let peer = SocketAddr::from(([127, 0, 0, 1], 5000));
        tokio::spawn(serve(
            server,
            peer,
            app(),
            config(),
            stop,
            || {},
            async |_, _| {},
        ));
        client
            .write_all(b"POST /echo HTTP/1.1\r\nhost: a\r\ncontent-length: 2\r\nexpect: 100-continue\r\n\r\n")
            .await
            .unwrap();
        let interim = b"HTTP/1.1 100 Continue\r\n\r\n";
        let mut received = vec![0; interim.len()];
        client.read_exact(&mut received).await.unwrap();
        assert_eq!(received, interim);
        client.write_all(b"ok").await.unwrap();
        client.shutdown().await.unwrap();
        let mut output = Vec::new();
        client.read_to_end(&mut output).await.unwrap();
        assert!(output.starts_with(b"HTTP/1.1 200 OK") && output.ends_with(b"\r\n\r\nok"));
    }

    #[tokio::test]
    async fn malformed_and_oversized_heads_are_rejected_and_closed() {
        let output = respond("GET / HTTP/1.1\r\nbad header\r\n\r\nGET / HTTP/1.1\r\n\r\n").await;
        assert!(
            output.starts_with("HTTP/1.1 400 Bad Request\r\n"),
            "{output}"
        );
        assert_eq!(output.matches("HTTP/1.1").count(), 1);
        let long = format!("GET / HTTP/1.1\r\nx: {}\r\n\r\n", "a".repeat(2048));
        let output = respond(&long).await;
        assert!(output.starts_with("HTTP/1.1 431 "), "{output}");
        let output = respond("GET / HTTP/1.1\r\nhost: a").await;
        assert!(output.starts_with("HTTP/1.1 400 "), "{output}");
    }

    #[tokio::test]
    async fn ambiguous_framing_is_rejected() {
        for input in [
            "POST /echo HTTP/1.1\r\nhost: a\r\ncontent-length: 1\r\ncontent-length: 2\r\n\r\nab",
            "POST /echo HTTP/1.1\r\nhost: a\r\ncontent-length: +1\r\n\r\na",
            "POST /echo HTTP/1.1\r\nhost: a\r\ntransfer-encoding: gzip\r\n\r\n",
            "POST /echo HTTP/1.1\r\nhost: a\r\ntransfer-encoding: chunked\r\ncontent-length: 3\r\n\r\n0\r\n\r\n",
            "POST /echo HTTP/1.1\r\nhost: a\r\ntransfer-encoding: chunked\r\n\r\nffffffffffffffffff\r\n",
        ] {
            let output = respond(input).await;
            assert!(output.starts_with("HTTP/1.1 400 "), "{input:?} -> {output}");
            assert_eq!(output.matches("HTTP/1.1").count(), 1, "{input:?}");
        }
    }

    #[tokio::test]
    async fn endless_blank_lines_cannot_outrun_the_header_deadline() {
        let (mut client, server) = tokio::io::duplex(1 << 16);
        let (_stop, stop) = watch::channel(false);
        let peer = SocketAddr::from(([127, 0, 0, 1], 5000));
        let config = Config {
            header_timeout: Duration::from_millis(50),
            ..config()
        };
        let served = tokio::spawn(serve(
            server,
            peer,
            app(),
            config,
            stop,
            || {},
            async |_, _| {},
        ));
        let flood = tokio::spawn(async move {
            while client.write_all(b"\r\n\r\n\r\n\r\n").await.is_ok() {
                tokio::task::yield_now().await;
            }
        });
        tokio::time::timeout(Duration::from_secs(2), served)
            .await
            .expect("the deadline closed the connection")
            .unwrap();
        flood.await.unwrap();
    }

    #[tokio::test]
    async fn upgrade_requests_are_replayed_in_full() {
        let input = b"GET /ws HTTP/1.1\r\nhost: a\r\nconnection: upgrade\r\nupgrade: websocket\r\n\r\nafter";
        let (output, upgraded) = exchange_bytes(input).await;
        assert!(output.is_empty());
        assert_eq!(upgraded.unwrap(), input);
    }
}
