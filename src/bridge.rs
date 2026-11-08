//! Native request handoff from Tokio to the Python asyncio worker.
//!
//! Tokio threads never take the GIL on the request path. A request becomes a
//! [`Job`] pushed onto a mutex-protected batch. The first job pushed into an
//! empty batch writes one byte to a socket pair that the asyncio loop watches
//! with `add_reader`. The loop thread then drains every queued job at once,
//! builds the Python arguments itself and starts one task per job. Completion
//! returns through a [`Ticket`] that decodes the handler result and wakes the
//! waiting Tokio future. Cancellation requests travel the same queue.

use crate::conversion::Argument;
use crate::{Admission, RequestMetadata, ResponseError, capacity_response, decode_response, error};
use axum::{
    body::Body,
    http::{HeaderMap, HeaderValue, StatusCode, header},
    response::Response,
};
use pyo3::{
    intern,
    prelude::*,
    types::{PyBytes, PyDict, PyList, PyTuple},
};
use std::{
    borrow::Cow,
    collections::{HashMap, HashSet},
    io::{ErrorKind, Read, Write},
    sync::{
        Arc, Mutex, OnceLock,
        atomic::{AtomicU64, AtomicUsize, Ordering},
    },
};
use tokio::{sync::oneshot, time::Instant};

/// High bit set on a cancel entry when the peer disconnected (as opposed to a
/// request deadline or server shutdown).
pub(crate) const DISCONNECT_BIT: u64 = 1 << 63;

/// Most queued jobs at which a local Rust loop is hurried (`Shared::hurry`).
/// Tokio then runs the loop within its `event_interval` (61) more tasks, so
/// about `hurry_at` + 61 requests can be admitted before the loop starts any.
/// (When the loop was the `block_on` future, Tokio's cadence alone capped
/// them at about 61.) Lower, the loop runs smaller batches.
const HURRY_AT: usize = 32;

/// Tokio's default `event_interval`: tasks a current-thread runtime may run
/// before it polls its `block_on` future again.
const EVENT_INTERVAL: usize = 61;

/// The queue length at which to hurry the loop for a server admitting
/// `max_in_flight` requests: `HURRY_AT`, lowered so that it plus Tokio's
/// cadence stays within the admission limit, and 1 (hurry on the first
/// job, as the `block_on` loop effectively ran) when the limit is that small.
pub(crate) fn hurry_threshold(max_in_flight: usize) -> usize {
    max_in_flight
        .saturating_sub(EVENT_INTERVAL)
        .clamp(1, HURRY_AT)
}

/// Everything needed to publish one response after the handler finishes.
pub(crate) struct Pending {
    pub(crate) sender: oneshot::Sender<Reply>,
    pub(crate) admission: Admission,
    pub(crate) head: bool,
    pub(crate) route_id: usize,
    pub(crate) max_response_size: usize,
    pub(crate) response_bytes: crate::budget::ByteBudget,
}

/// The largest JSON body settled as a [`JsonReply`]; larger ones take the
/// general response path, whose writer sends big bodies without copying them.
const JSON_REPLY_LIMIT: usize = 8 * 1024;

/// A small JSON result the HTTP/1 writer can send without a `Response`: status
/// 200 to 599 except 204, 205 and 304, a non-empty body, `application/json`,
/// no other headers. It holds the response byte budget until it is dropped.
pub(crate) struct JsonReply {
    pub(crate) status: StatusCode,
    pub(crate) body: Vec<u8>,
    permit: crate::budget::BytePermit,
    /// The application's headers, Content-Type included, for a result that
    /// carried any (an HTTP middleware's response); None means only
    /// `content-type: application/json`.
    pub(crate) headers: Option<Box<HeaderMap>>,
    pub(crate) after: Option<crate::deferred::AfterResponse>,
}

impl JsonReply {
    #[cfg(test)]
    pub(crate) fn new(status: StatusCode, body: Vec<u8>) -> Self {
        Self {
            status,
            body,
            permit: crate::budget::BytePermit::default(),
            headers: None,
            after: None,
        }
    }

    /// The general-path response, as `json_response` builds it.
    pub(crate) fn into_response(self) -> Response {
        let headers = self.headers.map(|headers| *headers).unwrap_or_else(|| {
            let mut headers = HeaderMap::with_capacity(4);
            headers.insert(
                header::CONTENT_TYPE,
                HeaderValue::from_static("application/json"),
            );
            headers
        });
        let mut response = Response::new(Body::from(crate::budget::ByteBudget::own_reserved(
            self.body,
            self.permit,
        )));
        *response.status_mut() = self.status;
        *response.headers_mut() = headers;
        if let Some(after) = self.after {
            response.extensions_mut().insert(after);
        }
        response
    }

    /// Release the byte budget once the bytes are written.
    pub(crate) fn into_body(self) -> (Vec<u8>, crate::budget::BytePermit) {
        (self.body, self.permit)
    }
}

/// What a waiting request receives from its handler.
pub(crate) enum Reply {
    Full(Response),
    Json(JsonReply),
}

impl Reply {
    #[cfg(test)]
    pub(crate) fn into_response(self) -> Response {
        match self {
            Reply::Full(response) => response,
            Reply::Json(reply) => reply.into_response(),
        }
    }
}

/// A decoded response that has not been handed to the waiting Tokio task yet.
///
/// The native invoker collects these while it drains a batch and publishes
/// them together afterwards, so the I/O side is woken once per batch.
pub(crate) struct Settled {
    sender: oneshot::Sender<Reply>,
    admission: Option<Admission>,
    response: Reply,
}

impl Settled {
    /// Hand the response to its waiter, after admission is available again
    /// (a stream may still hold it). Waking Tokio first could make a following
    /// sequential request observe a spurious 503.
    pub(crate) fn publish(self) {
        drop(self.admission);
        let _ = self.sender.send(self.response);
    }
}

impl Pending {
    /// Decode a completed handler result and publish it.
    pub(crate) fn finish(self, result: PyResult<Bound<'_, PyAny>>) {
        if let Some(settled) = self.settle(result) {
            settled.publish();
        }
    }

    /// Decode a completed handler result into the response to publish, or
    /// None when nobody is waiting for it any more.
    pub(crate) fn settle(self, result: PyResult<Bound<'_, PyAny>>) -> Option<Settled> {
        self.settle_deferred(result, None)
    }

    pub(crate) fn settle_deferred(
        self,
        result: PyResult<Bound<'_, PyAny>>,
        deferred: Option<(Py<PyAny>, Vec<Py<PyAny>>)>,
    ) -> Option<Settled> {
        if !self.head
            && let Ok(value) = &result
            && let Some((status, body, headers)) =
                plain_json_envelope(value, JSON_REPLY_LIMIT.min(self.max_response_size))
        {
            return self.settle_json_ready(status, body, headers, deferred);
        }
        let Pending {
            sender,
            admission,
            head,
            route_id,
            max_response_size,
            response_bytes,
        } = self;
        let mut admission = Some(admission);
        if sender.is_closed() {
            // Nobody is waiting for this response, but a stream result owns
            // dependency cleanup. Decoding it hands admission to the stream,
            // and dropping it closes the stream before admission is released.
            if let Ok(value) = result
                && let Ok(response) = decode_response(
                    &value,
                    max_response_size,
                    &response_bytes,
                    head,
                    &mut admission,
                )
                && deferred.is_some()
                && let Some(stream) = response.extensions().get::<crate::streaming::StreamStart>()
            {
                admission = stream.take_admission();
            }
            if let Some((start, handles)) = deferred {
                crate::deferred::AfterResponse::new(start, handles, admission.take()).finish(false);
            }
            return None;
        }
        let decoded = result.map_err(ResponseError::from).and_then(|value| {
            decode_response(
                &value,
                max_response_size,
                &response_bytes,
                head,
                &mut admission,
            )
        });
        let succeeded = decoded.is_ok();
        let mut response = settled_response(decoded, route_id);
        if let Some((start, handles)) = deferred {
            if admission.is_none()
                && let Some(stream) = response.extensions().get::<crate::streaming::StreamStart>()
            {
                admission = stream.take_admission();
            }
            let after = crate::deferred::AfterResponse::new(start, handles, admission.take());
            if succeeded {
                response.extensions_mut().insert(after);
            } else {
                after.finish(false);
            }
        }
        if response
            .extensions()
            .get::<crate::static_files::PendingFile>()
            .is_some()
        {
            if let Some(held) = admission.take() {
                response
                    .extensions_mut()
                    .insert(crate::deferred::AfterResponse::hold(held));
            } else if let Some(after) = response
                .extensions()
                .get::<crate::deferred::AfterResponse>()
            {
                after.mark_file();
            }
        }
        let response = Reply::Full(response);
        Some(Settled {
            sender,
            admission,
            response,
        })
    }

    /// The response for a JSON body the invoker already encoded natively, as
    /// [`decode_response`] builds it from a `json_bytes` envelope.
    pub(crate) fn settle_json(self, status: u16, body: Vec<u8>) -> Option<Settled> {
        if self.sender.is_closed() {
            return None;
        }
        let small = !self.head
            && !body.is_empty()
            && body.len() <= JSON_REPLY_LIMIT.min(self.max_response_size)
            && !matches!(status, 204 | 205 | 304);
        if small
            && let Some(status) = StatusCode::from_u16(status)
                .ok()
                .filter(|status| (200..=599).contains(&status.as_u16()))
        {
            let reply = match self.response_bytes.acquire(body.len()) {
                Ok(permit) => Reply::Json(JsonReply {
                    status,
                    body,
                    permit,
                    headers: None,
                    after: None,
                }),
                Err(_) => Reply::Full(capacity_response("Response byte capacity is full", false)),
            };
            return Some(Settled {
                sender: self.sender,
                admission: Some(self.admission),
                response: reply,
            });
        }
        let response = settled_response(
            json_response(
                status,
                body,
                self.max_response_size,
                &self.response_bytes,
                self.head,
            ),
            self.route_id,
        );
        Some(self.settled(response))
    }

    /// An already checked compact JSON envelope, including delivery-owned work.
    fn settle_json_ready(
        self,
        status: u16,
        body: Vec<u8>,
        headers: Option<HeaderMap>,
        deferred: Option<(Py<PyAny>, Vec<Py<PyAny>>)>,
    ) -> Option<Settled> {
        let mut admission = Some(self.admission);
        let after = deferred.map(|(start, handles)| {
            crate::deferred::AfterResponse::new(start, handles, admission.take())
        });
        if self.sender.is_closed() {
            // Dropping the guard skips tasks and retains admission for cleanup.
            drop(after);
            return None;
        }
        let status = StatusCode::from_u16(status).ok()?;
        let reply = match self.response_bytes.acquire(body.len()) {
            Ok(permit) => Reply::Json(JsonReply {
                status,
                body,
                permit,
                headers: headers.map(Box::new),
                after,
            }),
            Err(_) => {
                drop(after);
                Reply::Full(capacity_response("Response byte capacity is full", false))
            }
        };
        Some(Settled {
            sender: self.sender,
            admission,
            response: reply,
        })
    }

    /// The response to publish without running Python work.
    pub(crate) fn settled(self, response: Response) -> Settled {
        Settled {
            sender: self.sender,
            admission: Some(self.admission),
            response: Reply::Full(response),
        }
    }

    /// Publish a response without running Python work.
    pub(crate) fn respond(self, response: Response) {
        self.settled(response).publish();
    }
}

/// `(status, body, headers)` of a `(status, bytes, headers, "json_bytes")`
/// envelope with a valid status and a body of at most `limit` bytes, which
/// `decode_response` and `settle_json` answer alike. Larger bodies keep the
/// general path, which sends them without copying.
///
/// Headers (an HTTP middleware's response) are validated as
/// `decode_response` validates them, Content-Type added if missing, and are
/// only taken for a non-empty body of a status with one and without a `Date`
/// (which the writer would otherwise have to reconcile with its own); every
/// other envelope, and one whose headers are invalid, keeps the general path.
fn plain_json_envelope(
    value: &Bound<'_, PyAny>,
    limit: usize,
) -> Option<(u16, Vec<u8>, Option<HeaderMap>)> {
    let envelope = value.cast::<PyTuple>().ok()?;
    if envelope.len() != 4 {
        return None;
    }
    let kind = envelope.get_item(3).ok()?;
    let kind = kind.cast::<pyo3::types::PyString>().ok()?;
    if kind.to_str().ok()? != "json_bytes" {
        return None;
    }
    let status: u16 = envelope.get_item(0).ok()?.extract().ok()?;
    if !(200..=599).contains(&status) {
        return None;
    }
    let body = envelope.get_item(1).ok()?;
    let body = body.cast::<PyBytes>().ok()?.as_bytes();
    if body.len() > limit {
        return None;
    }
    if body.is_empty() || matches!(status, 204 | 205 | 304) {
        return None;
    }
    let raw = envelope.get_item(2).ok()?;
    if raw.len().ok()? == 0 {
        return Some((status, body.to_vec(), None));
    }
    let mut headers = crate::response_headers(&raw).ok()?;
    if headers.contains_key(header::DATE) {
        return None;
    }
    headers
        .entry(header::CONTENT_TYPE)
        .or_insert(HeaderValue::from_static("application/json"));
    Some((status, body.to_vec(), Some(headers)))
}

fn settled_response(decoded: Result<Response, ResponseError>, route_id: usize) -> Response {
    match decoded {
        Ok(response) => response,
        Err(ResponseError::Full) => capacity_response("Response byte capacity is full", false),
        Err(ResponseError::Invalid) => {
            diagnostic!("Bessemer could not encode the response for route {route_id}");
            error(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error")
        }
    }
}

/// Build the response for an encoded JSON body exactly as a `json_bytes`
/// envelope decodes, without a Python tuple or bytes object in between.
fn json_response(
    status: u16,
    body: Vec<u8>,
    max_bytes: usize,
    budget: &crate::budget::ByteBudget,
    head: bool,
) -> Result<Response, ResponseError> {
    let status = StatusCode::from_u16(status)
        .ok()
        .filter(|status| (200..=599).contains(&status.as_u16()))
        .ok_or(ResponseError::Invalid)?;
    // Later controls add a request id and content length; size for them.
    let mut headers = HeaderMap::with_capacity(4);
    let body = if matches!(status.as_u16(), 204 | 205 | 304) {
        Vec::new()
    } else {
        if body.len() > max_bytes {
            return Err(ResponseError::Invalid);
        }
        headers.insert(
            header::CONTENT_TYPE,
            HeaderValue::from_static("application/json"),
        );
        body
    };
    let body = if head {
        if !body.is_empty() {
            crate::set_head_length(&mut headers, body.len());
        }
        Body::empty()
    } else {
        Body::from(budget.own(body).map_err(|_| ResponseError::Full)?)
    };
    let mut response = Response::new(body);
    *response.status_mut() = status;
    *response.headers_mut() = headers;
    Ok(response)
}

/// A request's bound inputs by parameter name, in parameter order.
///
/// Keys share the route's parameter names (cloning one is a reference count),
/// and a route has only a few inputs, so a list searched in place replaces a
/// map built for every request. A name bound twice (a dependency and its
/// handler declaring the same input) keeps one entry with the last value.
#[derive(Debug, Default)]
pub(crate) struct Arguments(Vec<(Arc<str>, Argument)>);

impl Arguments {
    pub(crate) fn with_capacity(capacity: usize) -> Self {
        Self(Vec::with_capacity(capacity))
    }

    pub(crate) fn insert(&mut self, key: Arc<str>, value: impl Into<Argument>) {
        let value = value.into();
        match self.0.iter_mut().find(|(name, _)| **name == *key) {
            Some(entry) => entry.1 = value,
            None => self.0.push((key, value)),
        }
    }

    pub(crate) fn get(&self, key: &str) -> Option<&Argument> {
        self.0
            .iter()
            .find(|(name, _)| &**name == key)
            .map(|(_, value)| value)
    }

    pub(crate) fn len(&self) -> usize {
        self.0.len()
    }

    pub(crate) fn iter(&self) -> impl Iterator<Item = (&str, &Argument)> {
        self.0.iter().map(|(name, value)| (&**name, value))
    }
}

pub(crate) struct Job {
    pub(crate) id: u64,
    pub(crate) route_id: usize,
    pub(crate) arguments: Arguments,
    /// The JSON body for the route's body plan, checked but not yet parsed.
    pub(crate) raw_body: Option<bytes::Bytes>,
    pub(crate) uploads: Vec<crate::forms::Upload>,
    pub(crate) metadata: Option<RequestMetadata>,
    pub(crate) deadline: Instant,
    pub(crate) delay_deadline: Instant,
    pub(crate) pending: Pending,
}

#[derive(Default)]
struct Batch {
    jobs: Vec<Job>,
    cancels: Vec<u64>,
    closed: bool,
}

/// The compiled model for one route's JSON body argument.
pub(crate) struct BodyPlan {
    key: String,
    plan: Py<crate::ModelPlan>,
}

/// Counts one admitted request against the loop it was sent to, until the
/// request (including any streamed body) releases its admission.
#[derive(Default)]
pub(crate) struct LaneLoad(Option<Arc<AtomicUsize>>);

impl LaneLoad {
    fn enter(counter: &Arc<AtomicUsize>) -> Self {
        counter.fetch_add(1, Ordering::Relaxed);
        Self(Some(counter.clone()))
    }
}

impl Drop for LaneLoad {
    fn drop(&mut self) {
        if let Some(counter) = &self.0 {
            counter.fetch_sub(1, Ordering::Relaxed);
        }
    }
}

/// Picks the loop for each request when a worker runs several. Each request
/// goes to the loop with the fewest admitted requests, scanning from a rotating
/// start so ties spread evenly.
pub(crate) struct Lanes {
    lanes: Vec<Arc<Shared>>,
    next: AtomicUsize,
}

impl Lanes {
    /// Size each lane's hurry threshold to the server's admission limit.
    pub(crate) fn set_capacity(&self, max_in_flight: usize) {
        let hurry_at = hurry_threshold(max_in_flight);
        for lane in &self.lanes {
            lane.hurry_at.store(hurry_at, Ordering::Relaxed);
        }
    }

    /// Whether a Rust loop running on the calling thread has jobs it has not
    /// taken yet; if so, it is hurried to take them. A request refused
    /// admission on that thread can then yield and try again (see
    /// `admit_after_local_loop`): the permits it lacks are, at least in part,
    /// held by those jobs, which the loop cannot start until the calling
    /// task yields.
    pub(crate) fn hurry_local_backlog(&self) -> bool {
        #[cfg(any(target_os = "linux", target_os = "macos"))]
        for lane in &self.lanes {
            if let Some(event_loop) = lane.local.get() {
                let event_loop = event_loop.get();
                if event_loop.runs_here() && lane.has_jobs() {
                    event_loop.hurry();
                    return true;
                }
            }
        }
        false
    }

    pub(crate) fn enable_adaptive(&self, timeout: std::time::Duration) {
        for lane in &self.lanes {
            lane.delay.enable(timeout);
        }
    }

    pub(crate) fn set_shutting_down(&self) {
        for lane in &self.lanes {
            lane.shutting_down.store(true, Ordering::Relaxed);
        }
    }

    pub(crate) fn overloaded(&self) -> bool {
        !self.lanes.is_empty() && self.lanes.iter().all(|lane| lane.delay.overloaded())
    }

    pub(crate) fn new(lanes: Vec<Arc<Shared>>) -> Self {
        if lanes.len() > 1 {
            for lane in &lanes {
                lane.counts_load.store(true, Ordering::Relaxed);
            }
        }
        Self {
            lanes,
            next: AtomicUsize::new(0),
        }
    }

    pub(crate) fn choose(&self) -> Option<&Arc<Shared>> {
        match self.lanes.len() {
            0 => None,
            1 => self.lanes.first(),
            count => {
                let start = self.next.fetch_add(1, Ordering::Relaxed) % count;
                let mut best = None;
                let mut lowest = usize::MAX;
                for offset in 0..count {
                    let lane = &self.lanes[(start + offset) % count];
                    if lane.delay.overloaded() {
                        continue;
                    }
                    let load = lane.load.load(Ordering::Relaxed);
                    if best.is_none() || load < lowest {
                        best = Some(lane);
                        lowest = load;
                    }
                }
                best.or(Some(&self.lanes[start]))
            }
        }
    }
}

impl Shared {
    /// Whether the worker installed a body model plan for `route_id`.
    pub(crate) fn has_body_plan(&self, route_id: usize) -> bool {
        self.body_plans
            .get()
            .is_some_and(|plans| plans.contains_key(&route_id))
    }

    /// The body model plan installed for `route_id`, if any.
    pub(crate) fn body_plan(&self, route_id: usize) -> Option<&BodyPlan> {
        self.body_plans.get().and_then(|plans| plans.get(&route_id))
    }
}

pub(crate) struct Shared {
    pub(crate) completed: Mutex<Vec<crate::invoker::SyncDelivery>>,
    batch: Mutex<Batch>,
    /// Body model plans by route id, installed once by the worker.
    body_plans: OnceLock<HashMap<usize, BodyPlan>>,
    load: Arc<AtomicUsize>,
    /// Whether requests are counted in `load`: only when a worker runs
    /// several loops, because only then does anyone read it.
    counts_load: std::sync::atomic::AtomicBool,
    wake_sender: crate::platform::WakeStream,
    wake_receiver: crate::platform::WakeStream,
    next_id: AtomicU64,
    /// A Rust event loop that serves this bridge's connections on its own
    /// thread, woken directly instead of through the socket.
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    local: OnceLock<Py<bessemer_loop::LoopCore>>,
    /// Queued jobs at which the local loop is hurried; see `hurry_threshold`.
    hurry_at: AtomicUsize,
    pub(crate) delay: crate::adaptive::Delay,
    shutting_down: std::sync::atomic::AtomicBool,
}

impl Shared {
    /// Count a request against this loop for as long as `admission` lives.
    pub(crate) fn enter(&self, admission: &mut Admission) {
        if self.counts_load.load(Ordering::Relaxed) {
            admission._lane = LaneLoad::enter(&self.load);
        }
    }

    pub(crate) fn wake(&self) {
        #[cfg(any(target_os = "linux", target_os = "macos"))]
        if let Some(event_loop) = self.local.get() {
            event_loop
                .get()
                .wake_reader(std::os::fd::AsRawFd::as_raw_fd(&self.wake_receiver));
            return;
        }
        // A full socket buffer already guarantees a pending wake-up.
        let _ = (&self.wake_sender).write(&[1]);
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn submit(
        &self,
        route_id: usize,
        arguments: Arguments,
        raw_body: Option<bytes::Bytes>,
        uploads: Vec<crate::forms::Upload>,
        metadata: Option<RequestMetadata>,
        deadline: Instant,
        pending: Pending,
        queued_at: Instant,
    ) -> Result<u64, Pending> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let job = Job {
            id,
            route_id,
            arguments,
            raw_body,
            uploads,
            metadata,
            deadline,
            pending,
            delay_deadline: self.delay.deadline(queued_at),
        };
        let mut batch = self.batch.lock().unwrap();
        if batch.closed {
            return Err(job.pending);
        }
        let was_empty = batch.jobs.is_empty() && batch.cancels.is_empty();
        batch.jobs.push(job);
        let queued = batch.jobs.len();
        drop(batch);
        let hurry_at = self.hurry_at.load(Ordering::Relaxed);
        if was_empty {
            self.wake();
            if hurry_at <= 1 {
                self.hurry();
            }
        } else if queued == hurry_at {
            // `==`, not `>=`: the loop latches the hurry until it takes the
            // batch, so later jobs in the same batch need not repeat it.
            self.hurry();
        }
        Ok(id)
    }

    /// Have a local Rust loop take the batch ahead of the connection tasks
    /// queued on its runtime. Its body is a task of that runtime, woken by
    /// the first job behind every connection already ready: with many ready
    /// connections, every one of them would submit (and hold an admission
    /// permit) before the loop started any, and requests over
    /// `max_in_flight` would be refused.
    fn hurry(&self) {
        #[cfg(any(target_os = "linux", target_os = "macos"))]
        if let Some(event_loop) = self.local.get() {
            event_loop.get().hurry();
        }
    }

    /// Ask the worker to cancel a submitted job. Never touches Python.
    pub(crate) fn cancel(&self, id: u64) {
        let mut batch = self.batch.lock().unwrap();
        if batch.closed {
            return;
        }
        let was_empty = batch.jobs.is_empty() && batch.cancels.is_empty();
        batch.cancels.push(id);
        drop(batch);
        if was_empty {
            self.wake();
        }
    }

    /// Tell the worker that a submitted job's client disconnected.
    pub(crate) fn disconnect(&self, id: u64) {
        if self.shutting_down.load(Ordering::Relaxed) {
            self.cancel(id);
        } else {
            self.cancel(id | DISCONNECT_BIT);
        }
    }

    fn clear_wake(&self) {
        // A local wake never writes to the socket.
        #[cfg(any(target_os = "linux", target_os = "macos"))]
        if self.local.get().is_some() {
            return;
        }
        let mut buffer = [0u8; 256];
        loop {
            match (&self.wake_receiver).read(&mut buffer) {
                // A short read emptied the socket: skip the extra read that
                // would only report WouldBlock. A byte written after this read
                // costs at most one spurious drain of an empty batch.
                Ok(count) if count < buffer.len() => return,
                Ok(_) => continue,
                Err(error) if error.kind() == ErrorKind::Interrupted => continue,
                Err(_) => return,
            }
        }
    }

    /// Swap the queued batch into `jobs` and `cancels`, which must be empty,
    /// leaving their (reusable) buffers behind for the next batch.
    pub(crate) fn swap(&self, jobs: &mut Vec<Job>, cancels: &mut Vec<u64>) {
        self.clear_wake();
        let mut batch = self.batch.lock().unwrap();
        std::mem::swap(&mut batch.jobs, jobs);
        std::mem::swap(&mut batch.cancels, cancels);
    }

    /// Whether jobs are queued that the worker has not taken yet.
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    fn has_jobs(&self) -> bool {
        !self.batch.lock().unwrap().jobs.is_empty() || !self.completed.lock().unwrap().is_empty()
    }

    pub(crate) fn take(&self) -> (Vec<Job>, Vec<u64>) {
        // Clear the wake-up before taking the batch. A job pushed after the
        // take finds an empty batch and writes a fresh wake-up byte.
        self.clear_wake();
        let mut batch = self.batch.lock().unwrap();
        (
            std::mem::take(&mut batch.jobs),
            std::mem::take(&mut batch.cancels),
        )
    }
}

/// Why a handler's arguments could not be built.
pub(crate) enum ArgumentsError {
    /// The body is not valid JSON: answered with the 422 `json_invalid`.
    InvalidJson,
    Python,
}

impl From<PyErr> for ArgumentsError {
    fn from(_: PyErr) -> Self {
        Self::Python
    }
}

impl From<pyo3::CastError<'_, '_>> for ArgumentsError {
    fn from(_: pyo3::CastError<'_, '_>) -> Self {
        Self::Python
    }
}

/// The 422 for a body that is not valid JSON.
pub(crate) fn invalid_json_response() -> Response {
    // FastAPI's member order (type, loc, msg, input); `json!` would sort it.
    crate::json_body(
        StatusCode::UNPROCESSABLE_ENTITY,
        r#"{"detail":[{"type":"json_invalid","loc":["body"],"msg":"Invalid JSON","input":null}]}"#,
    )
}

/// Build the keyword arguments for one handler call on the loop thread.
///
/// A body bound to a plain Pydantic model with a compiled plan is built into
/// the model instance here when it already has the exact field types; the
/// worker then skips `model_validate` for it. Any other body reaches Python as
/// plain JSON values and is validated by Pydantic as before.
///
/// `raw_body` is that body still as bytes, not yet checked as JSON. It is
/// parsed straight into the model, in one pass, when every value has its exact
/// type, and otherwise parsed and converted exactly as an ordinary body. Bytes
/// that are not valid JSON give [`ArgumentsError::InvalidJson`].
pub(crate) fn python_arguments<'py>(
    py: Python<'py>,
    argument_values: &Arguments,
    raw_body: Option<bytes::Bytes>,
    parsed_body: Option<Bound<'py, PyAny>>,
    uploads: Vec<crate::forms::Upload>,
    metadata: Option<RequestMetadata>,
    body_plan: Option<&BodyPlan>,
) -> Result<Bound<'py, PyDict>, ArgumentsError> {
    let arguments = PyDict::new(py);
    if let Some(bytes) = raw_body {
        let plan = body_plan.ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err("raw body without a body plan")
        })?;
        let compiled = &plan.plan.get().0;
        let model = match parsed_body {
            Some(instance) => Some(instance),
            None => match bessemer_codec::parse_model(py, compiled, &bytes) {
                Ok(model) => model,
                // Typed parsing met bad syntax before any mismatch. The
                // scanner is the authority on what is valid.
                Err(_) => {
                    bessemer_codec::validate_json(&bytes)
                        .map_err(|_| ArgumentsError::InvalidJson)?;
                    None
                }
            },
        };
        let value = match model {
            Some(instance) => instance,
            None => {
                // Typed parsing stops at its first mismatch, so this is also
                // where the rest of the body is found invalid.
                let parsed =
                    bessemer_codec::parse_value(&bytes).map_err(|_| ArgumentsError::InvalidJson)?;
                match bessemer_codec::build_model(py, compiled, &parsed) {
                    Some(instance) => instance,
                    None => crate::conversion::json_to_python(py, &parsed)?.into_bound(py),
                }
            }
        };
        arguments.set_item(&plan.key, value)?;
    }
    for (key, value) in argument_values.iter() {
        if let Some(plan) = body_plan.filter(|plan| plan.key == key)
            && let Argument::Json(value) = value
            && let Some(instance) = bessemer_codec::build_model(py, &plan.plan.get().0, value)
        {
            arguments.set_item(key, instance)?;
            continue;
        }
        arguments.set_item(key, crate::conversion::argument_to_python(py, value)?)?;
    }
    for upload in uploads {
        let value = match (&upload.data, upload.raw) {
            (crate::forms::UploadData::Memory(data), true) => PyBytes::new(py, data).into_any(),
            // `uploads::materialize` reads these before dispatch; this only
            // covers a direct caller.
            (crate::forms::UploadData::Spooled { file, range }, true) => {
                PyBytes::new_with(py, range.len(), |data| {
                    crate::platform::read_exact_at(file, data, range.start as u64)
                        .map_err(|error| pyo3::exceptions::PyOSError::new_err(error.to_string()))
                })?
                .into_any()
            }
            (data, false) => {
                // The worker wraps these in UploadFile.
                let file = PyDict::new(py);
                file.set_item("filename", upload.filename)?;
                file.set_item("content_type", upload.content_type)?;
                file.set_item("headers", upload.headers)?;
                match data {
                    crate::forms::UploadData::Memory(data) => {
                        file.set_item("data", PyBytes::new(py, data))?;
                    }
                    crate::forms::UploadData::Spooled { file: spool, range } => {
                        // Python owns its own handle on the spool file,
                        // closed with the UploadFile.
                        let opened = crate::platform::python_file(py, spool)?;
                        file.set_item("spooled", opened)?;
                        file.set_item("offset", range.start)?;
                        file.set_item("size", range.len())?;
                    }
                }
                file.into_any()
            }
        };
        if upload.many {
            match arguments.get_item(&upload.key)? {
                Some(list) => list.cast::<PyList>()?.append(value)?,
                None => arguments.set_item(&upload.key, PyList::new(py, [value])?)?,
            }
        } else {
            arguments.set_item(&upload.key, value)?;
        }
    }
    if let Some(metadata) = metadata {
        arguments.set_item(
            intern!(py, "__bessemer_request__"),
            Bound::new(py, RequestHead { metadata })?,
        )?;
    }
    Ok(arguments)
}

/// A `needs_request` route's request metadata, kept in Rust until Python
/// reads it: `bessemer.request.Request` takes the few fields every request
/// has at once and converts the headers, body, cookies and the rest only
/// when they are first used.
#[pyclass(frozen, module = "bessemer._native")]
pub(crate) struct RequestHead {
    metadata: RequestMetadata,
}

#[pymethods]
impl RequestHead {
    /// `(method, path, scheme, query_string, request_id)`. The request id
    /// falls back to the last `x-request-id` header, as `Request` does for
    /// metadata without one.
    fn fields<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        let metadata = &self.metadata;
        let request_id = match metadata.request_id.as_deref() {
            Some(id) if !id.is_empty() => Some(Cow::Borrowed(id)),
            _ => metadata
                .headers
                .get_all("x-request-id")
                .iter()
                .next_back()
                .map(crate::header_text),
        };
        (
            metadata.method.as_str(),
            metadata.path.as_str(),
            if metadata.secure { "https" } else { "http" },
            metadata.query_string.as_str(),
            request_id,
        )
            .into_pyobject(py)
    }

    /// The header fields in order as `(name, value)` pairs; names are
    /// lowercase, as the HTTP parser stores them.
    fn headers<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        let pairs = self
            .metadata
            .headers
            .iter()
            .map(|(name, value)| (name.as_str(), crate::header_text(value)).into_pyobject(py))
            .collect::<PyResult<Vec<_>>>()?;
        PyTuple::new(py, pairs)
    }

    fn client(&self) -> Option<String> {
        self.metadata.client.map(|ip| ip.to_string())
    }

    fn client_certificate<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        self.metadata
            .client_certificate
            .as_deref()
            .map(|certificate| PyBytes::new(py, certificate))
    }

    fn body<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.metadata.body)
    }

    fn spooled(&self) -> bool {
        self.metadata.spooled
    }

    fn path_params(&self) -> HashMap<String, String> {
        self.metadata.path_params.clone()
    }

    fn cookies(&self) -> HashMap<String, String> {
        self.metadata.cookies.clone()
    }
}

/// The native queue shared by the HTTP server and one asyncio worker.
#[pyclass(frozen, module = "bessemer._native")]
pub(crate) struct Bridge {
    pub(crate) shared: Arc<Shared>,
    /// The worker's thread-safe `submit`, for paths that need a Python future
    /// to hold on to, such as WebSocket sessions. Ordinary requests use the queue.
    pub(crate) submit: Option<Py<PyAny>>,
}

#[pymethods]
impl Bridge {
    #[new]
    #[pyo3(signature = (submit=None))]
    fn new(submit: Option<Py<PyAny>>) -> PyResult<Self> {
        let (wake_sender, wake_receiver) = crate::platform::wake_pair()?;
        Ok(Self {
            shared: Arc::new(Shared {
                completed: Mutex::new(Vec::new()),
                batch: Mutex::new(Batch::default()),
                body_plans: OnceLock::new(),
                load: Arc::new(AtomicUsize::new(0)),
                counts_load: std::sync::atomic::AtomicBool::new(false),
                wake_sender,
                wake_receiver,
                next_id: AtomicU64::new(0),
                #[cfg(any(target_os = "linux", target_os = "macos"))]
                local: OnceLock::new(),
                hurry_at: AtomicUsize::new(HURRY_AT),
                delay: crate::adaptive::Delay::default(),
                shutting_down: std::sync::atomic::AtomicBool::new(false),
            }),
            submit,
        })
    }

    /// Install the body model plans, as `[(route_id, key, plan), ...]`.
    /// Called once, before the worker serves requests from this bridge.
    fn set_body_plans(&self, plans: Vec<(usize, String, Py<crate::ModelPlan>)>) -> PyResult<()> {
        let plans = plans
            .into_iter()
            .map(|(route_id, key, plan)| (route_id, BodyPlan { key, plan }))
            .collect();
        self.shared
            .body_plans
            .set(plans)
            .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("body plans are already set"))
    }

    /// Wake `event_loop`, a Rust event loop with an embedded runtime that
    /// watches `fileno()`, directly rather than through the socket.
    fn set_local_wake(&self, event_loop: Bound<'_, PyAny>) -> PyResult<()> {
        #[cfg(any(target_os = "linux", target_os = "macos"))]
        {
            let event_loop = event_loop
                .cast::<bessemer_loop::LoopCore>()
                .map_err(|_| {
                    pyo3::exceptions::PyTypeError::new_err(
                        "event_loop must be a bessemer._loop.Loop",
                    )
                })?
                .clone()
                .unbind();
            self.shared.local.set(event_loop).map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("the local wake is already set")
            })
        }
        #[cfg(not(any(target_os = "linux", target_os = "macos")))]
        {
            let _ = event_loop;
            Err(pyo3::exceptions::PyValueError::new_err(
                "a local wake is only supported on Linux and macOS",
            ))
        }
    }

    fn call_sync(&self, executor: &Bound<'_, PyAny>, spec: &Bound<'_, PyTuple>) -> PyResult<()> {
        crate::invoker::submit_pool(self.shared.clone(), executor, spec)
    }

    /// The descriptor (a socket handle on Windows) the asyncio loop watches
    /// for queued work.
    fn fileno(&self) -> i64 {
        crate::platform::wake_handle(&self.shared.wake_receiver)
    }

    /// Take all queued work.
    ///
    /// Returns `(jobs, cancels)`: each job is `(ticket, route_id, arguments)`,
    /// and `cancels` lists ids of previously drained jobs to cancel. Jobs that
    /// expired or were cancelled while queued are answered here and never
    /// start Python work.
    fn drain<'py>(&self, py: Python<'py>) -> PyResult<(Bound<'py, PyList>, Bound<'py, PyList>)> {
        let (jobs, cancels) = self.shared.take();
        let mut cancelled: HashSet<u64> =
            cancels.into_iter().map(|id| id & !DISCONNECT_BIT).collect();
        let body_plans = self.shared.body_plans.get();
        let output = PyList::empty(py);
        for job in jobs {
            if cancelled.remove(&job.id) || job.pending.sender.is_closed() {
                // Never started and the waiter has gone: release capacity now.
                drop(job);
                continue;
            }
            if Instant::now() >= job.deadline {
                job.pending
                    .respond(error(StatusCode::GATEWAY_TIMEOUT, "Request timed out"));
                continue;
            }
            let body_plan = body_plans.and_then(|plans| plans.get(&job.route_id));
            let arguments = match python_arguments(
                py,
                &job.arguments,
                job.raw_body,
                None,
                job.uploads,
                job.metadata,
                body_plan,
            ) {
                Ok(arguments) => arguments,
                Err(ArgumentsError::InvalidJson) => {
                    job.pending.respond(invalid_json_response());
                    continue;
                }
                Err(ArgumentsError::Python) => {
                    job.pending.respond(error(
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "Internal Server Error",
                    ));
                    continue;
                }
            };
            // Converting this or earlier jobs in the batch can outlast a short
            // deadline. Recheck so expired work never starts.
            if Instant::now() >= job.deadline || job.pending.sender.is_closed() {
                job.pending
                    .respond(error(StatusCode::GATEWAY_TIMEOUT, "Request timed out"));
                continue;
            }
            let ticket = Bound::new(
                py,
                Ticket {
                    id: job.id,
                    pending: Mutex::new(Some(job.pending)),
                },
            )?;
            output.append(PyTuple::new(
                py,
                [
                    ticket.into_any(),
                    job.route_id.into_pyobject(py)?.into_any(),
                    arguments.into_any(),
                ],
            )?)?;
        }
        let mut remaining: Vec<u64> = cancelled.into_iter().collect();
        remaining.sort_unstable();
        Ok((output, PyList::new(py, remaining)?))
    }

    /// Stop accepting work and answer everything still queued.
    fn close(&self) {
        let jobs = {
            let mut batch = self.shared.batch.lock().unwrap();
            batch.closed = true;
            batch.cancels.clear();
            std::mem::take(&mut batch.jobs)
        };
        for job in jobs {
            job.pending.respond(error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Internal Server Error",
            ));
        }
    }
}

/// Completion handle for one drained job.
#[pyclass(frozen, module = "bessemer._native")]
pub(crate) struct Ticket {
    #[pyo3(get)]
    id: u64,
    pending: Mutex<Option<Pending>>,
}

#[pymethods]
impl Ticket {
    /// Publish the result of a finished asyncio task (or future).
    fn complete(&self, task: &Bound<'_, PyAny>) {
        let Some(pending) = self.pending.lock().unwrap().take() else {
            return;
        };
        pending.finish(task.call_method0("result"));
    }

    /// Answer with a generic server error without running the handler.
    fn reject(&self) {
        if let Some(pending) = self.pending.lock().unwrap().take() {
            pending.respond(error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Internal Server Error",
            ));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyo3::types::IntoPyDict;
    use serde_json::Value;
    use std::time::Duration;
    use tokio::sync::Semaphore;

    #[test]
    fn the_hurry_threshold_keeps_the_loop_within_the_admission_limit() {
        assert_eq!(hurry_threshold(128), 32);
        assert_eq!(hurry_threshold(1024), 32);
        assert_eq!(hurry_threshold(93), 32);
        assert_eq!(hurry_threshold(80), 19);
        for small in [1, 32, 61, 62, 64] {
            assert!(hurry_threshold(small) <= 3, "{small}");
        }
        for limit in 1..2048 {
            let at = hurry_threshold(limit);
            assert!((1..=HURRY_AT).contains(&at));
            assert!(limit <= EVENT_INTERVAL || at + EVENT_INTERVAL <= limit);
        }
    }

    fn pending(capacity: &Arc<Semaphore>) -> (Pending, oneshot::Receiver<Reply>, ()) {
        let (sender, receiver) = oneshot::channel();
        let admission = Admission {
            _request: capacity.clone().try_acquire_owned().unwrap(),
            _input: crate::budget::BytePermit::default(),
            _spool: crate::budget::BytePermit::default(),
            _lane: LaneLoad::default(),
        };
        (
            Pending {
                sender,
                admission,
                head: false,
                route_id: 0,
                max_response_size: 64,
                response_bytes: crate::budget::ByteBudget::new(64),
            },
            receiver,
            (),
        )
    }

    fn queued_bytes(shared: &Shared) -> usize {
        let mut buffer = [0u8; 64];
        let mut total = 0;
        while let Ok(count) = (&shared.wake_receiver).read(&mut buffer) {
            if count == 0 {
                break;
            }
            total += count;
        }
        total
    }

    #[test]
    fn arguments_keep_one_entry_per_name_with_the_last_value() {
        let mut arguments = Arguments::with_capacity(2);
        let name: Arc<str> = "q".into();
        arguments.insert(name.clone(), Value::from(1));
        arguments.insert("page".into(), Value::from(2));
        arguments.insert(name, Value::from(3));
        assert_eq!(arguments.len(), 2);
        assert_eq!(arguments.get("q"), Some(&Argument::Json(Value::from(3))));
        assert_eq!(arguments.get("missing"), None);
        let names: Vec<&str> = arguments.iter().map(|(name, _)| name).collect();
        assert_eq!(names, ["q", "page"]);
    }

    #[test]
    fn wake_ups_coalesce_and_drain_answers_expired_and_cancelled_jobs() {
        Python::attach(|py| {
            let bridge = Bridge::new(None).unwrap();
            let shared = bridge.shared.clone();
            let capacity = Arc::new(Semaphore::new(4));
            let later = Instant::now() + Duration::from_secs(30);
            let (first, mut first_rx, _a) = pending(&capacity);
            let (expired, mut expired_rx, _b) = pending(&capacity);
            let (cancelled, mut cancelled_rx, _c) = pending(&capacity);
            let mut arguments = Arguments::default();
            arguments.insert("x".into(), Value::from(1));
            let first_id = shared
                .submit(
                    3,
                    arguments,
                    None,
                    Vec::new(),
                    None,
                    later,
                    first,
                    Instant::now(),
                )
                .ok()
                .unwrap();
            shared
                .submit(
                    4,
                    Arguments::default(),
                    None,
                    Vec::new(),
                    None,
                    Instant::now() - Duration::from_millis(1),
                    expired,
                    Instant::now(),
                )
                .ok()
                .unwrap();
            let cancelled_id = shared
                .submit(
                    5,
                    Arguments::default(),
                    None,
                    Vec::new(),
                    None,
                    later,
                    cancelled,
                    Instant::now(),
                )
                .ok()
                .unwrap();
            shared.cancel(cancelled_id);
            // A request whose waiter already gave up never starts either.
            let (abandoned, abandoned_rx, _d) = pending(&capacity);
            shared
                .submit(
                    6,
                    Arguments::default(),
                    None,
                    Vec::new(),
                    None,
                    later,
                    abandoned,
                    Instant::now(),
                )
                .ok()
                .unwrap();
            drop(abandoned_rx);
            // Only the transition from empty wrote a wake-up byte.
            assert_eq!(queued_bytes(&shared), 1);
            assert_eq!(capacity.available_permits(), 0);

            let (jobs, cancels) = bridge.drain(py).unwrap();
            assert!(cancels.is_empty());
            assert_eq!(jobs.len(), 1);
            let job = jobs.get_item(0).unwrap();
            let ticket = job.get_item(0).unwrap();
            assert_eq!(
                ticket.getattr("id").unwrap().extract::<u64>().unwrap(),
                first_id
            );
            assert_eq!(job.get_item(1).unwrap().extract::<usize>().unwrap(), 3);
            assert_eq!(
                job.get_item(2)
                    .unwrap()
                    .get_item("x")
                    .unwrap()
                    .extract::<i64>()
                    .unwrap(),
                1
            );
            // The expired job was answered and the cancelled one released.
            assert_eq!(
                expired_rx.try_recv().unwrap().into_response().status(),
                StatusCode::GATEWAY_TIMEOUT
            );
            assert!(cancelled_rx.try_recv().is_err());
            assert_eq!(capacity.available_permits(), 3);

            // A cancel for a running job is handed to Python.
            shared.cancel(first_id);
            let (jobs, cancels) = bridge.drain(py).unwrap();
            assert!(jobs.is_empty());
            assert_eq!(cancels.extract::<Vec<u64>>().unwrap(), vec![first_id]);

            // Completion decodes the result and releases admission first.
            let done = py
                .eval(c"__import__('asyncio').Future", None, None)
                .unwrap();
            let loop_ = py
                .eval(c"__import__('asyncio').new_event_loop()", None, None)
                .unwrap();
            let future = done
                .call((), Some(&[("loop", &loop_)].into_py_dict(py).unwrap()))
                .unwrap();
            future
                .call_method1(
                    "set_result",
                    ((200, [("ok", true)].into_py_dict(py).unwrap()),),
                )
                .unwrap();
            ticket.call_method1("complete", (&future,)).unwrap();
            assert_eq!(
                first_rx.try_recv().unwrap().into_response().status(),
                StatusCode::OK
            );
            assert_eq!(capacity.available_permits(), 4);
            loop_.call_method0("close").unwrap();
        });
    }

    #[test]
    fn closing_answers_queued_jobs_and_rejects_new_ones() {
        let bridge = Bridge::new(None).unwrap();
        let shared = bridge.shared.clone();
        let capacity = Arc::new(Semaphore::new(2));
        let later = Instant::now() + Duration::from_secs(30);
        let (queued, mut queued_rx, _a) = pending(&capacity);
        shared
            .submit(
                0,
                Arguments::default(),
                None,
                Vec::new(),
                None,
                later,
                queued,
                Instant::now(),
            )
            .ok()
            .unwrap();
        bridge.close();
        assert_eq!(
            queued_rx.try_recv().unwrap().into_response().status(),
            StatusCode::INTERNAL_SERVER_ERROR
        );
        let (late, _late_rx, _b) = pending(&capacity);
        assert!(
            shared
                .submit(
                    0,
                    Arguments::default(),
                    None,
                    Vec::new(),
                    None,
                    later,
                    late,
                    Instant::now()
                )
                .is_err()
        );
        assert_eq!(capacity.available_permits(), 2);
    }

    #[tokio::test(start_paused = true)]
    async fn congestion_skips_only_delayed_lanes_and_recovers() {
        let first = Bridge::new(None).unwrap().shared.clone();
        let second = Bridge::new(None).unwrap().shared.clone();
        let lanes = Lanes::new(vec![first.clone(), second.clone()]);
        lanes.enable_adaptive(Duration::from_secs(30));
        let queued = Instant::now();
        tokio::time::advance(Duration::from_millis(101)).await;
        first
            .delay
            .observe(first.delay.deadline(queued), Instant::now());
        tokio::time::advance(Duration::from_millis(101)).await;
        first
            .delay
            .observe(first.delay.deadline(queued), Instant::now());
        assert!(!lanes.overloaded());
        for _ in 0..4 {
            assert!(Arc::ptr_eq(lanes.choose().unwrap(), &second));
        }
        second
            .delay
            .observe(second.delay.deadline(queued), Instant::now());
        tokio::time::advance(Duration::from_millis(101)).await;
        first
            .delay
            .observe(first.delay.deadline(queued), Instant::now());
        second
            .delay
            .observe(second.delay.deadline(queued), Instant::now());
        assert!(lanes.overloaded());
        tokio::time::advance(Duration::from_millis(20)).await;
        assert!(!lanes.overloaded());
        assert!(Arc::ptr_eq(lanes.choose().unwrap(), &first));
    }

    #[test]
    fn requests_go_to_the_least_loaded_loop_until_admission_ends() {
        let shared: Vec<_> = (0..3)
            .map(|_| Bridge::new(None).unwrap().shared.clone())
            .collect();
        let lanes = Lanes::new(shared.clone());
        let capacity = Arc::new(Semaphore::new(8));
        let index = |chosen: &Arc<Shared>| {
            shared
                .iter()
                .position(|lane| Arc::ptr_eq(lane, chosen))
                .unwrap()
        };
        // Equal loads rotate, so idle loops share work evenly.
        let mut held = Vec::new();
        let mut seen = Vec::new();
        for _ in 0..3 {
            let lane = lanes.choose().unwrap();
            seen.push(index(lane));
            let (mut pending, _rx, ()) = pending(&capacity);
            lane.enter(&mut pending.admission);
            held.push(pending);
        }
        seen.sort_unstable();
        assert_eq!(seen, [0, 1, 2]);
        // A busy loop is skipped while it holds more admitted requests.
        let busy = index(lanes.choose().unwrap());
        let (mut extra, _rx, ()) = pending(&capacity);
        shared[busy].enter(&mut extra.admission);
        for _ in 0..4 {
            assert_ne!(index(lanes.choose().unwrap()), busy);
        }
        drop(extra);
        drop(held);
        assert!(
            shared
                .iter()
                .all(|lane| lane.load.load(Ordering::Relaxed) == 0)
        );
        assert!(Lanes::new(Vec::new()).choose().is_none());
    }
}
