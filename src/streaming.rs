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
    Unavailable,
}

#[cfg(test)]
thread_local! {
    static COMPLETION_CHANNELS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

#[inline]
fn completion_channel() -> (oneshot::Sender<Chunk>, oneshot::Receiver<Chunk>) {
    #[cfg(test)]
    COMPLETION_CHANNELS.with(|count| count.set(count.get() + 1));
    oneshot::channel()
}

fn request_chunk(handle: &Py<PyAny>) -> Requested {
    Python::attach(|py| {
        let future = match handle.bind(py).call_method0(intern!(py, "next")) {
            Ok(future) => future,
            Err(_) => return Requested::Unavailable,
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
        let (sender, receiver) = completion_channel();
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
    mut first: Option<Requested>,
) {
    let failure = |detail: &str| std::io::Error::other(detail.to_owned());
    loop {
        let requested = first
            .take()
            .unwrap_or_else(|| request_chunk(&stream.handle));
        let chunk = match requested {
            Requested::Unavailable => {
                let _ = sender
                    .send(Err(failure("streaming handler unavailable")))
                    .await;
                break;
            }
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
pub(crate) fn start(mut stream: Stream, budget: ByteBudget, idle: Duration) -> ChannelBody {
    // Capacity 1 lets the next chunk be produced while hyper writes this one,
    // while still bounding retained data to two chunks per stream.
    let (sender, receiver) = mpsc::channel(1);
    // The first chunk is requested here rather than by the pump task, so a
    // chunk the stream has ready (on the loop's own thread, an iterator that
    // did not suspend) is in the channel when the writer sends the head, and
    // a stream that ends with it needs no pump at all.
    let first = request_chunk(&stream.handle);
    let first = match first {
        Requested::Ready(Ok(Step::End)) => {
            stream.finished = true;
            return ChannelBody(receiver);
        }
        Requested::Ready(Ok(step @ (Step::Data(_) | Step::Last(_)))) => {
            let (data, last) = match step {
                Step::Data(data) => (data, false),
                Step::Last(data) => (data, true),
                Step::End => unreachable!(),
            };
            match budget.acquire(data.len()) {
                Ok(permit) if !data.is_empty() => {
                    let bytes = ByteBudget::own_reserved(data, permit);
                    // A fresh channel of capacity 1 has room for one frame.
                    let _ = sender.try_send(Ok(Frame::data(bytes)));
                    if last {
                        stream.finished = true;
                        return ChannelBody(receiver);
                    }
                    None
                }
                // Empty data, or a budget that is full for now: the pump
                // handles it as it would have.
                _ => Some(Requested::Ready(Ok(if last {
                    Step::Last(data)
                } else {
                    Step::Data(data)
                }))),
            }
        }
        other => Some(other),
    };
    tokio::spawn(pump(stream, sender, budget, idle, first));
    ChannelBody(receiver)
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyo3::types::PyModule;
    use std::{ffi::CString, hint::black_box, time::Instant};

    fn fixtures(py: Python<'_>) -> Bound<'_, PyModule> {
        let source = CString::new(
            r#"
class Future:
    def __init__(self, value=None, *, done=True, error=False):
        self.value, self.finished, self.error = value, done, error
        self.callback = None
    def done(self):
        return self.finished
    def result(self):
        if self.error:
            raise RuntimeError('step failed')
        return self.value
    def add_done_callback(self, callback):
        if self.finished:
            callback(self)
        else:
            self.callback = callback
    def complete(self):
        self.finished = True
        self.callback(self)

class Handle:
    def __init__(self, future):
        self.future, self.closed = future, 0
    def next(self):
        return self.future
    def close(self):
        self.closed += 1
        return Future()

class Unavailable(Handle):
    def next(self):
        raise RuntimeError('submission failed')

class Rejected(Future):
    def add_done_callback(self, callback):
        raise RuntimeError('callback registration failed')

raw = Handle(b'data')
done = Handle(Future(b'data'))
last = Handle(Future((b'data',)))
end = Handle(Future())
error = Handle(Future(error=True))
unavailable = Unavailable(None)
pending = Handle(Future(b'data', done=False))
rejected = Handle(Rejected(done=False))
"#,
        )
        .unwrap();
        PyModule::from_code(py, &source, c"streaming_fixture.py", c"streaming_fixture").unwrap()
    }

    #[test]
    #[ignore = "microprofile: run separately from throughput measurements"]
    fn profile_stream_steps() {
        Python::attach(|py| {
            let module = fixtures(py);
            let iterations = 100_000;
            for name in ["raw", "done", "last", "end", "error", "unavailable"] {
                let handle = module.getattr(name).unwrap().unbind();
                for _ in 0..1000 {
                    drop(request_chunk(&handle));
                }
                for sample in 0..3 {
                    COMPLETION_CHANNELS.with(|count| count.set(0));
                    let started = Instant::now();
                    for _ in 0..iterations {
                        drop(black_box(request_chunk(black_box(&handle))));
                    }
                    let ns = started.elapsed().as_nanos() as f64 / iterations as f64;
                    let channels = COMPLETION_CHANNELS.with(|count| count.get());
                    println!(
                        "stream-step case={name} sample={sample} iterations={iterations} completion_channels={channels} ns_per_step={ns:.2}"
                    );
                }
            }
        });
    }

    #[test]
    fn immediate_steps_do_not_allocate_completion_channels() {
        Python::attach(|py| {
            let module = fixtures(py);
            COMPLETION_CHANNELS.with(|count| count.set(0));
            for name in ["raw", "done", "last"] {
                let handle = module.getattr(name).unwrap().unbind();
                match request_chunk(&handle) {
                    Requested::Ready(Ok(Step::Data(data) | Step::Last(data))) => {
                        assert_eq!(data.as_ref(), b"data");
                    }
                    _ => panic!("expected an immediate data step"),
                }
            }
            let end = module.getattr("end").unwrap().unbind();
            assert!(matches!(
                request_chunk(&end),
                Requested::Ready(Ok(Step::End))
            ));
            let error = module.getattr("error").unwrap().unbind();
            assert!(matches!(request_chunk(&error), Requested::Ready(Err(()))));
            let unavailable = module.getattr("unavailable").unwrap().unbind();
            assert!(matches!(
                request_chunk(&unavailable),
                Requested::Unavailable
            ));
            assert_eq!(COMPLETION_CHANNELS.with(|count| count.get()), 0);
        });
    }

    #[test]
    fn pending_step_uses_one_channel_and_settles_only_once() {
        Python::attach(|py| {
            let module = fixtures(py);
            let handle = module.getattr("pending").unwrap().unbind();
            COMPLETION_CHANNELS.with(|count| count.set(0));
            let Requested::Pending(mut receiver) = request_chunk(&handle) else {
                panic!("expected a pending step");
            };
            assert_eq!(COMPLETION_CHANNELS.with(|count| count.get()), 1);
            assert!(matches!(
                receiver.try_recv(),
                Err(oneshot::error::TryRecvError::Empty)
            ));
            let future = handle.bind(py).getattr("future").unwrap();
            future.call_method0("complete").unwrap();
            let Ok(Ok(Step::Data(data))) = receiver.try_recv() else {
                panic!("expected the settled data");
            };
            assert_eq!(data.as_ref(), b"data");
            future.call_method0("complete").unwrap();
        });
    }

    #[test]
    fn rejected_pending_callback_still_reports_unavailability() {
        Python::attach(|py| {
            let module = fixtures(py);
            let handle = module.getattr("rejected").unwrap().unbind();
            let Requested::Pending(mut receiver) = request_chunk(&handle) else {
                panic!("expected a failed pending registration");
            };
            assert!(matches!(
                receiver.try_recv(),
                Err(oneshot::error::TryRecvError::Closed)
            ));
        });
    }

    #[tokio::test]
    async fn submission_failure_still_closes_the_iterator() {
        let handle = Python::attach(|py| fixtures(py).getattr("unavailable").unwrap().unbind());
        let stream = Stream {
            handle: Python::attach(|py| handle.clone_ref(py)),
            admission: None,
            finished: false,
        };
        let (sender, mut receiver) = mpsc::channel(1);
        pump(
            stream,
            sender,
            ByteBudget::new(8),
            Duration::from_secs(1),
            None,
        )
        .await;
        let error = receiver.recv().await.unwrap().unwrap_err();
        assert_eq!(error.to_string(), "streaming handler unavailable");
        Python::attach(|py| {
            assert_eq!(
                handle
                    .bind(py)
                    .getattr("closed")
                    .unwrap()
                    .extract::<usize>()
                    .unwrap(),
                1
            );
        });
    }

    #[tokio::test]
    async fn settled_failure_does_not_close_an_already_cleaned_up_iterator() {
        let handle = Python::attach(|py| fixtures(py).getattr("error").unwrap().unbind());
        let stream = Stream {
            handle: Python::attach(|py| handle.clone_ref(py)),
            admission: None,
            finished: false,
        };
        let (sender, mut receiver) = mpsc::channel(1);
        pump(
            stream,
            sender,
            ByteBudget::new(8),
            Duration::from_secs(1),
            None,
        )
        .await;
        let error = receiver.recv().await.unwrap().unwrap_err();
        assert_eq!(error.to_string(), "streaming handler failed");
        Python::attach(|py| {
            assert_eq!(
                handle
                    .bind(py)
                    .getattr("closed")
                    .unwrap()
                    .extract::<usize>()
                    .unwrap(),
                0
            );
        });
    }

    #[tokio::test]
    async fn final_data_retains_its_byte_budget_until_transport_drop() {
        let handle = Python::attach(|py| fixtures(py).getattr("last").unwrap().unbind());
        let stream = Stream {
            handle: Python::attach(|py| handle.clone_ref(py)),
            admission: None,
            finished: false,
        };
        let (sender, mut receiver) = mpsc::channel(1);
        let budget = ByteBudget::new(8);
        pump(stream, sender, budget.clone(), Duration::from_secs(1), None).await;
        let frame = receiver.recv().await.unwrap().unwrap();
        assert_eq!(frame.data_ref().unwrap().as_ref(), b"data");
        assert_eq!(budget.available(), 4);
        drop(frame);
        assert_eq!(budget.available(), 8);
        Python::attach(|py| {
            assert_eq!(
                handle
                    .bind(py)
                    .getattr("closed")
                    .unwrap()
                    .extract::<usize>()
                    .unwrap(),
                0
            );
        });
    }

    #[tokio::test]
    async fn disconnected_pending_reader_closes_before_a_late_callback() {
        let handle = Python::attach(|py| fixtures(py).getattr("pending").unwrap().unbind());
        let stream = Stream {
            handle: Python::attach(|py| handle.clone_ref(py)),
            admission: None,
            finished: false,
        };
        let (sender, receiver) = mpsc::channel(1);
        drop(receiver);
        pump(
            stream,
            sender,
            ByteBudget::new(8),
            Duration::from_secs(1),
            None,
        )
        .await;
        Python::attach(|py| {
            assert_eq!(
                handle
                    .bind(py)
                    .getattr("closed")
                    .unwrap()
                    .extract::<usize>()
                    .unwrap(),
                1
            );
            handle
                .bind(py)
                .getattr("future")
                .unwrap()
                .call_method0("complete")
                .unwrap();
        });
    }
}
