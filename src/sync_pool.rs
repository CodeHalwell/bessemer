//! Bounded-count native handler threads. Python work is never run under the
//! queue lock; joining releases the interpreter so running calls can finish.
use pyo3::{exceptions::PyRuntimeError, prelude::*};
use std::{
    collections::VecDeque,
    sync::atomic::{AtomicUsize, Ordering},
    sync::{Arc, Condvar, Mutex},
    thread::JoinHandle,
};

struct Job {
    call: Py<PyAny>,
    future: Option<Py<PyAny>>,
}
#[derive(Default)]
struct Queue {
    jobs: VecDeque<Job>,
    closed: bool,
}
#[derive(Default)]
struct Shared {
    queue: Mutex<Queue>,
    ready: Condvar,
    live: AtomicUsize,
    worker_ids: Mutex<Vec<std::thread::ThreadId>>,
}
#[pyclass(frozen, module = "bessemer._native")]
pub(crate) struct SyncPool {
    shared: Arc<Shared>,
    threads: Mutex<Vec<JoinHandle<()>>>,
}
#[pymethods]
impl SyncPool {
    #[new]
    fn new(workers: usize) -> PyResult<Self> {
        if !(1..=256).contains(&workers) {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "invalid sync worker count",
            ));
        }
        let pool = Self {
            shared: Arc::new(Shared::default()),
            threads: Mutex::new(Vec::new()),
        };
        for index in 0..workers {
            let shared = pool.shared.clone();
            let thread = std::thread::Builder::new()
                .name(format!("bessemer-handler-{index}"))
                .spawn(move || {
                    shared.live.fetch_add(1, Ordering::Release);
                    Python::attach(|py| {
                        loop {
                            let job = py.detach(|| {
                                let mut queue = shared.queue.lock().unwrap();
                                while queue.jobs.is_empty() && !queue.closed {
                                    queue = shared.ready.wait(queue).unwrap();
                                }
                                queue.jobs.pop_front()
                            });
                            let Some(job) = job else {
                                return;
                            };
                            if let Err(error) = job.call.bind(py).call0() {
                                error.write_unraisable(py, Some(job.call.bind(py)));
                            }
                            drop(job);
                        }
                    });
                    shared.live.fetch_sub(1, Ordering::Release);
                })
                .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
            pool.shared
                .worker_ids
                .lock()
                .unwrap()
                .push(thread.thread().id());
            pool.threads.lock().unwrap().push(thread);
        }
        Ok(pool)
    }

    #[getter]
    fn threads_alive(&self) -> usize {
        self.shared.live.load(Ordering::Acquire)
    }

    #[pyo3(signature = (call, future=None))]
    fn execute(&self, call: Py<PyAny>, future: Option<Py<PyAny>>) -> PyResult<()> {
        let mut queue = self.shared.queue.lock().unwrap();
        if queue.closed {
            return Err(PyRuntimeError::new_err(
                "cannot schedule new futures after shutdown",
            ));
        }
        queue.jobs.push_back(Job { call, future });
        self.shared.ready.notify_one();
        Ok(())
    }

    #[pyo3(signature = (wait=true, cancel_futures=false))]
    fn shutdown(&self, py: Python<'_>, wait: bool, cancel_futures: bool) -> PyResult<()> {
        let cancelled = {
            let mut queue = self.shared.queue.lock().unwrap();
            queue.closed = true;
            let mut cancelled = Vec::new();
            if cancel_futures {
                let jobs = std::mem::take(&mut queue.jobs);
                for job in jobs {
                    if job.future.is_some() {
                        cancelled.push(job);
                    } else {
                        queue.jobs.push_back(job);
                    }
                }
            }
            self.shared.ready.notify_all();
            cancelled
        };
        for job in cancelled {
            if let Some(future) = &job.future {
                future.bind(py).call_method0("cancel")?;
            }
        }
        if wait {
            if self
                .shared
                .worker_ids
                .lock()
                .unwrap()
                .contains(&std::thread::current().id())
            {
                return Err(PyRuntimeError::new_err("cannot join current thread"));
            }
            py.detach(|| {
                let mut threads = self.threads.lock().unwrap();
                for thread in threads.drain(..) {
                    let _ = thread.join();
                }
            });
        }
        Ok(())
    }
}
impl Drop for SyncPool {
    fn drop(&mut self) {
        let mut queue = self.shared.queue.lock().unwrap();
        queue.closed = true;
        self.shared.ready.notify_all();
    }
}
