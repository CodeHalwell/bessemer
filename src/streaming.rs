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
    prelude::*,
    pybacked::PyBackedBytes,
    types::{PyCFunction, PyDict, PyTuple},
};
use std::{
    pin::Pin,
    sync::{Arc, Mutex},
    task::{Context, Poll},
    time::Duration,
};
use tokio::sync::{mpsc, oneshot};

use crate::Admission;
type Chunk = Result<Option<PyBackedBytes>, ()>;

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
                eprintln!("Bessemer could not close a streaming response iterator");
            }
        });
    }
}

fn request_chunk(handle: &Py<PyAny>) -> oneshot::Receiver<Chunk> {
    let (sender, receiver) = oneshot::channel();
    Python::attach(|py| {
        let sender = Mutex::new(Some(sender));
        let callback = PyCFunction::new_closure(
            py,
            None,
            None,
            move |args: &Bound<'_, PyTuple>, _: Option<&Bound<'_, PyDict>>| -> PyResult<()> {
                let Some(sender) = sender.lock().unwrap().take() else {
                    return Ok(());
                };
                let chunk = args
                    .get_item(0)
                    .and_then(|future| future.call_method0("result"))
                    .and_then(|value| {
                        if value.is_none() {
                            Ok(None)
                        } else {
                            Ok(Some(value.extract::<PyBackedBytes>()?))
                        }
                    })
                    .map_err(|_| ());
                let _ = sender.send(chunk);
                Ok(())
            },
        );
        // A failed submission drops the sender, which the pump reads as an error.
        let _ = callback.and_then(|callback| {
            handle
                .bind(py)
                .call_method0("next")?
                .call_method1("add_done_callback", (callback,))
        });
    });
    receiver
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
        let next = request_chunk(&stream.handle);
        let chunk = tokio::select! {
            result = tokio::time::timeout(idle, next) => result,
            // Client disconnected while Python was producing the chunk.
            () = sender.closed() => break,
        };
        let data = match chunk {
            Ok(Ok(Ok(Some(data)))) => data,
            Ok(Ok(Ok(None))) => {
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
        if sender.send(Ok(Frame::data(bytes))).await.is_err() {
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
