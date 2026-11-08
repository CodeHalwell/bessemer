//! Post-response Python work, with admission held through its finalizers.
use crate::Admission;
use pyo3::{
    prelude::*,
    types::{PyCFunction, PyDict, PyList, PyTuple},
};
use std::sync::{Arc, Mutex};

#[derive(Clone)]
pub(crate) struct AfterResponse(Arc<AfterInner>);

struct AfterInner(Mutex<Option<Work>>);

struct Work {
    start: Py<PyAny>,
    handles: Vec<Py<PyAny>>,
    admission: Option<Admission>,
}

impl Work {
    fn start(self, sent: bool) {
        Python::attach(|py| {
            let held = Mutex::new(self.admission);
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
                let handles = PyList::new(py, self.handles)?;
                self.start
                    .bind(py)
                    .call1((handles, sent))?
                    .call_method1("add_done_callback", (callback,))
            });
            if let Err(error) = scheduled {
                diagnostic!("Bessemer could not schedule post-response cleanup: {error}");
            }
        });
    }
}

impl AfterResponse {
    pub(crate) fn new(
        start: Py<PyAny>,
        handles: Vec<Py<PyAny>>,
        admission: Option<Admission>,
    ) -> Self {
        Self(Arc::new(AfterInner(Mutex::new(Some(Work {
            start,
            handles,
            admission,
        })))))
    }

    pub(crate) fn finish(self, sent: bool) {
        let work = self.0.0.lock().unwrap().take();
        if let Some(work) = work {
            work.start(sent);
        }
    }
}

impl Drop for AfterInner {
    fn drop(&mut self) {
        // Arc invokes this exactly once, including concurrent clone drops.
        if let Some(work) = self.0.get_mut().unwrap().take() {
            work.start(false);
        }
    }
}
