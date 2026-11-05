//! Streaming response bodies driven from Tokio.
//!
//! The worker returns a stream handle instead of a buffered body. A pump task
//! pulls one chunk at a time from the Python iterator (with one chunk of
//! prefetch through a bounded channel), charges each chunk to the response byte
//! budget and forwards it to hyper. Request admission is held until the Python
//! iterator and its dependency cleanup have actually finished, including after
//! a client disconnect, an idle timeout or a HEAD request that never polls.

use crate::budget::ByteBudget;
use axum::body::Bytes;
use http_body::{Body as HttpBody, Frame};
use pyo3::{
    intern,
    prelude::*,
    pybacked::PyBackedBytes,
    types::{PyBytes, PyCFunction, PyDict, PyTuple},
};
use std::{
    pin::Pin,
    sync::{Arc, Mutex},
    task::{Context, Poll},
    time::Duration,
};
use tokio::sync::{mpsc, oneshot};

use crate::Admission;

/// One step of the Python iterator, as `_ResponseStream._next` reports it.
enum Step {
    Data(PyBackedBytes),
    /// The last data; the stream finished its cleanup before reporting it.
    Last(PyBackedBytes),
    End,
}

type Chunk = Result<Step, ()>;

/// The response extension carrying an unstarted stream from the worker callback
/// to the Tokio request task.
#[derive(Clone)]
pub(crate) struct StreamStart(Arc<Mutex<Option<Stream>>>);

pub(crate) struct Stream {
    handle: Py<PyAny>,
    admission: Option<Admission>,
    finished: bool,
}

impl StreamStart {
    pub(crate) fn new(handle: Py<PyAny>, admission: Admission) -> Self {
        Self(Arc::new(Mutex::new(Some(Stream {
            handle,
            admission: Some(admission),
            finished: false,
        }))))
    }

    pub(crate) fn take(&self) -> Option<Stream> {
        self.0.lock().unwrap().take()
    }
}

impl Drop for Stream {
    fn drop(&mut self) {
        let admission = self.admission.take();
        if self.finished {
            // Python completed its own cleanup before reporting the end.
            drop(admission);
            return;
        }
        // Ask the worker to close the iterator; capacity returns only after the
        // generator's finalizers and dependency cleanup complete.
        Python::attach(|py| {
            let held = Mutex::new(admission);
            let callback = PyCFunction::new_closure(
                py,
                None,
                None,
                move |_: &Bound<'_, PyTuple>, _: Option<&Bound<'_, PyDict>>| -> PyResult<()> {
                    held.lock().unwrap().take();
                    Ok(())
                },
            );
            let scheduled = callback.and_then(|callback| {
                self.handle
                    .bind(py)
                    .call_method0("close")?
                    .call_method1("add_done_callback", (callback,))
            });
            if scheduled.is_err() {
                diagnostic!("Bessemer could not close a streaming response iterator");
            }
        });
    }
}

/// Read a settled step future: `None` ends the stream, a 1-tuple carries the
/// last data of a stream that has already finished, bytes are a chunk.
fn read_step(future: &Bound<'_, PyAny>) -> Chunk {
    let value = future
        .call_method0(intern!(future.py(), "result"))
        .map_err(|_| ())?;
    if value.is_none() {
        return Ok(Step::End);
    }
    if let Ok(last) = value.cast::<PyTuple>() {
        let data = last.get_item(0).map_err(|_| ())?;
        return data.extract().map(Step::Last).map_err(|_| ());
    }
    value.extract().map(Step::Data).map_err(|_| ())
}

/// The done callback of a step future that was still running when requested.
#[pyclass(frozen, module = "bessemer._native")]
struct StepDone(Mutex<Option<oneshot::Sender<Chunk>>>);

#[pymethods]
impl StepDone {
    fn __call__(&self, future: &Bound<'_, PyAny>) {
        if let Some(sender) = self.0.lock().unwrap().take() {
            let _ = sender.send(read_step(future));
        }
    }
}

enum Requested {
    /// The step settled while it was requested (a chunk already buffered, or
    /// an iterator that did not suspend, on the loop's own thread).
    Ready(Chunk),
    Pending(oneshot::Receiver<Chunk>),
}

fn request_chunk(handle: &Py<PyAny>) -> Requested {
    let (sender, receiver) = oneshot::channel();
    Python::attach(|py| {
        let future = match handle.bind(py).call_method0(intern!(py, "next")) {
            Ok(future) => future,
            // A failed submission drops the sender, which the pump reads as
            // an error.
            Err(_) => return Requested::Pending(receiver),
        };
        // Chunks already buffered on the loop's own thread come back as bytes.
        if future.is_instance_of::<PyBytes>() {
            return Requested::Ready(future.extract().map(Step::Data).map_err(|_| ()));
        }
        if future
            .call_method0(intern!(py, "done"))
            .and_then(|done| done.is_truthy())
            .unwrap_or(false)
        {
            return Requested::Ready(read_step(&future));
        }
        let _ = Bound::new(py, StepDone(Mutex::new(Some(sender)))).and_then(|callback| {
            future.call_method1(intern!(py, "add_done_callback"), (callback,))
        });
        Requested::Pending(receiver)
    })
}

/// Pull chunks until the iterator ends, fails, idles out or the client leaves.
async fn pump(
    mut stream: Stream,
    sender: mpsc::Sender<Result<Frame<Bytes>, std::io::Error>>,
    budget: ByteBudget,
    idle: Duration,
) {
    let failure = |detail: &str| std::io::Error::other(detail.to_owned());
    loop {
        let chunk = match request_chunk(&stream.handle) {
            Requested::Ready(chunk) => Ok(Ok(chunk)),
            Requested::Pending(next) => tokio::select! {
                result = tokio::time::timeout(idle, next) => result,
                // Client disconnected while Python was producing the chunk.
                () = sender.closed() => break,
            },
        };
        let mut last = false;
        let data = match chunk {
            Ok(Ok(Ok(Step::Data(data)))) => data,
            Ok(Ok(Ok(Step::Last(data)))) => {
                // Python has finished the stream; nothing is left to close.
                stream.finished = true;
                last = true;
                data
            }
            Ok(Ok(Ok(Step::End))) => {
                stream.finished = true;
                break;
            }
            Ok(Ok(Err(()))) => {
                // The worker has already logged and cleaned up a failed iterator.
                stream.finished = true;
                let _ = sender.send(Err(failure("streaming handler failed"))).await;
                break;
            }
            Ok(Err(_)) => {
                let _ = sender
                    .send(Err(failure("streaming handler unavailable")))
                    .await;
                break;
            }
            Err(_) => {
                let _ = sender
                    .send(Err(failure("streaming response idle timeout")))
                    .await;
                break;
            }
        };
        if data.is_empty() {
            if last {
                break;
            }
            continue;
        }
        let bytes = tokio::select! {
            result = budget.own_waiting(data) => result,
            () = sender.closed() => break,
        };
        let Ok(bytes) = bytes else {
            let _ = sender
                .send(Err(failure("chunk exceeds response byte capacity")))
                .await;
            break;
        };
        if sender.send(Ok(Frame::data(bytes))).await.is_err() || last {
            break;
        }
    }
    drop(stream);
}

/// The hyper-facing body: frames arrive from the pump through a bounded channel.
pub(crate) struct ChannelBody(mpsc::Receiver<Result<Frame<Bytes>, std::io::Error>>);

impl HttpBody for ChannelBody {
    type Data = Bytes;
    type Error = std::io::Error;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, std::io::Error>>> {
        self.0.poll_recv(context)
    }
}

/// Start pumping a stream and return the body that delivers its frames.
pub(crate) fn start(stream: Stream, budget: ByteBudget, idle: Duration) -> ChannelBody {
    // Capacity 1 lets the next chunk be produced while hyper writes this one,
    // while still bounding retained data to two chunks per stream.
    let (sender, receiver) = mpsc::channel(1);
    tokio::spawn(pump(stream, sender, budget, idle));
    ChannelBody(receiver)
}
