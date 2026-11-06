//! The loop's scheduler and reactor: the ready queue, the timer heap, the
//! poller (epoll or kqueue, see `poller.rs`),
//! and `run_forever`.
//!
//! `LoopCore` is a base class. The Python `bessemer_loop.Loop` class inherits
//! from it and from asyncio's Unix selector loop, so the methods defined here
//! replace the stdlib's scheduler and I/O registration while the stdlib's
//! high-level APIs (servers, transports, streams, subprocesses, signals,
//! executors, the exception handler) run on top of them unchanged.

use std::cmp::Ordering as CmpOrdering;
use std::collections::{BinaryHeap, HashMap, VecDeque};
use std::os::fd::RawFd;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

use pyo3::exceptions::{PyOSError, PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyTuple};
use pyo3::{PyTraverseError, PyVisit, intern};

use crate::driver::Driver;
use crate::handle::{Callback, Handle, callback_context};
use crate::poller;

/// asyncio's limit on one poll's timeout, in seconds.
const MAXIMUM_SELECT_TIMEOUT: f64 = 24.0 * 3600.0;
/// Compact the timer heap once this many timers are scheduled and more than
/// half of them are cancelled (asyncio's thresholds).
const MIN_SCHEDULED_TIMER_HANDLES: usize = 100;
const MAX_EVENTS: usize = 256;

pub(crate) fn monotonic() -> f64 {
    let mut ts = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // The clock time.monotonic() reads: CLOCK_MONOTONIC on Linux, and on
    // macOS mach_absolute_time, which is CLOCK_UPTIME_RAW.
    #[cfg(target_os = "linux")]
    let clock = libc::CLOCK_MONOTONIC;
    #[cfg(target_os = "macos")]
    let clock = libc::CLOCK_UPTIME_RAW;
    unsafe { libc::clock_gettime(clock, &mut ts) };
    ts.tv_sec as f64 + ts.tv_nsec as f64 * 1e-9
}

/// Something to run on the next iteration: our own handle, or a foreign
/// object with `_run` and `_cancelled` (asyncio's signal handles, for one).
enum Ready {
    Native(Py<Handle>),
    Foreign(Py<PyAny>),
}

struct Timer {
    when: f64,
    seq: u64,
    handle: Py<Handle>,
}

impl PartialEq for Timer {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == CmpOrdering::Equal
    }
}
impl Eq for Timer {}
impl PartialOrd for Timer {
    fn partial_cmp(&self, other: &Self) -> Option<CmpOrdering> {
        Some(self.cmp(other))
    }
}
impl Ord for Timer {
    // Reversed, so the BinaryHeap is a min-heap on (when, seq).
    fn cmp(&self, other: &Self) -> CmpOrdering {
        other
            .when
            .total_cmp(&self.when)
            .then_with(|| other.seq.cmp(&self.seq))
    }
}

#[derive(Default)]
struct Registration {
    reader: Option<Py<Handle>>,
    writer: Option<Py<Handle>>,
    /// The poller interest this fd is registered for (`poller::READ` and
    /// `poller::WRITE`; 0 = not registered).
    mask: u32,
}

#[derive(Default)]
struct State {
    ready: VecDeque<Ready>,
    timers: BinaryHeap<Timer>,
    seq: u64,
    io: HashMap<RawFd, Registration>,
    /// Set (under the lock) while the loop is about to block or is blocked in
    /// the poller's wait, so `call_soon_threadsafe` knows a wake-up write is needed.
    polling: bool,
    /// Descriptors marked readable from this process with `wake_reader`.
    woken: Vec<RawFd>,
    /// The thread running `run_driven`, whose waits are in the embedded
    /// runtime: a callback queued from a runtime task on that thread wakes
    /// the wait through the driver instead of the self-pipe.
    driven: Option<std::thread::ThreadId>,
}

impl State {
    /// Whether a callback queued now needs the loop woken, and whether the
    /// driver can do it: the queueing thread is the driven loop's own, so it
    /// runs inside the runtime's wait (a Tokio task) rather than blocked in it.
    fn wake(&self) -> Wake {
        if !self.polling {
            Wake::None
        } else if self.driven.is_some() && self.driven == Some(std::thread::current().id()) {
            Wake::Driver
        } else {
            Wake::Pipe
        }
    }
}

#[derive(PartialEq)]
enum Wake {
    None,
    Driver,
    Pipe,
}

#[pyclass(frozen, subclass, module = "bessemer._native")]
pub struct LoopCore {
    /// Never held across a call into Python.
    state: Mutex<State>,
    /// -1 once the loop is closed.
    epfd: std::sync::atomic::AtomicI32,
    stopping: AtomicBool,
    /// Whether `run_forever` is between asyncio's `_run_forever_setup` and
    /// `_run_forever_cleanup`, which set and clear `_thread_id`.
    running: AtomicBool,
    /// The write end of asyncio's self-pipe, for cross-thread wake-ups.
    wakeup_fd: std::sync::atomic::AtomicI32,
    closed: AtomicBool,
    debug: AtomicBool,
    timer_cancelled: AtomicUsize,
    /// A Tokio runtime run on the loop's thread while it waits for events.
    /// Released on close.
    driver: Mutex<Option<std::sync::Arc<Driver>>>,
}

impl LoopCore {
    /// The handle of the loop's embedded Tokio runtime, created on first use.
    pub fn runtime(&self) -> std::io::Result<tokio::runtime::Handle> {
        let mut slot = self.driver.lock().unwrap();
        if let Some(driver) = slot.as_ref() {
            return Ok(driver.handle());
        }
        if self.closed.load(Ordering::Relaxed) {
            return Err(std::io::Error::other("the event loop is closed"));
        }
        let driver = Driver::new(self.epfd.load(Ordering::Relaxed))?;
        let handle = driver.handle();
        *slot = Some(std::sync::Arc::new(driver));
        Ok(handle)
    }

    fn driver(&self) -> Option<std::sync::Arc<Driver>> {
        self.driver.lock().unwrap().clone()
    }
}

impl LoopCore {
    /// Whether `close` has been called.
    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Relaxed)
    }

    /// Run the reader registered for `fd` on the next iteration, as if
    /// the poller had reported it readable, without a system call. Only for a
    /// loop with an embedded runtime; others ignore it.
    pub fn wake_reader(&self, fd: RawFd) {
        let Some(driver) = self.driver() else {
            return;
        };
        {
            let mut state = self.state.lock().unwrap();
            if !state.woken.contains(&fd) {
                state.woken.push(fd);
            }
        }
        driver.notify();
    }
}

impl Drop for LoopCore {
    fn drop(&mut self) {
        let epfd = self.epfd.swap(-1, Ordering::Relaxed);
        if epfd >= 0 {
            unsafe { libc::close(epfd) };
        }
    }
}

static FUTURE_TYPE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static TASK_TYPE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

fn asyncio_attr<'py>(
    py: Python<'py>,
    cell: &'static PyOnceLock<Py<PyAny>>,
    name: &str,
) -> PyResult<&'py Bound<'py, PyAny>> {
    cell.get_or_try_init(py, || {
        Ok::<_, PyErr>(py.import("asyncio")?.getattr(name)?.unbind())
    })
    .map(|obj| obj.bind(py))
}

/// The file descriptor of an int or an object with `fileno()`, as selectors
/// does it.
pub(crate) fn fileobj_to_fd(obj: &Bound<'_, PyAny>) -> PyResult<RawFd> {
    let fd: i64 = match obj.extract::<i64>() {
        Ok(fd) => fd,
        Err(_) => {
            let fileno = obj
                .call_method0(intern!(obj.py(), "fileno"))
                .map_err(|_| PyValueError::new_err(format!("Invalid file object: {obj:?}")))?;
            fileno
                .extract::<i64>()
                .map_err(|_| PyValueError::new_err(format!("Invalid file object: {obj:?}")))?
        }
    };
    if fd < 0 || fd > i32::MAX as i64 {
        return Err(PyValueError::new_err(format!(
            "Invalid file descriptor: {fd}"
        )));
    }
    Ok(fd as RawFd)
}

impl LoopCore {
    fn check_closed(&self) -> PyResult<()> {
        if self.closed.load(Ordering::Relaxed) {
            return Err(PyRuntimeError::new_err("Event loop is closed"));
        }
        Ok(())
    }

    pub(crate) fn get_debug_flag(&self) -> bool {
        self.debug.load(Ordering::Relaxed)
    }

    /// In debug mode, record where a handle was created, as asyncio does.
    /// No Rust frames are on the Python stack, so frame 0 is the caller.
    fn attach_traceback(&self, py: Python<'_>, handle: &Py<Handle>) -> PyResult<()> {
        if self.debug.load(Ordering::Relaxed) {
            let frame = py.import("sys")?.call_method1("_getframe", (0,))?;
            let stack = py
                .import("asyncio.format_helpers")?
                .call_method1("extract_stack", (frame,))?;
            let _ = handle.get().source_traceback.set(stack.unbind());
        }
        Ok(())
    }

    pub(crate) fn timer_cancelled(&self) {
        self.timer_cancelled.fetch_add(1, Ordering::Relaxed);
    }

    fn debug_checks(
        slf: &Bound<'_, Self>,
        callback: &Bound<'_, PyAny>,
        method: &str,
    ) -> PyResult<()> {
        if slf.get().debug.load(Ordering::Relaxed) {
            let py = slf.py();
            slf.call_method0(intern!(py, "_check_thread"))?;
            slf.call_method1(intern!(py, "_check_callback"), (callback, method))?;
        }
        Ok(())
    }

    pub(crate) fn push_handle(
        slf: &Bound<'_, Self>,
        callback: Bound<'_, PyAny>,
        args: Bound<'_, PyTuple>,
        context: Option<Py<PyAny>>,
    ) -> PyResult<Py<Handle>> {
        let py = slf.py();
        let handle = Py::new(
            py,
            Handle::new(Callback {
                func: callback.unbind(),
                args: args.unbind(),
                context: callback_context(py, context)?,
            }),
        )?;
        slf.get().attach_traceback(py, &handle)?;
        let queued = handle.clone_ref(py);
        let wake = {
            let mut state = slf.get().state.lock().unwrap();
            state.ready.push_back(Ready::Native(queued));
            state.wake()
        };
        // Only a Tokio task on the driven loop's thread queues while the loop
        // waits; the wait must end for the callback to run.
        if wake == Wake::Driver
            && let Some(driver) = slf.get().driver()
        {
            driver.notify();
        }
        Ok(handle)
    }

    /// Bring the poller in line with the wanted mask.
    fn sync_poller(&self, fd: RawFd, old: u32, new: u32) -> PyResult<()> {
        poller::update(self.epfd.load(Ordering::Relaxed), fd, old, new).map_err(PyErr::from)
    }

    pub(crate) fn add_io(
        slf: &Bound<'_, Self>,
        fd: RawFd,
        write: bool,
        callback: Bound<'_, PyAny>,
        args: Bound<'_, PyTuple>,
    ) -> PyResult<Py<Handle>> {
        let py = slf.py();
        let this = slf.get();
        this.check_closed()?;
        let handle = Py::new(
            py,
            Handle::new(Callback {
                func: callback.unbind(),
                args: args.unbind(),
                context: callback_context(py, None)?,
            }),
        )?;
        let replaced = {
            let mut state = this.state.lock().unwrap();
            let reg = state.io.entry(fd).or_default();
            let old = reg.mask;
            let bit = if write { poller::WRITE } else { poller::READ };
            let new = old | bit;
            if let Err(err) = this.sync_poller(fd, old, new) {
                if old == 0 && reg.reader.is_none() && reg.writer.is_none() {
                    state.io.remove(&fd);
                }
                return Err(err);
            }
            reg.mask = new;
            let slot = if write {
                &mut reg.writer
            } else {
                &mut reg.reader
            };
            slot.replace(handle.clone_ref(py))
        };
        // asyncio cancels a replaced handle.
        if let Some(old) = replaced {
            Handle::cancel_handle(old.bind(py));
        }
        Ok(handle)
    }

    pub(crate) fn remove_io(slf: &Bound<'_, Self>, fd: RawFd, write: bool) -> PyResult<bool> {
        let py = slf.py();
        let this = slf.get();
        if this.closed.load(Ordering::Relaxed) {
            return Ok(false);
        }
        let removed = {
            let mut state = this.state.lock().unwrap();
            let Some(reg) = state.io.get_mut(&fd) else {
                return Ok(false);
            };
            let slot = if write {
                &mut reg.writer
            } else {
                &mut reg.reader
            };
            let Some(handle) = slot.take() else {
                return Ok(false);
            };
            let old = reg.mask;
            let bit = if write { poller::WRITE } else { poller::READ };
            let new = old & !bit;
            reg.mask = new;
            if new == 0 {
                state.io.remove(&fd);
            }
            // A descriptor closed before removal is already gone from the
            // poller.
            let _ = this.sync_poller(fd, old, new);
            handle
        };
        Handle::cancel_handle(removed.bind(py));
        Ok(true)
    }

    /// The loop body with an embedded runtime: callbacks run inside the
    /// runtime's `block_on`, so tasks they wake are queued on this thread
    /// without waking the runtime's driver.
    fn run_driven(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py = slf.py();
        let this = slf.get();
        let driver = this.driver().expect("driver");
        let owned: Py<Self> = slf.clone().unbind();
        let first = this.next_timeout();
        this.state.lock().unwrap().driven = Some(std::thread::current().id());
        let result = py.detach(|| {
            driver.block_on(async {
                let mut timeout_ms = first;
                // Set when the last poll returned events. Tokio watches the
                // poller descriptor edge-triggered while the descriptors in it
                // are level-triggered: a reader that leaves data unread, or a
                // drain that filled the buffer, produces no new edge. So the
                // loop polls again, without blocking, until a poll is empty.
                let mut pending = false;
                // One buffer for the whole run: zeroing it per iteration cost
                // about 3k instructions a request.
                let mut events: [poller::Event; MAX_EVENTS] = [poller::EMPTY; MAX_EVENTS];
                loop {
                    let wait = if pending { 0 } else { timeout_ms };
                    let readable = driver.wait_async(wait).await || pending;
                    let step = Python::attach(|py| -> PyResult<Option<i32>> {
                        let slf = owned.bind(py);
                        let this = slf.get();
                        let epfd = this.epfd.load(Ordering::Relaxed);
                        let n = if readable {
                            unsafe { poller::wait(epfd, events.as_mut_ptr(), MAX_EVENTS, 0) }
                        } else {
                            0
                        };
                        let mut n = polled(py, n)?;
                        pending = n > 0;
                        let woken = std::mem::take(&mut this.state.lock().unwrap().woken);
                        for fd in woken {
                            let seen = events[..n]
                                .iter()
                                .any(|event| poller::fd(event) == fd && poller::readable(event));
                            if !seen && n < MAX_EVENTS {
                                events[n] = poller::read_event(fd);
                                n += 1;
                            } else if !seen {
                                // No room: keep it for the next iteration.
                                this.state.lock().unwrap().woken.push(fd);
                            }
                        }
                        Self::dispatch(slf, &events[..n])?;
                        if this.stopping.load(Ordering::Relaxed) {
                            return Ok(None);
                        }
                        Ok(Some(this.next_timeout()))
                    });
                    match step {
                        Ok(Some(next)) => timeout_ms = next,
                        Ok(None) => return Ok(()),
                        Err(err) => return Err(err),
                    }
                }
            })
        });
        this.state.lock().unwrap().driven = None;
        result
    }

    /// The poll timeout for the next wait in milliseconds (0: do not block,
    /// -1: no limit), after dropping cancelled timers. Marks the loop as
    /// polling when it will block.
    fn next_timeout(&self) -> i32 {
        let this = self;
        {
            let mut state = this.state.lock().unwrap();
            let scheduled = state.timers.len();
            let cancelled = this.timer_cancelled.load(Ordering::Relaxed);
            if scheduled > MIN_SCHEDULED_TIMER_HANDLES && cancelled * 2 > scheduled {
                let timers = std::mem::take(&mut state.timers);
                let mut kept = Vec::with_capacity(scheduled - cancelled.min(scheduled));
                for timer in timers.into_vec() {
                    if timer.handle.get().is_cancelled() {
                        timer.handle.get().unschedule();
                    } else {
                        kept.push(timer);
                    }
                }
                state.timers = BinaryHeap::from(kept);
                this.timer_cancelled.store(0, Ordering::Relaxed);
            } else {
                while let Some(top) = state.timers.peek() {
                    if !top.handle.get().is_cancelled() {
                        break;
                    }
                    let timer = state.timers.pop().unwrap();
                    timer.handle.get().unschedule();
                    this.timer_cancelled.fetch_sub(1, Ordering::Relaxed);
                }
            }
            let timeout: i32 = if !state.ready.is_empty()
                || !state.woken.is_empty()
                || this.stopping.load(Ordering::Relaxed)
            {
                0
            } else if let Some(top) = state.timers.peek() {
                let wait = (top.when - monotonic()).clamp(0.0, MAXIMUM_SELECT_TIMEOUT);
                // Round up, as selectors does, so a timer is due when we wake.
                (wait * 1e3).ceil() as i32
            } else {
                -1
            };
            state.polling = timeout != 0;
            timeout
        }
    }

    fn run_once(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py = slf.py();
        let this = slf.get();
        let timeout_ms = this.next_timeout();

        // Left uninitialised: the poller writes the first `n` entries, and
        // only those are read.
        let mut events = [std::mem::MaybeUninit::<poller::Event>::uninit(); MAX_EVENTS];
        let epfd = this.epfd.load(Ordering::Relaxed);
        let ptr = events.as_mut_ptr() as usize;
        let poll = move || unsafe {
            poller::wait(epfd, ptr as *mut poller::Event, MAX_EVENTS, timeout_ms)
        };
        let n = if let Some(driver) = this.driver() {
            py.detach(|| driver.wait(timeout_ms));
            unsafe { poller::wait(epfd, ptr as *mut poller::Event, MAX_EVENTS, 0) }
        } else if timeout_ms == 0 {
            poll()
        } else {
            py.detach(poll)
        };
        let n = polled(py, n)?;
        // SAFETY: the poller initialised the first `n` entries.
        let events = unsafe { std::slice::from_raw_parts(ptr as *const poller::Event, n) };
        Self::dispatch(slf, events)
    }

    /// Queue the handles for `n` polled events and the due timers, then run
    /// the ready queue once.
    fn dispatch(slf: &Bound<'_, Self>, events: &[poller::Event]) -> PyResult<()> {
        let py = slf.py();
        let this = slf.get();
        let ntodo = {
            let mut state = this.state.lock().unwrap();
            state.polling = false;
            for event in events {
                let fd = poller::fd(event);
                let Some(reg) = state.io.get(&fd) else {
                    continue;
                };
                // With the selectors' semantics: see `poller::readable` and
                // `poller::writable`.
                let mut wake: [Option<Py<Handle>>; 2] = [None, None];
                if poller::readable(event)
                    && let Some(h) = &reg.reader
                    && !h.get().is_cancelled()
                {
                    wake[0] = Some(h.clone_ref(py));
                }
                if poller::writable(event)
                    && let Some(h) = &reg.writer
                    && !h.get().is_cancelled()
                {
                    wake[1] = Some(h.clone_ref(py));
                }
                for h in wake.into_iter().flatten() {
                    state.ready.push_back(Ready::Native(h));
                }
            }

            let end = monotonic() + 1e-9;
            while let Some(top) = state.timers.peek() {
                if top.when >= end {
                    break;
                }
                let timer = state.timers.pop().unwrap();
                timer.handle.get().unschedule();
                state.ready.push_back(Ready::Native(timer.handle));
            }
            state.ready.len()
        };

        let as_any = slf.as_any();
        for _ in 0..ntodo {
            let item = this.state.lock().unwrap().ready.pop_front();
            match item {
                Some(Ready::Native(handle)) => {
                    let handle = handle.into_bound(py);
                    if handle.get().is_cancelled() {
                        continue;
                    }
                    Handle::run(&handle, as_any)?;
                }
                Some(Ready::Foreign(obj)) => {
                    let obj = obj.into_bound(py);
                    if obj.getattr(intern!(py, "_cancelled"))?.is_truthy()? {
                        continue;
                    }
                    obj.call_method0(intern!(py, "_run"))?;
                }
                None => break,
            }
        }
        Ok(())
    }
}

/// The event count from a poller wait result, with an interrupted wait
/// counted as no events after running Python's signal handlers.
fn polled(py: Python<'_>, n: i32) -> PyResult<usize> {
    if n >= 0 {
        return Ok(n as usize);
    }
    let err = std::io::Error::last_os_error();
    if err.raw_os_error() != Some(libc::EINTR) {
        return Err(PyOSError::new_err(err.to_string()));
    }
    // Run Python-level signal handlers (KeyboardInterrupt, for one).
    py.check_signals()?;
    Ok(0)
}

impl Handle {
    pub(crate) fn cancel_handle(handle: &Bound<'_, Handle>) {
        // Same as Handle.cancel() from Python.
        let _ = handle.call_method0(intern!(handle.py(), "cancel"));
    }
}

#[pymethods]
impl LoopCore {
    #[new]
    #[pyo3(signature = (*_args, **_kwargs))]
    fn new(_args: &Bound<'_, PyTuple>, _kwargs: Option<&Bound<'_, PyDict>>) -> PyResult<Self> {
        let epfd = poller::create()?;
        Ok(LoopCore {
            state: Mutex::new(State::default()),
            epfd: std::sync::atomic::AtomicI32::new(epfd),
            stopping: AtomicBool::new(false),
            running: AtomicBool::new(false),
            wakeup_fd: std::sync::atomic::AtomicI32::new(-1),
            closed: AtomicBool::new(false),
            debug: AtomicBool::new(false),
            timer_cancelled: AtomicUsize::new(0),
            driver: Mutex::new(None),
        })
    }

    fn time(&self) -> f64 {
        monotonic()
    }

    fn get_debug(&self) -> bool {
        self.debug.load(Ordering::Relaxed)
    }

    /// What asyncio's `_thread_id is not None` answers, without a Python
    /// call: every eagerly started task asks it.
    fn is_running(&self) -> bool {
        self.running.load(Ordering::Relaxed)
    }

    fn _core_set_debug(&self, enabled: bool) {
        self.debug.store(enabled, Ordering::Relaxed);
    }

    #[pyo3(signature = (callback, *args, context=None))]
    fn call_soon(
        slf: &Bound<'_, Self>,
        callback: Bound<'_, PyAny>,
        args: Bound<'_, PyTuple>,
        context: Option<Py<PyAny>>,
    ) -> PyResult<Py<Handle>> {
        slf.get().check_closed()?;
        Self::debug_checks(slf, &callback, "call_soon")?;
        Self::push_handle(slf, callback, args, context)
    }

    #[pyo3(signature = (callback, *args, context=None))]
    fn call_soon_threadsafe(
        slf: &Bound<'_, Self>,
        callback: Bound<'_, PyAny>,
        args: Bound<'_, PyTuple>,
        context: Option<Py<PyAny>>,
    ) -> PyResult<Py<Handle>> {
        slf.get().check_closed()?;
        if slf.get().debug.load(Ordering::Relaxed) {
            slf.call_method1(
                intern!(slf.py(), "_check_callback"),
                (&callback, "call_soon_threadsafe"),
            )?;
        }
        let py = slf.py();
        let this = slf.get();
        let handle = Py::new(
            py,
            Handle::new(Callback {
                func: callback.unbind(),
                args: args.unbind(),
                context: callback_context(py, context)?,
            }),
        )?;
        this.attach_traceback(py, &handle)?;
        let wake = {
            let mut state = this.state.lock().unwrap();
            state.ready.push_back(Ready::Native(handle.clone_ref(py)));
            state.wake()
        };
        // Only a loop blocked in the poller needs the pipe written; one that is
        // running callbacks sees the queue on its next iteration. A Tokio task
        // on the driven loop's own thread ends the runtime's wait directly,
        // without the pipe's two system calls and its reader callback.
        if wake == Wake::Driver
            && let Some(driver) = this.driver()
        {
            driver.notify();
            return Ok(handle);
        }
        let fd = this.wakeup_fd.load(Ordering::Relaxed);
        if wake != Wake::None {
            if fd >= 0 {
                unsafe { libc::send(fd, c"".as_ptr().cast(), 1, libc::MSG_DONTWAIT) };
            } else {
                slf.call_method0(intern!(py, "_write_to_self"))?;
            }
        }
        Ok(handle)
    }

    /// asyncio's internal entry point, used by its own code with a tuple.
    fn _call_soon(
        slf: &Bound<'_, Self>,
        callback: Bound<'_, PyAny>,
        args: Bound<'_, PyTuple>,
        context: Option<Py<PyAny>>,
    ) -> PyResult<Py<Handle>> {
        Self::push_handle(slf, callback, args, context)
    }

    #[pyo3(signature = (delay, callback, *args, context=None))]
    fn call_later(
        slf: &Bound<'_, Self>,
        delay: Option<f64>,
        callback: Bound<'_, PyAny>,
        args: Bound<'_, PyTuple>,
        context: Option<Py<PyAny>>,
    ) -> PyResult<Py<Handle>> {
        let Some(delay) = delay else {
            return Err(PyTypeError::new_err("delay must not be None"));
        };
        Self::schedule_at(
            slf,
            monotonic() + delay,
            callback,
            args,
            context,
            "call_later",
        )
    }

    #[pyo3(signature = (when, callback, *args, context=None))]
    fn call_at(
        slf: &Bound<'_, Self>,
        when: Option<f64>,
        callback: Bound<'_, PyAny>,
        args: Bound<'_, PyTuple>,
        context: Option<Py<PyAny>>,
    ) -> PyResult<Py<Handle>> {
        let Some(when) = when else {
            return Err(PyTypeError::new_err("when cannot be None"));
        };
        Self::schedule_at(slf, when, callback, args, context, "call_at")
    }

    /// For asyncio code that appends its own handles (signal delivery).
    fn _add_callback(slf: &Bound<'_, Self>, handle: Bound<'_, PyAny>) -> PyResult<()> {
        let py = slf.py();
        let item = match handle.cast::<Handle>() {
            Ok(native) => {
                if native.get().is_cancelled() {
                    return Ok(());
                }
                Ready::Native(native.clone().unbind())
            }
            Err(_) => {
                if handle.getattr(intern!(py, "_cancelled"))?.is_truthy()? {
                    return Ok(());
                }
                Ready::Foreign(handle.unbind())
            }
        };
        slf.get().state.lock().unwrap().ready.push_back(item);
        Ok(())
    }

    fn _timer_handle_cancelled(&self, _handle: Bound<'_, PyAny>) {}

    #[pyo3(signature = (fd, callback, *args))]
    fn _add_reader(
        slf: &Bound<'_, Self>,
        fd: Bound<'_, PyAny>,
        callback: Bound<'_, PyAny>,
        args: Bound<'_, PyTuple>,
    ) -> PyResult<Py<Handle>> {
        let fd = fileobj_to_fd(&fd)?;
        Self::add_io(slf, fd, false, callback, args)
    }

    #[pyo3(signature = (fd, callback, *args))]
    fn _add_writer(
        slf: &Bound<'_, Self>,
        fd: Bound<'_, PyAny>,
        callback: Bound<'_, PyAny>,
        args: Bound<'_, PyTuple>,
    ) -> PyResult<Py<Handle>> {
        let fd = fileobj_to_fd(&fd)?;
        Self::add_io(slf, fd, true, callback, args)
    }

    fn _remove_reader(slf: &Bound<'_, Self>, fd: Bound<'_, PyAny>) -> PyResult<bool> {
        let fd = fileobj_to_fd(&fd)?;
        Self::remove_io(slf, fd, false)
    }

    fn _remove_writer(slf: &Bound<'_, Self>, fd: Bound<'_, PyAny>) -> PyResult<bool> {
        let fd = fileobj_to_fd(&fd)?;
        Self::remove_io(slf, fd, true)
    }

    /// Whether a reader or writer is registered for the descriptor, for the
    /// transports' repr and for tests.
    fn _bessemer_io_state(&self, fd: Bound<'_, PyAny>) -> PyResult<(bool, bool)> {
        let fd = fileobj_to_fd(&fd)?;
        let state = self.state.lock().unwrap();
        Ok(match state.io.get(&fd) {
            Some(reg) => (reg.reader.is_some(), reg.writer.is_some()),
            None => (false, false),
        })
    }

    /// The loop body. `_run_forever_setup` and `_run_forever_cleanup` are
    /// asyncio's own (running-loop registration, async generator hooks).
    fn run_forever(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py = slf.py();
        slf.call_method0(intern!(py, "_run_forever_setup"))?;
        let this = slf.get();
        this.running.store(true, Ordering::Relaxed);
        let result: PyResult<()> = if this.driver().is_some() {
            Self::run_driven(slf)
        } else {
            (|| {
                loop {
                    Self::run_once(slf)?;
                    if this.stopping.load(Ordering::Relaxed) {
                        return Ok(());
                    }
                }
            })()
        };
        this.stopping.store(false, Ordering::Relaxed);
        // Cleanup clears `_thread_id` first.
        this.running.store(false, Ordering::Relaxed);
        let cleanup = slf.call_method0(intern!(py, "_run_forever_cleanup"));
        result?;
        cleanup?;
        Ok(())
    }

    fn _run_once(slf: &Bound<'_, Self>) -> PyResult<()> {
        Self::run_once(slf)
    }

    fn stop(&self) {
        self.stopping.store(true, Ordering::Relaxed);
    }

    /// Drop queued callbacks, timers and registrations. The Python `close`
    /// calls this and then asyncio's own close.
    fn _core_close(&self) -> PyResult<()> {
        self.closed.store(true, Ordering::Relaxed);
        let state = std::mem::take(&mut *self.state.lock().unwrap());
        for timer in &state.timers {
            timer.handle.get().unschedule();
        }
        let poller_fd = self.epfd.load(Ordering::Relaxed);
        for (fd, reg) in &state.io {
            let _ = poller::update(poller_fd, *fd, reg.mask, 0);
        }
        // Release the reactor now rather than when the object is collected:
        // a closed loop can stay referenced for a long time.
        let epfd = self.epfd.swap(-1, Ordering::Relaxed);
        if epfd >= 0 {
            unsafe { libc::close(epfd) };
        }
        // Dropped here, outside the lock: destructors may run Python.
        drop(state);
        // The embedded runtime holds a duplicate of the poller descriptor and
        // its own reactor; dropping it also drops any tasks still spawned.
        let driver = self.driver.lock().unwrap().take();
        drop(driver);
        Ok(())
    }

    /// Create the embedded Tokio runtime before the loop starts, so its
    /// first wait already runs in it.
    fn _core_enable_runtime(&self) -> PyResult<()> {
        self.runtime()?;
        Ok(())
    }

    fn _core_set_wakeup_fd(&self, fd: RawFd) {
        self.wakeup_fd.store(fd, Ordering::Relaxed);
    }

    fn _core_is_closed(&self) -> bool {
        self.closed.load(Ordering::Relaxed)
    }

    fn create_future(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let future = asyncio_attr(py, &FUTURE_TYPE, "Future")?;
        let kwargs = PyDict::new(py);
        kwargs.set_item(intern!(py, "loop"), slf)?;
        Ok(future.call((), Some(&kwargs))?.unbind())
    }

    /// Fast path for the common case (no task factory); everything else goes
    /// to asyncio's implementation.
    #[pyo3(signature = (coro, **kwargs))]
    fn create_task(
        slf: &Bound<'_, Self>,
        coro: Bound<'_, PyAny>,
        kwargs: Option<Bound<'_, PyDict>>,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        slf.get().check_closed()?;
        let factory = slf.getattr(intern!(py, "_task_factory"))?;
        if !factory.is_none() {
            return Ok(slf
                .call_method(
                    intern!(py, "_bessemer_create_task_slow"),
                    (coro,),
                    kwargs.as_ref(),
                )?
                .unbind());
        }
        let task = asyncio_attr(py, &TASK_TYPE, "Task")?;
        let kwargs = kwargs.unwrap_or_else(|| PyDict::new(py));
        kwargs.set_item(intern!(py, "loop"), slf)?;
        Ok(task.call((coro,), Some(&kwargs))?.unbind())
    }

    #[pyo3(signature = (sock, n))]
    fn _bessemer_sock_recv(
        slf: &Bound<'_, Self>,
        sock: Bound<'_, PyAny>,
        n: isize,
    ) -> PyResult<Py<PyAny>> {
        crate::sock::sock_recv(slf, sock, n)
    }

    #[pyo3(signature = (sock, data))]
    fn _bessemer_sock_sendall(
        slf: &Bound<'_, Self>,
        sock: Bound<'_, PyAny>,
        data: Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        crate::sock::sock_sendall(slf, sock, data)
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        let Ok(state) = self.state.try_lock() else {
            return Ok(());
        };
        for item in &state.ready {
            match item {
                Ready::Native(h) => visit.call(h)?,
                Ready::Foreign(o) => visit.call(o)?,
            }
        }
        for timer in state.timers.iter() {
            visit.call(&timer.handle)?;
        }
        for reg in state.io.values() {
            if let Some(h) = &reg.reader {
                visit.call(h)?;
            }
            if let Some(h) = &reg.writer {
                visit.call(h)?;
            }
        }
        Ok(())
    }

    fn __clear__(&self) {
        let state = self.state.lock().ok().map(|mut s| std::mem::take(&mut *s));
        drop(state);
    }
}

impl LoopCore {
    fn schedule_at(
        slf: &Bound<'_, Self>,
        when: f64,
        callback: Bound<'_, PyAny>,
        args: Bound<'_, PyTuple>,
        context: Option<Py<PyAny>>,
        method: &str,
    ) -> PyResult<Py<Handle>> {
        let py = slf.py();
        let this = slf.get();
        this.check_closed()?;
        if when.is_nan() {
            return Err(PyValueError::new_err("when must not be NaN"));
        }
        Self::debug_checks(slf, &callback, method)?;
        let handle = Py::new(
            py,
            Handle::new_timer(
                Callback {
                    func: callback.unbind(),
                    args: args.unbind(),
                    context: callback_context(py, context)?,
                },
                when,
                slf.clone().unbind(),
            ),
        )?;
        this.attach_traceback(py, &handle)?;
        let mut state = this.state.lock().unwrap();
        state.seq += 1;
        let seq = state.seq;
        state.timers.push(Timer {
            when,
            seq,
            handle: handle.clone_ref(py),
        });
        Ok(handle)
    }
}
