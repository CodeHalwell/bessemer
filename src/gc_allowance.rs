//! Keeps the objects of a request body out of the collector's young budget.
//!
//! CPython starts a collection of its young generation when the number of
//! container objects allocated since the last one, less those freed, passes
//! a threshold (2,000 by default). A body parsed into models allocates that
//! many at once: an instance, its `__dict__` and its field set for every
//! model, and every list. None of them can be garbage while the request that
//! owns them is being served, so the collection they provoke walks all of
//! them and frees nothing. For a 1,000-item body that walk was about a sixth
//! of the request's instructions and more of its time.
//!
//! The collector cannot be told which objects not to count, but its
//! threshold can be moved. While a request holds objects that its body's
//! parse allocated, the young threshold is higher by their number, and it
//! comes back down when the request lets go. The count and the threshold
//! rise and fall together, so every other allocation meets the same budget
//! as before and collections happen when they would have without the body.
//!
//! A request that keeps its body's objects after it ends (a cache, a
//! global) is no exception: the threshold comes down, the objects still
//! count, and the next allocation finds the budget spent and starts the
//! collection that was put off.
//!
//! This needs the interpreter's global lock. Without it other threads
//! allocate, free and collect while a body is parsed, so what the parse
//! allocated cannot be read from the collector's count, and the collector
//! there is not generational. On a free-threaded interpreter no body is
//! measured and the threshold is never touched.
//!
//! The threshold belongs to the application. Each adjustment reads it first:
//! a value other than the one last set here is the application's new choice
//! and becomes the base the allowances are added to. A threshold of zero,
//! which switches automatic collection off, is left at zero.

use std::sync::{
    Mutex, PoisonError,
    atomic::{AtomicBool, Ordering},
};

use pyo3::{
    prelude::*,
    sync::{MutexExt, PyOnceLock},
};

/// Bodies shorter than this are not measured: they cannot hold enough
/// objects to matter, and measuring is two calls into `gc`.
const MEASURED_BODY: usize = 4096;

/// A parse that allocated fewer containers than this gets no allowance.
/// Granting and releasing one is four more calls into `gc`.
const MIN_OBJECTS: i64 = 512;

static ENABLED: AtomicBool = AtomicBool::new(true);

static STATE: Mutex<State> = Mutex::new(State {
    base: 0,
    granted: 0,
    last_set: None,
});

struct State {
    /// The application's young threshold, as last seen.
    base: i64,
    /// The sum of the allowances in force.
    granted: i64,
    /// The threshold last set here, which tells this module's own value
    /// from one the application has set since.
    last_set: Option<i64>,
}

struct Collector {
    /// Whether the interpreter runs with its global lock. Without it the
    /// count across a parse is not the parse's own (other threads allocate,
    /// free and collect meanwhile) and collections are scheduled differently,
    /// so no body is measured and no allowance is made.
    serial: bool,
    is_enabled: Py<PyAny>,
    get_count: Py<PyAny>,
    get_threshold: Py<PyAny>,
    set_threshold: Py<PyAny>,
}

fn collector(py: Python<'_>) -> PyResult<&'static Collector> {
    static COLLECTOR: PyOnceLock<Collector> = PyOnceLock::new();
    COLLECTOR.get_or_try_init(py, || {
        let gc = py.import("gc")?;
        // Absent before 3.13, where there is always a global lock.
        let serial = match py.import("sys")?.getattr("_is_gil_enabled") {
            Ok(enabled) => enabled.call0()?.extract()?,
            Err(_) => true,
        };
        Ok(Collector {
            serial,
            is_enabled: gc.getattr("isenabled")?.unbind(),
            get_count: gc.getattr("get_count")?.unbind(),
            get_threshold: gc.getattr("get_threshold")?.unbind(),
            set_threshold: gc.getattr("set_threshold")?.unbind(),
        })
    })
}

/// Containers allocated and not yet freed since the last young collection.
fn young_count(py: Python<'_>) -> Option<i64> {
    let count = collector(py).ok()?.get_count.bind(py).call0().ok()?;
    count.get_item(0).ok()?.extract().ok()
}

/// Whether a body is to be measured: the interpreter has its global lock,
/// and automatic collection is on. An application that switched collection
/// off has none to put off, and its threshold is left alone.
fn collecting(py: Python<'_>) -> bool {
    collector(py)
        .and_then(|collector| {
            Ok(collector.serial && collector.is_enabled.bind(py).call0()?.extract()?)
        })
        .unwrap_or(false)
}

/// Switch the allowance on or off for requests parsed from now on. One
/// already granted is still released when its request ends.
pub(crate) fn set_enabled(enabled: bool) {
    ENABLED.store(enabled, Ordering::Release);
}

/// Whether bodies are measured, and the sum of the allowances in force.
pub(crate) fn status(py: Python<'_>) -> (bool, i64) {
    let granted = STATE
        .lock_py_attached(py)
        .unwrap_or_else(PoisonError::into_inner)
        .granted;
    (ENABLED.load(Ordering::Acquire), granted)
}

/// Counts what parsing one body allocates. No Python code runs between
/// `start` and `finish`, and the interpreter's global lock is held
/// throughout, so no collection runs either and the difference is the
/// parse's own. Bodies are only measured where that lock exists.
pub(crate) struct Meter(Option<i64>);

impl Meter {
    /// Begin measuring the parse of a body of `length` bytes, if it is
    /// large enough to be worth it.
    pub(crate) fn start(py: Python<'_>, length: Option<usize>) -> Self {
        let wanted = length.is_some_and(|length| length >= MEASURED_BODY)
            && ENABLED.load(Ordering::Acquire)
            && collecting(py);
        Self(if wanted { young_count(py) } else { None })
    }

    /// The allowance for what was allocated since `start`, if that was
    /// enough to count. The caller keeps it for as long as the request may
    /// hold those objects.
    pub(crate) fn finish(self, py: Python<'_>) -> Option<Allowance> {
        // An unmeasured body asks `gc` nothing.
        let before = self.0?;
        let objects = young_count(py)? - before;
        // Made only once it has been granted: an `Allowance` gives itself
        // back when dropped, so one built and discarded would take from the
        // allowances of other requests, or, for a count that fell during the
        // parse, add one that nothing returns.
        (objects >= MIN_OBJECTS && adjust(py, objects)).then(|| Allowance(objects))
    }
}

/// A raised young threshold, lowered again when this is dropped.
pub(crate) struct Allowance(i64);

impl Drop for Allowance {
    fn drop(&mut self) {
        // An allowance lives in a Python object or in a frame of the thread
        // that parsed the body, so the interpreter is attached here and this
        // only joins it. While the interpreter shuts down there is nothing
        // left to put back.
        Python::try_attach(|py| adjust(py, -self.0));
    }
}

/// Move the young threshold by `change`. False if `gc` could not be asked,
/// in which case nothing was granted.
fn adjust(py: Python<'_>, change: i64) -> bool {
    // An allowance can be dropped by a deallocator that runs while an
    // exception is being raised. The calls below must neither see that
    // exception nor replace it.
    let raised = PyErr::take(py);
    let applied = apply(py, change).is_ok();
    if let Some(raised) = raised {
        raised.restore(py);
    }
    applied
}

fn apply(py: Python<'_>, change: i64) -> PyResult<()> {
    let collector = collector(py)?;
    let mut state = STATE
        .lock_py_attached(py)
        .unwrap_or_else(PoisonError::into_inner);
    let thresholds = collector.get_threshold.bind(py).call0()?;
    let (young, old, oldest): (i64, i64, i64) = thresholds.extract()?;
    if state.last_set != Some(young) {
        state.base = young;
    }
    state.granted = (state.granted + change).max(0);
    let wanted = if state.base <= 0 {
        state.base
    } else {
        state
            .base
            .saturating_add(state.granted)
            .min(i64::from(i32::MAX))
    };
    if wanted != young {
        collector
            .set_threshold
            .bind(py)
            .call1((wanted, old, oldest))?;
    }
    state.last_set = Some(wanted);
    Ok(())
}
