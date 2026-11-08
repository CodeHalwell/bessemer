//! Callback handles: the objects `call_soon`, `call_at` and `add_reader` return.
//!
//! These play the part of `asyncio.Handle` and `asyncio.TimerHandle`. They are
//! not subclasses of those Python classes (a native type cannot inherit from
//! them), which is the same trade uvloop makes.

use std::sync::atomic::{AtomicBool, Ordering};

use bessemer_codec::runtime::Guarded;
use pyo3::exceptions::{PyKeyboardInterrupt, PyRuntimeError, PySystemExit};
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use pyo3::{PyTraverseError, PyVisit};

use crate::core::LoopCore;

pub(crate) struct Callback {
    pub(crate) func: Py<PyAny>,
    pub(crate) args: Py<PyTuple>,
    pub(crate) context: Py<PyAny>,
}

#[pyclass(frozen, module = "bessemer._native", name = "Handle")]
pub struct Handle {
    /// Cleared on cancel, as asyncio does, so a cancelled handle does not keep
    /// its callback (and whatever that references) alive.
    callback: Guarded<Option<Callback>>,
    cancelled: AtomicBool,
    /// The loop, kept only by timer handles so that `cancel` can tell the loop
    /// to count it for heap compaction.
    owner: Guarded<Option<Py<LoopCore>>>,
    pub(crate) when: f64,
    pub(crate) is_timer: bool,
    /// Whether a timer is still in the loop's heap.
    pub(crate) scheduled: AtomicBool,
    /// Where the handle was created, recorded in debug mode only.
    pub(crate) source_traceback: std::sync::OnceLock<Py<PyAny>>,
}

impl Handle {
    pub(crate) fn new(callback: Callback) -> Self {
        Handle {
            callback: Guarded::new(Some(callback)),
            cancelled: AtomicBool::new(false),
            owner: Guarded::new(None),
            when: 0.0,
            is_timer: false,
            scheduled: AtomicBool::new(false),
            source_traceback: std::sync::OnceLock::new(),
        }
    }

    pub(crate) fn new_timer(callback: Callback, when: f64, owner: Py<LoopCore>) -> Self {
        Handle {
            callback: Guarded::new(Some(callback)),
            cancelled: AtomicBool::new(false),
            owner: Guarded::new(Some(owner)),
            when,
            is_timer: true,
            scheduled: AtomicBool::new(true),
            source_traceback: std::sync::OnceLock::new(),
        }
    }

    #[inline]
    pub(crate) fn is_cancelled(&self) -> bool {
        self.cancelled.load(Ordering::Relaxed)
    }

    /// A timer leaves the heap: drop its reference to the loop.
    pub(crate) fn unschedule(&self, py: Python<'_>) {
        self.scheduled.store(false, Ordering::Relaxed);
        let owner = self.owner.with(py, Option::take);
        drop(owner);
    }

    /// Run the callback in its context. An exception other than SystemExit
    /// and KeyboardInterrupt goes to the loop's exception handler.
    pub(crate) fn run(slf: &Bound<'_, Handle>, loop_: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = slf.py();
        let this = slf.get();
        let callback = this.callback.with(py, |stored| {
            stored.as_ref().map(|cb| {
                (
                    cb.func.clone_ref(py),
                    cb.args.clone_ref(py),
                    cb.context.clone_ref(py),
                )
            })
        });
        let Some((func, args, context)) = callback else {
            return Ok(());
        };
        if this.is_cancelled() {
            return Ok(());
        }
        match run_in_context(py, &context, &func, &args) {
            Ok(()) => Ok(()),
            Err(err) => {
                if err.is_instance_of::<PySystemExit>(py)
                    || err.is_instance_of::<PyKeyboardInterrupt>(py)
                {
                    return Err(err);
                }
                // Formatting the report is the cold path; Python does it.
                loop_.call_method1(
                    pyo3::intern!(py, "_bessemer_report_handle_error"),
                    (slf, err.value(py), func, args),
                )?;
                Ok(())
            }
        }
    }
}

/// `context.run(func, *args)` without the method lookup and argument packing.
pub(crate) fn run_in_context(
    py: Python<'_>,
    context: &Py<PyAny>,
    func: &Py<PyAny>,
    args: &Py<PyTuple>,
) -> PyResult<()> {
    unsafe {
        if ffi::PyContext_Enter(context.as_ptr()) != 0 {
            return Err(PyErr::fetch(py));
        }
        let result = ffi::PyObject_Call(func.as_ptr(), args.as_ptr(), std::ptr::null_mut());
        let error = if result.is_null() {
            Some(PyErr::fetch(py))
        } else {
            ffi::Py_DECREF(result);
            None
        };
        if ffi::PyContext_Exit(context.as_ptr()) != 0 {
            // Exiting only fails if the context was not entered, which would
            // be a bug here; report it rather than swallow it.
            let exit_error = PyErr::fetch(py);
            return Err(error.unwrap_or(exit_error));
        }
        match error {
            Some(err) => Err(err),
            None => Ok(()),
        }
    }
}

/// The context a new callback runs in: the one given, or a copy of the
/// current one, as asyncio does.
pub(crate) fn callback_context(py: Python<'_>, context: Option<Py<PyAny>>) -> PyResult<Py<PyAny>> {
    match context {
        Some(ctx) if !ctx.is_none(py) => Ok(ctx),
        _ => unsafe {
            let ctx = ffi::PyContext_CopyCurrent();
            if ctx.is_null() {
                return Err(PyErr::fetch(py));
            }
            Ok(Bound::from_owned_ptr(py, ctx).unbind())
        },
    }
}

#[pymethods]
impl Handle {
    fn cancel(slf: &Bound<'_, Self>) {
        let this = slf.get();
        if this.cancelled.swap(true, Ordering::Relaxed) {
            return;
        }
        if this.is_timer && this.scheduled.load(Ordering::Relaxed) {
            let owner = this.owner.with(slf.py(), |owner| {
                owner.as_ref().map(|o| o.clone_ref(slf.py()))
            });
            if let Some(owner) = owner {
                owner.get().timer_cancelled();
            }
        }
        // Drop outside the access: its destructor may run Python.
        let callback = this.callback.with(slf.py(), Option::take);
        drop(callback);
    }

    fn cancelled(&self) -> bool {
        self.is_cancelled()
    }

    #[getter]
    fn _cancelled(&self) -> bool {
        self.is_cancelled()
    }

    #[getter]
    fn _scheduled(&self) -> bool {
        self.scheduled.load(Ordering::Relaxed)
    }

    #[getter]
    fn _callback(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.callback
            .with(py, |stored| stored.as_ref().map(|cb| cb.func.clone_ref(py)))
    }

    #[getter]
    fn _args(&self, py: Python<'_>) -> Option<Py<PyTuple>> {
        self.callback
            .with(py, |stored| stored.as_ref().map(|cb| cb.args.clone_ref(py)))
    }

    fn get_context(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.callback.with(py, |stored| {
            stored.as_ref().map(|cb| cb.context.clone_ref(py))
        })
    }

    fn when(&self) -> PyResult<f64> {
        if !self.is_timer {
            return Err(PyRuntimeError::new_err("only timer handles have a time"));
        }
        Ok(self.when)
    }

    #[getter]
    fn _when(&self) -> f64 {
        self.when
    }

    #[getter]
    fn _source_traceback(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.source_traceback.get().map(|tb| tb.clone_ref(py))
    }

    /// Run the callback now, as asyncio's `Handle._run` does. The loop's own
    /// queue does not go through here.
    fn _run(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py = slf.py();
        let callback = slf.get().callback.with(py, |stored| {
            stored.as_ref().map(|cb| {
                (
                    cb.func.clone_ref(py),
                    cb.args.clone_ref(py),
                    cb.context.clone_ref(py),
                )
            })
        });
        let Some((func, args, context)) = callback else {
            return Ok(());
        };
        run_in_context(py, &context, &func, &args)
    }

    fn __repr__(slf: &Bound<'_, Self>) -> PyResult<String> {
        let py = slf.py();
        let this = slf.get();
        let kind = if this.is_timer {
            "TimerHandle"
        } else {
            "Handle"
        };
        let mut parts = vec![kind.to_string()];
        if this.is_cancelled() {
            parts.push("cancelled".into());
        }
        if this.is_timer {
            parts.push(format!("when={}", this.when));
        }
        let callback = this.callback.with(py, |stored| {
            stored
                .as_ref()
                .map(|cb| (cb.func.clone_ref(py), cb.args.clone_ref(py)))
        });
        if let Some((func, args)) = callback {
            let helpers = py.import("asyncio.format_helpers")?;
            let text: String = helpers
                .call_method1("_format_callback_source", (func, args))?
                .extract()?;
            parts.push(text);
        }
        // Debug mode records where the handle was created; asyncio's repr
        // ends with its innermost frame, and the slow callback warning
        // shows this text.
        if let Some(stack) = this.source_traceback.get() {
            let stack = stack.bind(py);
            if let Ok(len) = stack.len()
                && len > 0
            {
                let frame = stack.get_item(len - 1)?;
                let file: String = frame.get_item(0)?.str()?.extract()?;
                let line: String = frame.get_item(1)?.str()?.extract()?;
                parts.push(format!("created at {file}:{line}"));
            }
        }
        Ok(format!("<{}>", parts.join(" ")))
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(visited) = self.callback.try_with_attached(|stored| {
            if let Some(cb) = stored.as_ref() {
                visit.call(&cb.func)?;
                visit.call(&cb.args)?;
                visit.call(&cb.context)?;
            }
            Ok(())
        }) {
            visited?;
        }
        if let Some(tb) = self.source_traceback.get() {
            visit.call(tb)?;
        }
        if let Some(visited) = self.owner.try_with_attached(|owner| visit.call(&*owner)) {
            visited?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let callback = self.callback.try_with_attached(Option::take);
        let owner = self.owner.try_with_attached(Option::take);
        drop(callback);
        drop(owner);
    }
}
