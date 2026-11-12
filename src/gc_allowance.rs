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
//! comes back down when the request lets go or a collection promotes the
//! body out of the young generation. The count and the threshold
//! rise and fall together, so every other allocation meets the same budget
//! as before and collections happen when they would have without the body.
//!
//! A request that keeps its body's objects after it ends (a cache, a
//! global) is no exception: the threshold comes down, the objects still
//! count, and the next allocation finds the budget spent and starts the
//! collection that was put off.
//!
//! This needs the collector of a build that always has the global lock. A
//! free-threaded build has a different one: it does not decide to collect on
//! the young count alone, so moving the threshold does not move its
//! collections, and without the lock other threads allocate, free and
//! collect while a body is parsed. On a free-threaded build, with its GIL
//! switched on or off, no body is measured and neither the threshold nor
//! `gc.callbacks` is touched.
//!
//! The threshold belongs to the application. Each adjustment reads it first:
//! a value other than the one last set here is the application's new choice
//! and becomes the base the allowances are added to. A threshold of zero,
//! which switches automatic collection off, is left at zero.

use std::sync::{
    Mutex, PoisonError,
    atomic::{AtomicBool, Ordering},
};

use bessemer_codec::runtime::FREE_THREADED;
use pyo3::{
    prelude::*,
    sync::{MutexExt, PyOnceLock},
    types::{PyCFunction, PyList, PyTuple},
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
    epoch: 0,
    collecting: false,
});

#[derive(Clone, Copy, PartialEq, Eq)]
struct State {
    /// The application's young threshold, as last seen.
    base: i64,
    /// The sum of the allowances in force.
    granted: i64,
    /// The threshold last set here, which tells this module's own value
    /// from one the application has set since.
    last_set: Option<i64>,
    /// A collection retires every grant from the preceding epoch.
    epoch: u64,
    /// A collection is between its start and its stop. Objects allocated
    /// meanwhile must not earn a grant.
    collecting: bool,
}

struct Collector {
    /// Whether allowances can be made in this process. Two things are
    /// needed. The build is not free-threaded: there the count across a
    /// parse is not the parse's own and collections are not scheduled on the
    /// young count alone. And the four `gc` functions below are the
    /// interpreter's own, because the threshold is set with no Python code
    /// running after the last check of the state.
    usable: bool,
    is_enabled: Py<PyAny>,
    get_count: Py<PyAny>,
    get_threshold: Py<PyAny>,
    set_threshold: Py<PyAny>,
    callbacks: Py<PyList>,
    callback: Py<PyCFunction>,
}

fn collector(py: Python<'_>) -> PyResult<&'static Collector> {
    static COLLECTOR: PyOnceLock<Collector> = PyOnceLock::new();
    COLLECTOR.get_or_try_init(py, || {
        let gc = py.import("gc")?;
        let is_enabled = gc.getattr("isenabled")?;
        let get_count = gc.getattr("get_count")?;
        let get_threshold = gc.getattr("get_threshold")?;
        let set_threshold = gc.getattr("set_threshold")?;
        // The transaction below calls the cached built-in setter with an
        // already allocated tuple. A Python replacement could re-enter it
        // after the final state check, so keep the unmodified GC behaviour
        // when these functions were wrapped before the collector was cached.
        let builtin = [&is_enabled, &get_count, &get_threshold, &set_threshold]
            .iter()
            .all(|function| function.is_instance_of::<PyCFunction>());
        Ok(Collector {
            usable: !FREE_THREADED && builtin,
            is_enabled: is_enabled.unbind(),
            get_count: get_count.unbind(),
            get_threshold: get_threshold.unbind(),
            set_threshold: set_threshold.unbind(),
            callbacks: gc.getattr("callbacks")?.cast_into::<PyList>()?.unbind(),
            callback: wrap_pyfunction!(on_collection, py)?.unbind(),
        })
    })
}

/// Containers allocated and not yet freed since the last young collection.
fn young_count(py: Python<'_>) -> Option<i64> {
    let count = collector(py).ok()?.get_count.bind(py).call0().ok()?;
    count.get_item(0).ok()?.extract().ok()
}

/// Whether a body is to be measured: allowances can be made here, the
/// collection hook is in place, and automatic collection is on. An
/// application that switched collection off has none to put off, and its
/// threshold is left alone.
fn measuring(py: Python<'_>) -> bool {
    collector(py)
        .and_then(|collector| {
            Ok(observe(py, collector)? && collector.is_enabled.bind(py).call0()?.extract()?)
        })
        .unwrap_or(false)
}

/// Switch the allowance on or off for requests parsed from now on. One
/// already granted is still released when its request ends.
///
/// Returns why no allowance will be made although one was asked for, when
/// the reason is not one the setting's documentation already gives. A
/// free-threaded build is such a documented reason and returns nothing.
pub(crate) fn set_enabled(py: Python<'_>, enabled: bool) -> Option<&'static str> {
    ENABLED.store(enabled, Ordering::Release);
    if !enabled || FREE_THREADED {
        return None;
    }
    // Install before serving requests, outside the Collector initializer.
    match collector(py) {
        Ok(collector) if collector.usable => match observe(py, collector) {
            Ok(_) => None,
            Err(_) => Some("the collection hook could not be added to gc.callbacks"),
        },
        Ok(_) => Some("functions of the gc module have been replaced"),
        Err(_) => Some("the gc module could not be read"),
    }
}

fn snapshot(py: Python<'_>) -> State {
    *STATE
        .lock_py_attached(py)
        .unwrap_or_else(PoisonError::into_inner)
}

/// Make sure the collection hook is in `gc.callbacks`. False where no
/// allowance can be made, and nothing is installed there.
fn observe(py: Python<'_>, collector: &Collector) -> PyResult<bool> {
    if !collector.usable {
        return Ok(false);
    }
    let callbacks = collector.callbacks.bind(py);
    if !callbacks
        .iter()
        .any(|item| item.is(collector.callback.bind(py)))
    {
        // If the application's callback list was cleared, old grants are
        // no longer observable. Retire them before installing the hook again.
        adjust(py, Change::Collection);
        STATE
            .lock_py_attached(py)
            .unwrap_or_else(PoisonError::into_inner)
            .collecting = false;
        // Appended, never inserted. CPython walks this list by index while a
        // collection's callbacks run, and one of them can let this thread
        // in. An entry put in front would move the callback being called up
        // one place, and it would be called again for the same phase. One
        // appended is reached by that same walk, so a collection already
        // under way still retires what is granted here.
        callbacks.append(collector.callback.bind(py))?;
    }
    Ok(true)
}

#[pyfunction]
fn on_collection(py: Python<'_>, phase: &str, _info: &Bound<'_, PyAny>) {
    match phase {
        // A collection of any generation resets the young count and
        // promotes what was young. A body that an earlier callback let in
        // was allocated before that reset, so it is retired here with the
        // rest; one parsed from here to the stop gets no grant.
        "start" => {
            adjust(py, Change::Collection);
        }
        "stop" => {
            STATE
                .lock_py_attached(py)
                .unwrap_or_else(PoisonError::into_inner)
                .collecting = false;
        }
        _ => {}
    }
}

/// Whether bodies are measured in this process, and the sum of the
/// allowances in force. The first is false when the setting is off and also
/// when it is on and no allowance can be made here.
pub(crate) fn status(py: Python<'_>) -> (bool, i64) {
    let measured =
        ENABLED.load(Ordering::Acquire) && collector(py).is_ok_and(|collector| collector.usable);
    (measured, snapshot(py).granted)
}

/// Counts what parsing one body allocates. The interpreter's global lock is
/// held from `start` to `finish`, so the difference is the parse's own
/// unless a collection came between them. One can only when Python code ran
/// while the arguments were built, and the grant's epoch then refuses it.
/// Bodies are only measured where that lock always exists.
pub(crate) struct Meter(Option<(i64, u64)>);

impl Meter {
    /// Begin measuring the parse of a body of `length` bytes, if it is
    /// large enough to be worth it.
    pub(crate) fn start(py: Python<'_>, length: Option<usize>) -> Self {
        let wanted = length.is_some_and(|length| length >= MEASURED_BODY)
            && ENABLED.load(Ordering::Acquire)
            && measuring(py);
        Self(if wanted {
            let state = snapshot(py);
            young_count(py).and_then(|count| {
                let after = snapshot(py);
                (!state.collecting && !after.collecting && state.epoch == after.epoch)
                    .then_some((count, state.epoch))
            })
        } else {
            None
        })
    }

    /// The allowance for what was allocated since `start`, if that was
    /// enough to count. The caller keeps it for as long as the request may
    /// hold those objects.
    pub(crate) fn finish(self, py: Python<'_>) -> Option<Allowance> {
        // An unmeasured body asks `gc` nothing.
        let (before, epoch) = self.0?;
        let objects = young_count(py)? - before;
        // Made only once it has been granted: an `Allowance` gives itself
        // back when dropped, so one built and discarded would take from the
        // allowances of other requests, or, for a count that fell during the
        // parse, add one that nothing returns.
        if objects < MIN_OBJECTS {
            return None;
        }
        adjust(py, Change::Grant { objects, epoch }).map(|epoch| Allowance { objects, epoch })
    }
}

/// A raised young threshold, lowered again when this is dropped.
pub(crate) struct Allowance {
    objects: i64,
    epoch: u64,
}

impl Drop for Allowance {
    fn drop(&mut self) {
        // An allowance lives in a Python object or in a frame of the thread
        // that parsed the body, so the interpreter is attached here and this
        // only joins it. While the interpreter shuts down there is nothing
        // left to put back.
        Python::try_attach(|py| {
            adjust(
                py,
                Change::Release {
                    objects: self.objects,
                    epoch: self.epoch,
                },
            )
        });
    }
}

#[derive(Clone, Copy)]
enum Change {
    Grant { objects: i64, epoch: u64 },
    Release { objects: i64, epoch: u64 },
    Collection,
}

/// Return the epoch only when the adjustment succeeded.
fn adjust(py: Python<'_>, change: Change) -> Option<u64> {
    // An allowance can be dropped by a deallocator that runs while an
    // exception is being raised. The calls below must neither see that
    // exception nor replace it.
    let raised = PyErr::take(py);
    let applied = apply(py, change).ok().flatten();
    if let Some(raised) = raised {
        raised.restore(py);
    }
    applied
}

fn apply(py: Python<'_>, change: Change) -> PyResult<Option<u64>> {
    let collector = collector(py)?;
    loop {
        let before = snapshot(py);
        let mut state = before;
        match change {
            Change::Grant { objects, epoch } => {
                if before.collecting || before.epoch != epoch {
                    return Ok(None);
                }
                state.granted += objects;
            }
            Change::Release { objects, epoch } => {
                // A retired request cannot consume grants issued after the
                // collection, nor reinterpret their application's base.
                if before.epoch != epoch {
                    return Ok(None);
                }
                state.granted = (state.granted - objects).max(0);
            }
            Change::Collection => {
                state.epoch = state.epoch.wrapping_add(1);
                state.collecting = true;
                state.granted = 0;
                if before.granted == 0 {
                    *STATE
                        .lock_py_attached(py)
                        .unwrap_or_else(PoisonError::into_inner) = state;
                    return Ok(Some(state.epoch));
                }
            }
        }
        // Neither Python calls nor Python value destruction occur under
        // STATE: the collection hook locks it, and a collection that began
        // while it was held would wait on its own thread for ever.
        //
        // CPython 3.12 to 3.14 start a collection only at the eval breaker
        // or in PyErr_CheckSignals, so the two calls below cannot start one
        // and the comparison after them finds nothing changed. It is kept
        // because an interpreter that collected inside an allocation would
        // otherwise write a state from before the collection over the one
        // the hook left.
        let thresholds = collector.get_threshold.bind(py).call0()?;
        let (young, old, oldest): (i64, i64, i64) = thresholds.extract()?;
        if state.last_set != Some(young) {
            state.base = young;
        }
        let wanted = if state.base <= 0 {
            state.base
        } else {
            state
                .base
                .saturating_add(state.granted)
                .min(i64::from(i32::MAX))
        };
        let arguments = PyTuple::new(py, [wanted, old, oldest])?;
        if snapshot(py) != before {
            continue;
        }
        if wanted != young {
            // The cached built-in receives an existing exact-int tuple.
            // Even on 3.12's METH_VARARGS setter no argument tuple is
            // allocated after the final epoch check, and no Python runs.
            collector.set_threshold.bind(py).call1(&arguments)?;
        }
        state.last_set = Some(wanted);
        *STATE
            .lock_py_attached(py)
            .unwrap_or_else(PoisonError::into_inner) = state;
        return Ok(Some(state.epoch));
    }
}
