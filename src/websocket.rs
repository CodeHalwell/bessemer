//! Native WebSocket (RFC 6455) handshake, framing and session management.
//!
//! The frame codec is implemented here: masking, fragmentation, UTF-8
//! validation, ping/pong and the close handshake never reach Python. Python
//! sees whole messages, delivered in batches through one loop wake-up per
//! batch, and sends through a queue drained by a writer that coalesces queued
//! frames into one socket write. Inbound flow is credit based (Python acks
//! each consumed message) and outbound flow uses high/low byte watermarks.

use axum::{
    body::Body,
    extract::Request,
    http::{HeaderMap, HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
};
use hyper_util::rt::TokioIo;
use pyo3::{
    exceptions::PyRuntimeError,
    prelude::*,
    pybacked::PyBackedBytes,
    types::{PyBytes, PyCFunction, PyDict, PyList, PyString, PyTuple},
};
use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWriteExt, BufReader},
    sync::{Notify, Semaphore, mpsc, oneshot, watch},
};

const GUID: &[u8] = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
/// Messages delivered to Python but not yet consumed by the handler.
const INBOUND_CREDIT: usize = 64;
/// Outbound bytes queued before `send` starts waiting for the writer.
const HIGH_WATER: usize = 1 << 20;
const LOW_WATER: usize = 256 * 1024;

const TEXT: u8 = 0x1;
const BINARY: u8 = 0x2;
const CLOSE: u8 = 0x8;
const PING: u8 = 0x9;
const PONG: u8 = 0xA;
/// How long the peer has to answer our close frame, and the writer to flush it.
const CLOSE_WAIT: Duration = Duration::from_secs(5);

/// Per-connection context inserted by the transport so an upgraded session keeps
/// its connection slot and observes server shutdown.
#[derive(Clone)]
pub(crate) struct ConnectionContext {
    pub(crate) _slot: Arc<tokio::sync::OwnedSemaphorePermit>,
    pub(crate) stop: watch::Receiver<bool>,
}

fn base64(input: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let word = (chunk[0] as u32) << 16
            | (*chunk.get(1).unwrap_or(&0) as u32) << 8
            | *chunk.get(2).unwrap_or(&0) as u32;
        for index in 0..4 {
            if index <= chunk.len() {
                output.push(ALPHABET[(word >> (18 - 6 * index) & 63) as usize] as char);
            } else {
                output.push('=');
            }
        }
    }
    output
}

pub(crate) fn accept_key(key: &[u8]) -> String {
    let mut context = ring::digest::Context::new(&ring::digest::SHA1_FOR_LEGACY_USE_ONLY);
    context.update(key);
    context.update(GUID);
    base64(context.finish().as_ref())
}

fn token_list_contains(headers: &HeaderMap, name: header::HeaderName, token: &str) -> bool {
    headers.get_all(name).iter().any(|value| {
        value.to_str().is_ok_and(|value| {
            value
                .split(',')
                .any(|item| item.trim().eq_ignore_ascii_case(token))
        })
    })
}

/// Validate the client handshake and return the `Sec-WebSocket-Key`.
pub(crate) fn handshake_key(request: &Request) -> Result<HeaderValue, &'static str> {
    if request.method() != axum::http::Method::GET {
        return Err("WebSocket handshakes must use GET");
    }
    let headers = request.headers();
    if !token_list_contains(headers, header::UPGRADE, "websocket")
        || !token_list_contains(headers, header::CONNECTION, "upgrade")
    {
        return Err("Expected a WebSocket upgrade request");
    }
    if headers
        .get(header::SEC_WEBSOCKET_VERSION)
        .is_none_or(|value| value != "13")
    {
        return Err("Unsupported WebSocket version");
    }
    let mut keys = headers.get_all(header::SEC_WEBSOCKET_KEY).iter();
    let key = keys.next().ok_or("Missing Sec-WebSocket-Key")?;
    if keys.next().is_some()
        || crate::jwt::base64url(
            &key.as_bytes()
                .iter()
                .map(|byte| match byte {
                    b'+' => b'-',
                    b'/' => b'_',
                    other => *other,
                })
                .filter(|byte| *byte != b'=')
                .collect::<Vec<_>>(),
        )
        .is_none_or(|value| value.len() != 16)
    {
        return Err("Invalid Sec-WebSocket-Key");
    }
    Ok(key.clone())
}

enum Outbound {
    Message(u8, PyBackedBytes),
    Control(u8, Vec<u8>),
}

struct Shared {
    outbound: mpsc::UnboundedSender<Outbound>,
    queued: AtomicUsize,
    closed: AtomicBool,
    close_sent: AtomicBool,
    credit: Semaphore,
    deliver: Mutex<Option<Py<PyAny>>>,
    drain_waiter: Mutex<Option<Py<PyAny>>>,
    /// Signalled when the application starts the close handshake.
    close_started: Notify,
}

impl Shared {
    /// Drop the Python callbacks. The delivery callback references the Python
    /// `WebSocket`, which owns the channel and so this `Shared`: clearing it
    /// breaks that cycle once the session is over.
    fn release_callbacks(&self) {
        // Wake a send blocked on backpressure; it then sees the closed channel.
        notify_drain(self);
        let deliver = self.deliver.lock().unwrap().take();
        let drain = self.drain_waiter.lock().unwrap().take();
        if deliver.is_some() || drain.is_some() {
            Python::attach(|_| {
                drop(deliver);
                drop(drain);
            });
        }
    }
}

/// The native side of one WebSocket, owned by the Python `WebSocket` object.
#[pyclass(module = "bessemer._native", name = "WebSocketChannel", frozen)]
pub(crate) struct Channel {
    shared: Arc<Shared>,
    accept: Mutex<Option<oneshot::Sender<Option<String>>>>,
}

#[pymethods]
impl Channel {
    /// Complete the handshake. `deliver(batch)` must be thread-safe; it receives
    /// lists of `(kind, payload)` with kind 1 text, 2 bytes and 8 close.
    #[pyo3(signature = (deliver, subprotocol=None))]
    fn accept(&self, deliver: Py<PyAny>, subprotocol: Option<String>) -> PyResult<()> {
        if let Some(value) = &subprotocol
            && (value.is_empty()
                || !value
                    .bytes()
                    .all(|byte| byte.is_ascii_graphic() && !b"()<>@,;:\\\"/[]?={}".contains(&byte)))
        {
            return Err(PyRuntimeError::new_err("invalid WebSocket subprotocol"));
        }
        let sender = self
            .accept
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("WebSocket already accepted or closed"))?;
        *self.shared.deliver.lock().unwrap() = Some(deliver);
        sender.send(subprotocol).map_err(|_| {
            self.shared.release_callbacks();
            PyRuntimeError::new_err("WebSocket handshake was abandoned")
        })
    }

    /// Queue a text (1) or binary (2) message. Returns true when the caller
    /// should wait for `on_drain` before sending more.
    fn send(&self, opcode: u8, data: PyBackedBytes) -> PyResult<bool> {
        if opcode != TEXT && opcode != BINARY {
            return Err(PyRuntimeError::new_err("invalid WebSocket opcode"));
        }
        if self.shared.closed.load(Ordering::Acquire)
            || self.shared.close_sent.load(Ordering::Acquire)
        {
            return Err(PyRuntimeError::new_err("WebSocket is closed"));
        }
        let length = data.len();
        let queued = self.shared.queued.fetch_add(length, Ordering::AcqRel) + length;
        self.shared
            .outbound
            .send(Outbound::Message(opcode, data))
            .map_err(|_| PyRuntimeError::new_err("WebSocket is closed"))?;
        Ok(queued > HIGH_WATER)
    }

    /// Start the close handshake. Idempotent.
    #[pyo3(signature = (code=1000, reason=""))]
    fn close(&self, code: u16, reason: &str) -> PyResult<()> {
        if !valid_close_code(code) || reason.len() > 123 {
            return Err(PyRuntimeError::new_err(
                "invalid WebSocket close code or reason",
            ));
        }
        if self.accept.lock().unwrap().take().is_some() {
            // Closing before accept denies the handshake.
            return Ok(());
        }
        if !self.shared.close_sent.swap(true, Ordering::AcqRel) {
            let mut payload = code.to_be_bytes().to_vec();
            payload.extend_from_slice(reason.as_bytes());
            let _ = self.shared.outbound.send(Outbound::Control(CLOSE, payload));
            self.shared.close_started.notify_one();
        }
        Ok(())
    }

    /// Return consumed-message credit to the reader.
    fn ack(&self, count: usize) {
        self.shared.credit.add_permits(count.min(INBOUND_CREDIT));
    }

    fn queued(&self) -> usize {
        self.shared.queued.load(Ordering::Acquire)
    }

    /// Register a thread-safe callable invoked once queued bytes fall below the
    /// low watermark or the connection closes.
    fn on_drain(&self, callback: Py<PyAny>) {
        *self.shared.drain_waiter.lock().unwrap() = Some(callback);
        if self.shared.queued.load(Ordering::Acquire) < LOW_WATER
            || self.shared.closed.load(Ordering::Acquire)
        {
            notify_drain(&self.shared);
        }
    }
}

fn notify_drain(shared: &Shared) {
    if let Some(callback) = shared.drain_waiter.lock().unwrap().take() {
        Python::attach(|py| {
            let _ = callback.bind(py).call0();
        });
    }
}

fn valid_close_code(code: u16) -> bool {
    matches!(code, 1000..=1003 | 1007..=1014 | 3000..=4999)
}

enum Inbound {
    Text(String),
    Binary(Vec<u8>),
    Close(u16),
}

fn deliver(shared: &Shared, batch: Vec<Inbound>) -> bool {
    let Some(callback) = Python::attach(|py| {
        shared
            .deliver
            .lock()
            .unwrap()
            .as_ref()
            .map(|value| value.clone_ref(py))
    }) else {
        return false;
    };
    Python::attach(|py| {
        let items = batch
            .into_iter()
            .map(|item| -> PyResult<Bound<'_, PyTuple>> {
                match item {
                    Inbound::Text(text) => (TEXT, PyString::new(py, &text)).into_pyobject(py),
                    Inbound::Binary(data) => (BINARY, PyBytes::new(py, &data)).into_pyobject(py),
                    Inbound::Close(code) => (CLOSE, code).into_pyobject(py),
                }
            });
        let list = PyList::empty(py);
        for item in items {
            list.append(item?)?;
        }
        callback.bind(py).call1((list,))?;
        Ok::<_, PyErr>(())
    })
    .is_ok()
}

#[derive(Debug)]
enum Failure {
    /// Close with this status code.
    Protocol(u16),
    /// The transport ended without a close frame.
    Gone,
}

fn unmask(data: &mut [u8], mask: [u8; 4]) {
    let word = u64::from_ne_bytes([
        mask[0], mask[1], mask[2], mask[3], mask[0], mask[1], mask[2], mask[3],
    ]);
    let mut chunks = data.chunks_exact_mut(8);
    for chunk in &mut chunks {
        let value = u64::from_ne_bytes(chunk.try_into().unwrap()) ^ word;
        chunk.copy_from_slice(&value.to_ne_bytes());
    }
    for (index, byte) in chunks.into_remainder().iter_mut().enumerate() {
        *byte ^= mask[index & 3];
    }
}

struct Frame {
    fin: bool,
    opcode: u8,
    payload: Vec<u8>,
}

/// Whether `buffer` already holds a whole frame, so reading it cannot block.
fn frame_buffered(buffer: &[u8]) -> bool {
    let [_, second, rest @ ..] = buffer else {
        return false;
    };
    let (length, rest) = match second & 0x7F {
        126 if rest.len() >= 2 => (u16::from_be_bytes([rest[0], rest[1]]) as u64, &rest[2..]),
        127 if rest.len() >= 8 => (
            u64::from_be_bytes(rest[..8].try_into().unwrap()),
            &rest[8..],
        ),
        126 | 127 => return false,
        length => (u64::from(length), rest),
    };
    let mask = if second & 0x80 != 0 { 4 } else { 0 };
    (rest.len() as u64) >= mask + length
}

async fn read_frame<R: AsyncRead + Unpin>(
    reader: &mut R,
    max_payload: usize,
) -> Result<Frame, Failure> {
    let mut head = [0u8; 2];
    reader
        .read_exact(&mut head)
        .await
        .map_err(|_| Failure::Gone)?;
    let fin = head[0] & 0x80 != 0;
    if head[0] & 0x70 != 0 {
        return Err(Failure::Protocol(1002));
    }
    let opcode = head[0] & 0x0f;
    if head[1] & 0x80 == 0 {
        // Client frames must be masked.
        return Err(Failure::Protocol(1002));
    }
    let mut length = (head[1] & 0x7f) as u64;
    if length == 126 {
        let mut extended = [0u8; 2];
        reader
            .read_exact(&mut extended)
            .await
            .map_err(|_| Failure::Gone)?;
        length = u16::from_be_bytes(extended) as u64;
    } else if length == 127 {
        let mut extended = [0u8; 8];
        reader
            .read_exact(&mut extended)
            .await
            .map_err(|_| Failure::Gone)?;
        length = u64::from_be_bytes(extended);
        if length >> 63 != 0 {
            return Err(Failure::Protocol(1002));
        }
    }
    if opcode & 0x8 != 0 && (length > 125 || !fin) {
        return Err(Failure::Protocol(1002));
    }
    if length > max_payload as u64 {
        return Err(Failure::Protocol(1009));
    }
    let mut mask = [0u8; 4];
    reader
        .read_exact(&mut mask)
        .await
        .map_err(|_| Failure::Gone)?;
    let mut payload = vec![0u8; length as usize];
    reader
        .read_exact(&mut payload)
        .await
        .map_err(|_| Failure::Gone)?;
    unmask(&mut payload, mask);
    Ok(Frame {
        fin,
        opcode,
        payload,
    })
}

fn encode_frame(output: &mut Vec<u8>, opcode: u8, payload: &[u8]) {
    output.push(0x80 | opcode);
    let length = payload.len();
    if length < 126 {
        output.push(length as u8);
    } else if length <= u16::MAX as usize {
        output.push(126);
        output.extend_from_slice(&(length as u16).to_be_bytes());
    } else {
        output.push(127);
        output.extend_from_slice(&(length as u64).to_be_bytes());
    }
    output.extend_from_slice(payload);
}

/// Read messages and deliver them in batches until close or failure.
async fn read_loop<R: AsyncRead + Unpin>(
    mut reader: BufReader<R>,
    shared: Arc<Shared>,
    max_message: usize,
) -> Option<u16> {
    let mut batch = Vec::new();
    let mut partial: Option<(u8, Vec<u8>)> = None;
    let outcome = loop {
        // Flush what we have before blocking on the socket or on credit.
        if !batch.is_empty()
            && (!frame_buffered(reader.buffer()) || shared.credit.available_permits() == 0)
            && !deliver(&shared, std::mem::take(&mut batch))
        {
            break None;
        }
        let Ok(permit) = shared.credit.acquire().await else {
            break None;
        };
        let frame = match read_frame(&mut reader, max_message).await {
            Ok(frame) => frame,
            Err(Failure::Gone) => break Some(1006),
            Err(Failure::Protocol(code)) => {
                send_close(&shared, code);
                break Some(code);
            }
        };
        match frame.opcode {
            PING => {
                let _ = shared.outbound.send(Outbound::Control(PONG, frame.payload));
                continue;
            }
            PONG => continue,
            CLOSE => {
                let code = match frame.payload.len() {
                    0 => 1005,
                    1 => {
                        send_close(&shared, 1002);
                        break Some(1002);
                    }
                    _ => {
                        let code = u16::from_be_bytes([frame.payload[0], frame.payload[1]]);
                        if !valid_close_code(code)
                            || std::str::from_utf8(&frame.payload[2..]).is_err()
                        {
                            send_close(&shared, 1002);
                            break Some(1002);
                        }
                        code
                    }
                };
                // Echo the close (with the peer's code) unless one was already sent.
                send_close(&shared, if code == 1005 { 1000 } else { code });
                break Some(code);
            }
            0 => {
                let Some((_, buffer)) = partial.as_mut() else {
                    send_close(&shared, 1002);
                    break Some(1002);
                };
                if buffer.len() + frame.payload.len() > max_message {
                    send_close(&shared, 1009);
                    break Some(1009);
                }
                buffer.extend_from_slice(&frame.payload);
            }
            TEXT | BINARY => {
                if partial.is_some() {
                    send_close(&shared, 1002);
                    break Some(1002);
                }
                partial = Some((frame.opcode, frame.payload));
            }
            _ => {
                send_close(&shared, 1002);
                break Some(1002);
            }
        }
        if !frame.fin {
            continue;
        }
        let (opcode, payload) = partial.take().unwrap();
        let message = if opcode == TEXT {
            match String::from_utf8(payload) {
                Ok(text) => Inbound::Text(text),
                Err(_) => {
                    send_close(&shared, 1007);
                    break Some(1007);
                }
            }
        } else {
            Inbound::Binary(payload)
        };
        // The permit is returned by the Python handler's ack.
        permit.forget();
        batch.push(message);
    };
    if !batch.is_empty() {
        deliver(&shared, batch);
    }
    outcome
}

fn send_close(shared: &Shared, code: u16) {
    if !shared.close_sent.swap(true, Ordering::AcqRel) {
        let _ = shared
            .outbound
            .send(Outbound::Control(CLOSE, code.to_be_bytes().to_vec()));
    }
}

/// Write queued frames, coalescing everything already queued into one write.
async fn write_loop<W: tokio::io::AsyncWrite + Unpin>(
    mut writer: W,
    mut receiver: mpsc::UnboundedReceiver<Outbound>,
    shared: Arc<Shared>,
) {
    let mut output = Vec::with_capacity(16 * 1024);
    while let Some(first) = receiver.recv().await {
        let mut released = 0usize;
        let mut closing = false;
        let mut item = Some(first);
        while let Some(next) = item.take() {
            match next {
                Outbound::Message(opcode, data) => {
                    encode_frame(&mut output, opcode, &data);
                    released += data.len();
                }
                Outbound::Control(opcode, payload) => {
                    encode_frame(&mut output, opcode, &payload);
                    closing |= opcode == CLOSE;
                }
            }
            if closing || output.len() >= 256 * 1024 {
                break;
            }
            item = receiver.try_recv().ok();
        }
        let failed = writer.write_all(&output).await.is_err() || writer.flush().await.is_err();
        output.clear();
        if output.capacity() > 1 << 20 {
            output.shrink_to(16 * 1024);
        }
        let remaining = shared.queued.fetch_sub(released, Ordering::AcqRel) - released;
        if remaining < LOW_WATER {
            notify_drain(&shared);
        }
        if failed || closing {
            break;
        }
    }
    let _ = writer.shutdown().await;
}

/// Run an accepted session over an upgraded connection.
async fn session(
    upgrade: hyper::upgrade::OnUpgrade,
    shared: Arc<Shared>,
    receiver: mpsc::UnboundedReceiver<Outbound>,
    mut finished: oneshot::Receiver<()>,
    context: Option<ConnectionContext>,
    max_message: usize,
) {
    let Ok(upgraded) = upgrade.await else {
        shared.closed.store(true, Ordering::Release);
        deliver(&shared, vec![Inbound::Close(1006)]);
        shared.release_callbacks();
        return;
    };
    let (read_half, write_half) = tokio::io::split(TokioIo::new(upgraded));
    let writer = tokio::spawn(write_loop(write_half, receiver, shared.clone()));
    let mut stop = context.as_ref().map(|context| context.stop.clone());
    let reader = read_loop(
        BufReader::with_capacity(64 * 1024, read_half),
        shared.clone(),
        max_message,
    );
    tokio::pin!(reader);
    let mut handler_done = false;
    // Once any close is sent, the peer gets a bounded time to answer it.
    let closing = tokio::time::sleep(Duration::from_secs(86_400 * 365));
    tokio::pin!(closing);
    let mut armed = false;
    let mut arm = |closing: std::pin::Pin<&mut tokio::time::Sleep>| {
        if !armed {
            armed = true;
            closing.reset(tokio::time::Instant::now() + CLOSE_WAIT);
        }
    };
    let code = loop {
        tokio::select! {
            code = &mut reader => break code,
            _ = &mut finished, if !handler_done => {
                // The handler returned: close normally if it did not already.
                handler_done = true;
                send_close(&shared, 1000);
                arm(closing.as_mut());
            }
            () = shared.close_started.notified() => arm(closing.as_mut()),
            _ = async { match stop.as_mut() { Some(stop) => { let _ = stop.wait_for(|value| *value).await; } None => std::future::pending().await } } => {
                send_close(&shared, 1001);
                stop = None;
                arm(closing.as_mut());
            }
            () = &mut closing => break Some(1006),
        }
    };
    shared.closed.store(true, Ordering::Release);
    if let Some(code) = code {
        deliver(&shared, vec![Inbound::Close(code)]);
    }
    shared.release_callbacks();
    // Give the writer a bounded moment to flush the closing frame.
    let _ = tokio::time::timeout(CLOSE_WAIT, writer).await;
    notify_drain(&shared);
    drop(context);
}

/// Handle a WebSocket route: dispatch the Python handler, wait for it to accept
/// or deny, then complete the upgrade.
pub(crate) async fn handle(
    dispatch: &Py<PyAny>,
    route_id: usize,
    mut request: Request,
    metadata: impl FnOnce(Python<'_>) -> PyResult<Py<PyDict>>,
    timeout: Duration,
    max_message: usize,
) -> Response {
    let key = match handshake_key(&request) {
        Ok(key) => key,
        Err(detail) => {
            let mut response = crate::error(StatusCode::BAD_REQUEST, detail);
            response.headers_mut().insert(
                header::SEC_WEBSOCKET_VERSION,
                HeaderValue::from_static("13"),
            );
            return response;
        }
    };
    let Some(upgrade) = request
        .extensions_mut()
        .remove::<hyper::upgrade::OnUpgrade>()
    else {
        return crate::error(StatusCode::BAD_REQUEST, "Connection cannot be upgraded");
    };
    let context = request.extensions().get::<ConnectionContext>().cloned();
    let (outbound, receiver) = mpsc::unbounded_channel();
    let shared = Arc::new(Shared {
        outbound,
        queued: AtomicUsize::new(0),
        closed: AtomicBool::new(false),
        close_sent: AtomicBool::new(false),
        credit: Semaphore::new(INBOUND_CREDIT),
        deliver: Mutex::new(None),
        drain_waiter: Mutex::new(None),
        close_started: Notify::new(),
    });
    let (accept_sender, accept_receiver) = oneshot::channel();
    let (finished_sender, finished) = oneshot::channel();
    let submitted = Python::attach(|py| -> PyResult<crate::CancelOnDrop> {
        let channel = Py::new(
            py,
            Channel {
                shared: shared.clone(),
                accept: Mutex::new(Some(accept_sender)),
            },
        )?;
        let arguments = PyDict::new(py);
        arguments.set_item("__bessemer_request__", metadata(py)?)?;
        arguments.set_item("__bessemer_websocket__", channel.clone_ref(py))?;
        let future = dispatch.bind(py).call1((route_id, arguments))?;
        let finished_sender = Mutex::new(Some(finished_sender));
        let callback = PyCFunction::new_closure(
            py,
            None,
            None,
            move |_: &Bound<'_, PyTuple>, _: Option<&Bound<'_, PyDict>>| -> PyResult<()> {
                // A handler that returns without accepting denies the handshake.
                channel.get().accept.lock().unwrap().take();
                if let Some(sender) = finished_sender.lock().unwrap().take() {
                    let _ = sender.send(());
                }
                Ok(())
            },
        )?;
        future.call_method1("add_done_callback", (callback,))?;
        // Cancel the handler if the handshake is abandoned before it accepts.
        Ok(crate::CancelOnDrop {
            future: future.unbind(),
            armed: true,
        })
    });
    let Ok(mut invocation) = submitted else {
        return crate::error(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error");
    };
    let subprotocol = match tokio::time::timeout(timeout, accept_receiver).await {
        Ok(Ok(subprotocol)) => subprotocol,
        Ok(Err(_)) => {
            invocation.armed = false;
            return crate::error(StatusCode::FORBIDDEN, "WebSocket connection denied");
        }
        Err(_) => {
            // Cancel the handler and keep the connection slot until its
            // cleanup has finished, so timed-out handshakes cannot pile up
            // work beyond the connection limit.
            drop(invocation);
            tokio::spawn(async move {
                let _ = finished.await;
                drop(context);
            });
            return crate::error(StatusCode::GATEWAY_TIMEOUT, "WebSocket handshake timed out");
        }
    };
    invocation.armed = false;
    tokio::spawn(session(
        upgrade,
        shared,
        receiver,
        finished,
        context,
        max_message,
    ));
    let mut response = Body::empty().into_response();
    *response.status_mut() = StatusCode::SWITCHING_PROTOCOLS;
    let headers = response.headers_mut();
    headers.insert(header::UPGRADE, HeaderValue::from_static("websocket"));
    headers.insert(header::CONNECTION, HeaderValue::from_static("Upgrade"));
    headers.insert(
        header::SEC_WEBSOCKET_ACCEPT,
        HeaderValue::from_str(&accept_key(key.as_bytes())).unwrap(),
    );
    if let Some(subprotocol) = subprotocol
        && let Ok(value) = HeaderValue::from_str(&subprotocol)
    {
        headers.insert(header::SEC_WEBSOCKET_PROTOCOL, value);
    }
    response
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accept_key_matches_rfc_6455_example() {
        assert_eq!(
            accept_key(b"dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
        );
    }

    #[test]
    fn unmask_matches_bytewise_xor() {
        for length in 0..40 {
            let original: Vec<u8> = (0..length).map(|value: u32| (value * 7) as u8).collect();
            let mask = [0x12, 0x34, 0x56, 0x78];
            let mut data = original.clone();
            unmask(&mut data, mask);
            for (index, byte) in data.iter().enumerate() {
                assert_eq!(*byte, original[index] ^ mask[index % 4]);
            }
        }
    }

    fn masked(fin: bool, opcode: u8, payload: &[u8]) -> Vec<u8> {
        let mask = [1, 2, 3, 4];
        let mut output = vec![(if fin { 0x80 } else { 0 }) | opcode];
        if payload.len() < 126 {
            output.push(0x80 | payload.len() as u8);
        } else {
            output.push(0x80 | 126);
            output.extend_from_slice(&(payload.len() as u16).to_be_bytes());
        }
        output.extend_from_slice(&mask);
        output.extend(
            payload
                .iter()
                .enumerate()
                .map(|(index, byte)| byte ^ mask[index % 4]),
        );
        output
    }

    #[tokio::test]
    async fn frames_are_parsed_and_protocol_errors_detected() {
        let mut input = masked(true, TEXT, b"hello");
        input.extend(masked(true, BINARY, &[7u8; 300]));
        let mut reader = &input[..];
        let frame = read_frame(&mut reader, 1024).await.unwrap();
        assert_eq!(
            (frame.fin, frame.opcode, &frame.payload[..]),
            (true, TEXT, &b"hello"[..])
        );
        let frame = read_frame(&mut reader, 1024).await.unwrap();
        assert_eq!(frame.payload, vec![7u8; 300]);
        assert!(matches!(
            read_frame(&mut reader, 1024).await,
            Err(Failure::Gone)
        ));
        // Unmasked, reserved-bit, oversized and fragmented control frames.
        for (bytes, code) in [
            (vec![0x81, 0x00], 1002),
            (vec![0xC1, 0x80, 0, 0, 0, 0], 1002),
            (masked(true, BINARY, &[0; 300]), 1009),
            (masked(false, PING, b""), 1002),
        ] {
            let result = read_frame(&mut &bytes[..], 256).await;
            assert!(matches!(result, Err(Failure::Protocol(value)) if value == code));
        }
    }

    #[test]
    fn frame_buffered_requires_the_whole_frame() {
        let frame = masked(true, TEXT, &[b'x'; 300]);
        assert!(frame_buffered(&frame));
        for cut in [0, 1, 3, 7, frame.len() - 1] {
            assert!(!frame_buffered(&frame[..cut]), "cut at {cut}");
        }
        let short = masked(true, TEXT, b"hi");
        assert!(frame_buffered(&short));
        assert!(!frame_buffered(&short[..short.len() - 1]));
    }

    #[test]
    fn server_frames_use_minimal_lengths() {
        for (length, header) in [(5usize, 2usize), (126, 4), (70_000, 10)] {
            let mut output = Vec::new();
            encode_frame(&mut output, BINARY, &vec![0; length]);
            assert_eq!(output.len(), header + length);
            assert_eq!(output[0], 0x82);
        }
    }
}
