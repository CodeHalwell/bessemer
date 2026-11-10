//! Bounded-count native handler threads. Python work is never run under the
//! queue lock; joining releases the interpreter so running calls can finish.
//! Threads start as work needs them, up to the limit, as in
//! `concurrent.futures.ThreadPoolExecutor`.
#[cfg(any(target_os = "linux", target_os = "macos"))]
use bessemer_ffi::process_cpu_time;
use pyo3::{exceptions::PyRuntimeError, prelude::*};
use std::{
    collections::VecDeque,
    sync::atomic::{AtomicUsize, Ordering},
    sync::{Arc, Condvar, Mutex},
    thread::JoinHandle,
    time::{Duration, Instant},
};

const ADAPTIVE_BACKLOG: usize = 4;
const ADAPTIVE_DELAY: Duration = Duration::from_millis(10);
const CPU_WINDOW: Duration = Duration::from_millis(20);

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
fn process_cpu_time() -> Option<Duration> {
    None
}

struct Adaptive {
    base: usize,
    backlog_since: Option<Instant>,
    cpu_baseline: Option<(Instant, Duration)>,
    cpu_unavailable: bool,
    last_growth: Option<Instant>,
}

impl Adaptive {
    fn note_drain(&mut self, backlog: usize) {
        if backlog < ADAPTIVE_BACKLOG {
            self.backlog_since = None;
            self.cpu_baseline = None;
        }
    }

    fn should_check_cpu(
        &mut self,
        backlog: usize,
        spawned: usize,
        ceiling: usize,
        now: Instant,
    ) -> bool {
        self.note_drain(backlog);
        if self.cpu_unavailable
            || backlog < ADAPTIVE_BACKLOG
            || spawned < self.base
            || spawned >= ceiling
        {
            return false;
        }
        let since = *self.backlog_since.get_or_insert(now);
        if now.duration_since(since) < ADAPTIVE_DELAY {
            return false;
        }
        self.cpu_baseline
            .is_none_or(|(sampled, _)| now.duration_since(sampled) >= CPU_WINDOW)
    }

    fn observe_cpu(&mut self, now: Instant, cpu: Option<Duration>) -> bool {
        let Some(cpu) = cpu else {
            self.cpu_unavailable = true;
            self.cpu_baseline = None;
            return false;
        };
        let previous = self.cpu_baseline.replace((now, cpu));
        let Some((sampled, before)) = previous else {
            return false;
        };
        let Some(used) = cpu.checked_sub(before) else {
            self.cpu_unavailable = true;
            return false;
        };
        let wall = now.duration_since(sampled);
        let below_limit = used.as_nanos() * 4 < wall.as_nanos() * 3;
        let spaced = self
            .last_growth
            .is_none_or(|last| now.duration_since(last) >= CPU_WINDOW);
        if below_limit && spaced {
            self.last_growth = Some(now);
            return true;
        }
        false
    }

    fn next_check_delay(&self, now: Instant) -> Duration {
        let backlog = self.backlog_since.map_or(ADAPTIVE_DELAY, |since| {
            (since + ADAPTIVE_DELAY).saturating_duration_since(now)
        });
        let cpu = self.cpu_baseline.map_or(Duration::ZERO, |(sampled, _)| {
            (sampled + CPU_WINDOW).saturating_duration_since(now)
        });
        backlog.max(cpu)
    }

    fn needs_observer(
        &mut self,
        backlog: usize,
        spawned: usize,
        ceiling: usize,
        closed: bool,
    ) -> bool {
        self.note_drain(backlog);
        !closed
            && !self.cpu_unavailable
            && backlog >= ADAPTIVE_BACKLOG
            && spawned >= self.base
            && spawned < ceiling
    }
}

struct Job {
    call: Py<PyAny>,
    future: Option<Py<PyAny>>,
}
#[derive(Default)]
struct Queue {
    jobs: VecDeque<Job>,
    closed: bool,
    /// Threads waiting for a job.
    idle: usize,
    /// Threads started so far; never more than the pool's limit.
    spawned: usize,
    adaptive: Option<Adaptive>,
    observer_running: bool,
}

impl Queue {
    fn should_spawn(&mut self, ceiling: usize) -> bool {
        let backlog = self.jobs.len().saturating_sub(self.idle);
        let base = self.adaptive.as_ref().map_or(ceiling, |policy| policy.base);
        if backlog > 0 && self.spawned < base {
            return true;
        }
        let Some(policy) = &mut self.adaptive else {
            return false;
        };
        if backlog < ADAPTIVE_BACKLOG {
            policy.note_drain(backlog);
            return false;
        }
        let now = Instant::now();
        policy.should_check_cpu(backlog, self.spawned, ceiling, now)
            && policy.observe_cpu(now, process_cpu_time())
    }

    fn adaptive_backlog(&mut self, ceiling: usize) -> Option<usize> {
        let backlog = self.jobs.len().saturating_sub(self.idle);
        let policy = self.adaptive.as_mut()?;
        if policy.needs_observer(backlog, self.spawned, ceiling, self.closed) {
            Some(backlog)
        } else {
            None
        }
    }
}
#[derive(Default)]
struct Shared {
    queue: Mutex<Queue>,
    ready: Condvar,
    /// Wakes the growth observer early. Only shutdown uses it: the observer
    /// keeps its own clock, and a wake for every job submitted or taken cost
    /// a saturated pool far more than the jobs themselves.
    observer_ready: Condvar,
    /// Times the growth observer has looked at the queue.
    observer_checks: AtomicUsize,
    live: AtomicUsize,
    worker_ids: Mutex<Vec<std::thread::ThreadId>>,
    threads: Mutex<Vec<JoinHandle<()>>>,
    observer_thread: Mutex<Option<JoinHandle<()>>>,
}
#[pyclass(frozen, module = "bessemer._native")]
pub(crate) struct SyncPool {
    shared: Arc<Shared>,
    workers: usize,
    name: String,
}

/// Counted alive from before it is spawned (see `SyncPool::spawn`), so a
/// thread that has not yet started never reads as exited.
fn work(shared: &Shared) {
    Python::attach(|py| {
        loop {
            let job = py.detach(|| {
                let mut queue = shared.queue.lock().unwrap();
                while queue.jobs.is_empty() && !queue.closed {
                    queue.idle += 1;
                    queue = shared.ready.wait(queue).unwrap();
                    queue.idle -= 1;
                }
                let job = queue.jobs.pop_front();
                let backlog = queue.jobs.len().saturating_sub(queue.idle);
                if let Some(adaptive) = &mut queue.adaptive {
                    adaptive.note_drain(backlog);
                }
                job
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
}

/// Called with the queue lock held, so `spawned` and join handles stay in step.
fn spawn_worker(shared: &Arc<Shared>, name: &str, queue: &mut Queue) -> std::io::Result<()> {
    let worker_shared = shared.clone();
    shared.live.fetch_add(1, Ordering::Release);
    let thread = std::thread::Builder::new()
        .name(format!("{}-{}", name, queue.spawned))
        .spawn(move || work(&worker_shared))
        .inspect_err(|_| {
            shared.live.fetch_sub(1, Ordering::Release);
        })?;
    queue.spawned += 1;
    shared.worker_ids.lock().unwrap().push(thread.thread().id());
    shared.threads.lock().unwrap().push(thread);
    Ok(())
}

fn watch_backlog(shared: Arc<Shared>, ceiling: usize, name: String) {
    let mut queue = shared.queue.lock().unwrap();
    // The backlog and CPU windows decide when to look again, never the jobs
    // themselves: a drained backlog is noticed at the next look, at most one
    // window late, and a backlog that returns meanwhile finds this thread
    // still watching instead of starting another.
    while let Some(backlog) = queue.adaptive_backlog(ceiling) {
        shared.observer_checks.fetch_add(1, Ordering::Relaxed);
        let now = Instant::now();
        let spawned = queue.spawned;
        let grow = {
            let policy = queue.adaptive.as_mut().unwrap();
            policy.should_check_cpu(backlog, spawned, ceiling, now)
                && policy.observe_cpu(now, process_cpu_time())
        };
        if grow && spawn_worker(&shared, &name, &mut queue).is_err() {
            break;
        }
        if queue.adaptive_backlog(ceiling).is_none() {
            break;
        }
        let delay = queue
            .adaptive
            .as_ref()
            .unwrap()
            .next_check_delay(Instant::now())
            .max(Duration::from_millis(1));
        (queue, _) = shared.observer_ready.wait_timeout(queue, delay).unwrap();
    }
    queue.observer_running = false;
}

impl SyncPool {
    fn start_observer(&self, queue: &mut Queue) -> std::io::Result<()> {
        let shared = self.shared.clone();
        let ceiling = self.workers;
        let name = self.name.clone();
        let previous = self.shared.observer_thread.lock().unwrap().take();
        queue.observer_running = true;
        let thread = std::thread::Builder::new()
            .name(format!("{}-growth", self.name))
            .spawn(move || {
                if let Some(previous) = previous {
                    let _ = previous.join();
                }
                watch_backlog(shared, ceiling, name);
            })
            .inspect_err(|_| queue.observer_running = false)?;
        *self.shared.observer_thread.lock().unwrap() = Some(thread);
        Ok(())
    }

    fn join_threads(&self) {
        let observer = self.shared.observer_thread.lock().unwrap().take();
        if let Some(observer) = observer {
            let _ = observer.join();
        }
        let threads = std::mem::take(&mut *self.shared.threads.lock().unwrap());
        for thread in threads {
            let _ = thread.join();
        }
    }
}

#[pymethods]
impl SyncPool {
    #[new]
    #[pyo3(signature = (workers, name="bessemer-handler"))]
    fn new(workers: usize, name: &str) -> PyResult<Self> {
        if !(1..=256).contains(&workers) {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "invalid sync worker count",
            ));
        }
        Ok(Self {
            shared: Arc::new(Shared::default()),
            workers,
            name: name.to_owned(),
        })
    }

    /// Threads started and not yet exited, including ones still starting.
    #[getter]
    fn threads_alive(&self) -> usize {
        self.shared.live.load(Ordering::Acquire)
    }

    #[getter]
    fn threads_started(&self) -> usize {
        self.shared.queue.lock().unwrap().spawned
    }

    /// Threads waiting for a job, for tests. A thread whose call has just
    /// returned its result is not waiting again until it is back at the
    /// queue, and a job submitted before then starts another thread.
    #[getter]
    fn threads_idle(&self) -> usize {
        self.shared.queue.lock().unwrap().idle
    }

    /// Times the growth observer has looked at the queue, for tests: it
    /// follows the clock, not the number of jobs.
    #[getter]
    fn observer_checks(&self) -> usize {
        self.shared.observer_checks.load(Ordering::Relaxed)
    }

    #[getter]
    fn adaptive_base(&self) -> Option<usize> {
        self.shared
            .queue
            .lock()
            .unwrap()
            .adaptive
            .as_ref()
            .map(|policy| policy.base)
    }

    #[staticmethod]
    fn adaptive_available() -> bool {
        process_cpu_time().is_some()
    }

    fn enable_adaptive(&self, base_workers: usize) -> PyResult<()> {
        if base_workers == 0 || base_workers >= self.workers {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "adaptive base must be below the worker limit",
            ));
        }
        if !Self::adaptive_available() {
            return Err(PyRuntimeError::new_err(
                "adaptive process CPU clock is unavailable",
            ));
        }
        let mut queue = self.shared.queue.lock().unwrap();
        if queue.closed || queue.spawned != 0 || !queue.jobs.is_empty() {
            return Err(PyRuntimeError::new_err(
                "adaptive pool must be configured before use",
            ));
        }
        queue.adaptive = Some(Adaptive {
            base: base_workers,
            backlog_since: None,
            cpu_baseline: None,
            cpu_unavailable: false,
            last_growth: None,
        });
        Ok(())
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
        // Start the base eagerly. Extra threads need sustained backlog and CPU slack.
        let should_spawn = queue.should_spawn(self.workers);
        if should_spawn
            && let Err(error) = spawn_worker(&self.shared, &self.name, &mut queue)
            && queue.spawned == 0
        {
            // Without any thread the job would never run.
            queue.jobs.pop_back();
            return Err(PyRuntimeError::new_err(error.to_string()));
        }
        if !queue.observer_running && queue.adaptive_backlog(self.workers).is_some() {
            let _ = self.start_observer(&mut queue);
        }
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
            self.shared.observer_ready.notify_all();
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
            py.detach(|| self.join_threads());
        }
        Ok(())
    }
}
impl Drop for SyncPool {
    fn drop(&mut self) {
        let mut queue = self.shared.queue.lock().unwrap();
        queue.closed = true;
        self.shared.ready.notify_all();
        self.shared.observer_ready.notify_all();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn policy() -> Adaptive {
        Adaptive {
            base: 8,
            backlog_since: None,
            cpu_baseline: None,
            cpu_unavailable: false,
            last_growth: None,
        }
    }

    #[test]
    fn adaptive_growth_needs_backlog_cpu_slack_and_rate_limit() {
        let mut policy = policy();
        let start = Instant::now();
        let cpu = Duration::from_secs(1);
        assert!(!policy.should_check_cpu(3, 8, 16, start));
        assert!(!policy.should_check_cpu(4, 8, 16, start));
        assert!(!policy.should_check_cpu(4, 8, 16, start + Duration::from_millis(9)));
        assert!(policy.should_check_cpu(4, 8, 16, start + ADAPTIVE_DELAY));
        assert!(!policy.observe_cpu(start + ADAPTIVE_DELAY, Some(cpu)));
        assert!(!policy.should_check_cpu(4, 8, 16, start + Duration::from_millis(29)));
        assert!(policy.should_check_cpu(4, 8, 16, start + Duration::from_millis(30)));
        // Exactly 0.75 CPU cores is too busy to add GIL threads.
        assert!(!policy.observe_cpu(
            start + Duration::from_millis(30),
            Some(cpu + Duration::from_millis(15))
        ));
        assert!(policy.should_check_cpu(4, 8, 16, start + Duration::from_millis(50)));
        assert!(policy.observe_cpu(
            start + Duration::from_millis(50),
            Some(cpu + Duration::from_millis(25))
        ));
        assert!(!policy.should_check_cpu(4, 9, 16, start + Duration::from_millis(60)));
        assert!(!policy.should_check_cpu(4, 16, 16, start + Duration::from_millis(70)));
        policy.note_drain(3);
        assert!(!policy.should_check_cpu(4, 10, 16, start + Duration::from_millis(70)));
        assert!(policy.should_check_cpu(4, 10, 16, start + Duration::from_millis(80)));
        assert!(!policy.observe_cpu(
            start + Duration::from_millis(80),
            Some(cpu + Duration::from_millis(30))
        ));
    }

    #[test]
    fn adaptive_growth_denies_missing_or_regressing_cpu_clock() {
        let start = Instant::now();
        let mut missing = policy();
        assert!(!missing.should_check_cpu(4, 8, 16, start));
        assert!(missing.should_check_cpu(4, 8, 16, start + ADAPTIVE_DELAY));
        assert!(!missing.observe_cpu(start + ADAPTIVE_DELAY, None));
        assert!(!missing.should_check_cpu(4, 8, 16, start + Duration::from_millis(50)));

        let mut regressing = policy();
        assert!(!regressing.should_check_cpu(4, 8, 16, start));
        assert!(regressing.should_check_cpu(4, 8, 16, start + ADAPTIVE_DELAY));
        assert!(!regressing.observe_cpu(start + ADAPTIVE_DELAY, Some(Duration::from_secs(2))));
        assert!(regressing.should_check_cpu(4, 8, 16, start + Duration::from_millis(30)));
        assert!(!regressing.observe_cpu(
            start + Duration::from_millis(30),
            Some(Duration::from_secs(1))
        ));
        assert!(!regressing.should_check_cpu(4, 8, 16, start + Duration::from_millis(50)));
    }

    #[test]
    fn adaptive_growth_limits_spawn_frequency() {
        let mut policy = policy();
        let start = Instant::now();
        policy.backlog_since = Some(start);
        policy.cpu_baseline = Some((start, Duration::from_secs(1)));
        policy.last_growth = Some(start + Duration::from_millis(15));
        assert!(policy.should_check_cpu(4, 9, 16, start + CPU_WINDOW));
        assert!(!policy.observe_cpu(
            start + CPU_WINDOW,
            Some(Duration::from_secs(1) + Duration::from_millis(1))
        ));
    }

    #[test]
    fn observer_only_runs_for_live_adaptive_backlog() {
        let mut policy = policy();
        assert!(!policy.needs_observer(3, 8, 16, false));
        assert!(!policy.needs_observer(4, 7, 16, false));
        assert!(!policy.needs_observer(4, 16, 16, false));
        assert!(!policy.needs_observer(4, 8, 16, true));
        assert!(policy.needs_observer(4, 8, 16, false));
        assert!(!policy.observe_cpu(Instant::now(), None));
        assert!(!policy.needs_observer(4, 8, 16, false));
    }

    #[test]
    fn observer_waits_for_backlog_and_cpu_windows() {
        let mut policy = policy();
        let start = Instant::now();
        policy.backlog_since = Some(start);
        assert_eq!(policy.next_check_delay(start), ADAPTIVE_DELAY);
        policy.cpu_baseline = Some((start + ADAPTIVE_DELAY, Duration::ZERO));
        assert_eq!(
            policy.next_check_delay(start + Duration::from_millis(15)),
            Duration::from_millis(15)
        );
        assert_eq!(
            policy.next_check_delay(start + Duration::from_millis(30)),
            Duration::ZERO
        );
    }
}
