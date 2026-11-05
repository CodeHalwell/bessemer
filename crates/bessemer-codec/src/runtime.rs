//! Safe wrappers over the few CPython runtime calls the native invoker needs:
//! stepping a coroutine without raising `StopIteration` for its result, and
//! setting a context variable inside a fresh `contextvars.Context`.

use pyo3::{ffi, prelude::*};

/// The outcome of resuming a coroutine once.
pub enum Sent<'py> {
    /// The coroutine suspended and yielded this value to its runner.
    Yielded(Bound<'py, PyAny>),
    /// The coroutine finished and returned this value.
    Returned(Bound<'py, PyAny>),
    /// The coroutine raised.
    Raised(PyErr),
}

/// Resume `coroutine` with `value`, as `coroutine.send(value)` would, but
/// report a return as a value rather than a raised `StopIteration`.
pub fn send<'py>(coroutine: &Bound<'py, PyAny>, value: &Bound<'py, PyAny>) -> Sent<'py> {
    let py = coroutine.py();
    let mut result: *mut ffi::PyObject = std::ptr::null_mut();
    // SAFETY: both arguments are live borrowed references held by the caller
    // while the GIL (or the thread state, on free-threaded builds) is attached.
    // PyIter_Send stores a new strong reference in `result` for PYGEN_NEXT and
    // PYGEN_RETURN, and NULL with an exception set for PYGEN_ERROR.
    let status = unsafe { ffi::PyIter_Send(coroutine.as_ptr(), value.as_ptr(), &mut result) };
    match status {
        // SAFETY: `result` is the new strong reference described above.
        ffi::PySendResult::PYGEN_NEXT => {
            Sent::Yielded(unsafe { Bound::from_owned_ptr(py, result) })
        }
        // SAFETY: as above.
        ffi::PySendResult::PYGEN_RETURN => {
            Sent::Returned(unsafe { Bound::from_owned_ptr(py, result) })
        }
        ffi::PySendResult::PYGEN_ERROR => Sent::Raised(PyErr::fetch(py)),
    }
}

/// A new, empty `contextvars.Context`.
pub fn new_context(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    // SAFETY: PyContext_New returns a new strong reference, or NULL with an
    // exception set.
    unsafe { Bound::from_owned_ptr_or_err(py, ffi::PyContext_New()) }
}

/// Set `variable` to `value` inside `context`, which must not be entered on
/// any thread, as `context.run(variable.set, value)` does.
pub fn set_in_context(
    context: &Bound<'_, PyAny>,
    variable: &Bound<'_, PyAny>,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py = context.py();
    // SAFETY: the type checks keep the C calls to the objects they accept.
    // Enter and Exit are paired on this thread; Enter fails without side
    // effects when the context is already entered somewhere.
    unsafe {
        if ffi::PyContext_CheckExact(context.as_ptr()) == 0
            || ffi::PyContextVar_CheckExact(variable.as_ptr()) == 0
        {
            return Err(pyo3::exceptions::PyTypeError::new_err(
                "expected a Context and a ContextVar",
            ));
        }
        if ffi::PyContext_Enter(context.as_ptr()) != 0 {
            return Err(PyErr::fetch(py));
        }
        let token = ffi::PyContextVar_Set(variable.as_ptr(), value.as_ptr());
        let set = if token.is_null() {
            Err(PyErr::fetch(py))
        } else {
            ffi::Py_DECREF(token);
            Ok(())
        };
        if ffi::PyContext_Exit(context.as_ptr()) != 0 {
            return Err(PyErr::fetch(py));
        }
        set
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn coroutines_step_and_return_without_stop_iteration() {
        Python::attach(|py| {
            let module = pyo3::types::PyModule::from_code(
                py,
                c"import types\n@types.coroutine\ndef pause():\n    return (yield 'paused')\nasync def run():\n    got = await pause()\n    return got * 2\nasync def fail():\n    raise ValueError('no')\n",
                c"steps.py",
                c"steps",
            )
            .unwrap();
            let coroutine = module.getattr("run").unwrap().call0().unwrap();
            let none = py.None().into_bound(py);
            match send(&coroutine, &none) {
                Sent::Yielded(value) => assert_eq!(value.extract::<String>().unwrap(), "paused"),
                _ => panic!("expected a suspension"),
            }
            let three = 3i64.into_pyobject(py).unwrap().into_any();
            match send(&coroutine, &three) {
                Sent::Returned(value) => assert_eq!(value.extract::<i64>().unwrap(), 6),
                _ => panic!("expected a return"),
            }
            let failing = module.getattr("fail").unwrap().call0().unwrap();
            match send(&failing, &none) {
                Sent::Raised(error) => {
                    assert!(error.is_instance_of::<pyo3::exceptions::PyValueError>(py))
                }
                _ => panic!("expected an error"),
            }
        });
    }

    #[test]
    fn a_variable_is_set_only_inside_the_new_context() {
        Python::attach(|py| {
            let contextvars = py.import("contextvars").unwrap();
            let variable = contextvars
                .getattr("ContextVar")
                .unwrap()
                .call1(("probe",))
                .unwrap();
            let context = new_context(py).unwrap();
            let value = "inside".into_pyobject(py).unwrap().into_any();
            set_in_context(&context, &variable, &value).unwrap();
            let seen = context.call_method1("get", (&variable,)).unwrap();
            assert_eq!(seen.extract::<String>().unwrap(), "inside");
            assert!(
                variable
                    .call_method1("get", (py.None(),))
                    .unwrap()
                    .is_none()
            );
            assert!(set_in_context(&value, &variable, &value).is_err());
        });
    }
}
