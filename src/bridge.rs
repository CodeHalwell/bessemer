//! Native request handoff from Tokio to the Python asyncio worker.
//!
//! Tokio threads never take the GIL on the request path. A request becomes a
//! [`Job`] pushed onto a mutex-protected batch. The first job pushed into an
//! empty batch writes one byte to a socket pair that the asyncio loop watches
//! with `add_reader`. The loop thread then drains every queued job at once,
//! builds the Python arguments itself and starts one task per job. Completion
//! returns through a [`Ticket`] that decodes the handler result and wakes the
//! waiting Tokio future. Cancellation requests travel the same queue.

use crate::{Admission, RequestMetadata, ResponseError, capacity_response, decode_response, error};
use axum::{http::StatusCode, response::Response};
use pyo3::{
    prelude::*,
    types::{PyBytes, PyDict, PyList, PyTuple},
};
use serde_json::{Map, Value};
use std::{
    collections::HashSet,
    io::{ErrorKind, Read, Write},
    os::{fd::AsRawFd, unix::net::UnixStream},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
};
use tokio::{sync::oneshot, time::Instant};

/// Everything needed to publish one response after the handler finishes.
pub(crate) struct Pending {
    pub(crate) sender: oneshot::Sender<Response>,
    pub(crate) admission: Admission,
    pub(crate) head: bool,
    pub(crate) route_id: usize,
    pub(crate) max_response_size: usize,
    pub(crate) response_bytes: crate::budget::ByteBudget,
}

impl Pending {
    /// Decode a completed handler result and publish it.
    pub(crate) fn finish(self, result: PyResult<Bound<'_, PyAny>>) {
        if self.sender.is_closed() {
            return;
        }
        let decoded = result.map_err(ResponseError::from).and_then(|value| {
            decode_response(
                &value,
                self.max_response_size,
                &self.response_bytes,
                self.head,
            )
        });
        let response = match decoded {
            Ok(response) => response,
            Err(ResponseError::Full) => capacity_response("Response byte capacity is full", false),
            Err(ResponseError::Invalid) => {
                eprintln!(
                    "Bessemer could not encode the response for route {}",
                    self.route_id
                );
                error(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error")
            }
        };
        self.respond(response);
    }

    /// Publish a response without running Python work.
    pub(crate) fn respond(self, response: Response) {
        // Publish only after admission is available again. Waking Tokio first can
        // make a subsequent sequential request observe a spurious 503.
        drop(self.admission);
        let _ = self.sender.send(response);
    }
}

pub(crate) struct Job {
    id: u64,
    route_id: usize,
    arguments: Map<String, Value>,
    metadata: Option<RequestMetadata>,
    deadline: Instant,
    pending: Pending,
}

#[derive(Default)]
struct Batch {
    jobs: Vec<Job>,
    cancels: Vec<u64>,
    closed: bool,
}

pub(crate) struct Shared {
    batch: Mutex<Batch>,
    wake_sender: UnixStream,
    wake_receiver: UnixStream,
    next_id: AtomicU64,
}

impl Shared {
    fn wake(&self) {
        // A full socket buffer already guarantees a pending wake-up.
        let _ = (&self.wake_sender).write(&[1]);
    }

    pub(crate) fn submit(
        &self,
        route_id: usize,
        arguments: Map<String, Value>,
        metadata: Option<RequestMetadata>,
        deadline: Instant,
        pending: Pending,
    ) -> Result<u64, Pending> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let job = Job {
            id,
            route_id,
            arguments,
            metadata,
            deadline,
            pending,
        };
        let mut batch = self.batch.lock().unwrap();
        if batch.closed {
            return Err(job.pending);
        }
        let was_empty = batch.jobs.is_empty() && batch.cancels.is_empty();
        batch.jobs.push(job);
        drop(batch);
        if was_empty {
            self.wake();
        }
        Ok(id)
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

    fn clear_wake(&self) {
        let mut buffer = [0u8; 256];
        loop {
            match (&self.wake_receiver).read(&mut buffer) {
                Ok(0) => return,
                Ok(_) => continue,
                Err(error) if error.kind() == ErrorKind::Interrupted => continue,
                Err(_) => return,
            }
        }
    }

    fn take(&self) -> (Vec<Job>, Vec<u64>) {
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

/// Build the keyword arguments for one handler call on the loop thread.
pub(crate) fn python_arguments<'py>(
    py: Python<'py>,
    argument_values: &Map<String, Value>,
    metadata: Option<RequestMetadata>,
) -> PyResult<Bound<'py, PyDict>> {
    let arguments = PyDict::new(py);
    for (key, value) in argument_values {
        arguments.set_item(key, crate::conversion::json_to_python(py, value)?)?;
    }
    if let Some(metadata) = metadata {
        let data = PyDict::new(py);
        data.set_item("method", metadata.method)?;
        data.set_item("path", metadata.path)?;
        data.set_item("query_string", metadata.query_string)?;
        data.set_item("headers", metadata.headers)?;
        data.set_item("client", metadata.client)?;
        data.set_item("scheme", "http")?;
        data.set_item("body", PyBytes::new(py, &metadata.body))?;
        data.set_item("request_id", metadata.request_id)?;
        data.set_item("path_params", metadata.path_params)?;
        data.set_item("cookies", metadata.cookies)?;
        arguments.set_item("__bessemer_request__", data)?;
    }
    Ok(arguments)
}

/// The native queue shared by the HTTP server and one asyncio worker.
#[pyclass(frozen, module = "bessemer._native")]
pub(crate) struct Bridge {
    pub(crate) shared: Arc<Shared>,
}

#[pymethods]
impl Bridge {
    #[new]
    fn new() -> PyResult<Self> {
        let (wake_sender, wake_receiver) = UnixStream::pair()?;
        wake_sender.set_nonblocking(true)?;
        wake_receiver.set_nonblocking(true)?;
        Ok(Self {
            shared: Arc::new(Shared {
                batch: Mutex::new(Batch::default()),
                wake_sender,
                wake_receiver,
                next_id: AtomicU64::new(0),
            }),
        })
    }

    /// The descriptor the asyncio loop watches for queued work.
    fn fileno(&self) -> i32 {
        self.shared.wake_receiver.as_raw_fd()
    }

    /// Take all queued work.
    ///
    /// Returns `(jobs, cancels)`: each job is `(ticket, route_id, arguments)`,
    /// and `cancels` lists ids of previously drained jobs to cancel. Jobs that
    /// expired or were cancelled while queued are answered here and never
    /// start Python work.
    fn drain<'py>(&self, py: Python<'py>) -> PyResult<(Bound<'py, PyList>, Bound<'py, PyList>)> {
        let (jobs, cancels) = self.shared.take();
        let mut cancelled: HashSet<u64> = cancels.into_iter().collect();
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
            let arguments = match python_arguments(py, &job.arguments, job.metadata) {
                Ok(arguments) => arguments,
                Err(_) => {
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
    use std::time::Duration;
    use tokio::sync::Semaphore;

    fn pending(capacity: &Arc<Semaphore>) -> (Pending, oneshot::Receiver<Response>, ()) {
        let (sender, receiver) = oneshot::channel();
        let admission = Admission {
            _request: capacity.clone().try_acquire_owned().unwrap(),
            _input: crate::budget::BytePermit::default(),
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
    fn wake_ups_coalesce_and_drain_answers_expired_and_cancelled_jobs() {
        Python::attach(|py| {
            let bridge = Bridge::new().unwrap();
            let shared = bridge.shared.clone();
            let capacity = Arc::new(Semaphore::new(4));
            let later = Instant::now() + Duration::from_secs(30);
            let (first, mut first_rx, _a) = pending(&capacity);
            let (expired, mut expired_rx, _b) = pending(&capacity);
            let (cancelled, mut cancelled_rx, _c) = pending(&capacity);
            let mut arguments = Map::new();
            arguments.insert("x".into(), Value::from(1));
            let first_id = shared
                .submit(3, arguments, None, later, first)
                .ok()
                .unwrap();
            shared
                .submit(
                    4,
                    Map::new(),
                    None,
                    Instant::now() - Duration::from_millis(1),
                    expired,
                )
                .ok()
                .unwrap();
            let cancelled_id = shared
                .submit(5, Map::new(), None, later, cancelled)
                .ok()
                .unwrap();
            shared.cancel(cancelled_id);
            // A request whose waiter already gave up never starts either.
            let (abandoned, abandoned_rx, _d) = pending(&capacity);
            shared
                .submit(6, Map::new(), None, later, abandoned)
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
                expired_rx.try_recv().unwrap().status(),
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
            assert_eq!(first_rx.try_recv().unwrap().status(), StatusCode::OK);
            assert_eq!(capacity.available_permits(), 4);
            loop_.call_method0("close").unwrap();
        });
    }

    #[test]
    fn closing_answers_queued_jobs_and_rejects_new_ones() {
        let bridge = Bridge::new().unwrap();
        let shared = bridge.shared.clone();
        let capacity = Arc::new(Semaphore::new(2));
        let later = Instant::now() + Duration::from_secs(30);
        let (queued, mut queued_rx, _a) = pending(&capacity);
        shared
            .submit(0, Map::new(), None, later, queued)
            .ok()
            .unwrap();
        bridge.close();
        assert_eq!(
            queued_rx.try_recv().unwrap().status(),
            StatusCode::INTERNAL_SERVER_ERROR
        );
        let (late, _late_rx, _b) = pending(&capacity);
        assert!(shared.submit(0, Map::new(), None, later, late).is_err());
        assert_eq!(capacity.available_permits(), 2);
    }
}
