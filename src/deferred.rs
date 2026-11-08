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
    start: Option<Py<PyAny>>,
    handles: Vec<Py<PyAny>>,
    admission: Option<Admission>,
    file: bool,
    in_flight: Option<crate::telemetry::DeliveryMetric>,
}

impl Work {
    fn start(self, sent: bool) {
        let Some(start) = self.start else { return };
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
                start
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
            start: Some(start),
            handles,
            admission,
            file: false,
            in_flight: None,
        })))))
    }

    /// Files without Python finalizers still own admission through delivery.
    pub(crate) fn hold(admission: Admission) -> Self {
        Self(Arc::new(AfterInner(Mutex::new(Some(Work {
            start: None,
            handles: Vec::new(),
            admission: Some(admission),
            file: true,
            in_flight: None,
        })))))
    }

    pub(crate) fn mark_file(&self) {
        if let Some(work) = self.0.0.lock().unwrap().as_mut() {
            work.file = true;
        }
    }

    pub(crate) fn track_file(&self, metrics: &Arc<crate::telemetry::Metrics>) {
        if let Some(work) = self.0.0.lock().unwrap().as_mut()
            && work.file
        {
            work.in_flight = metrics.delivery();
        }
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
