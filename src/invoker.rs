//! The native invoker: starts queued requests on the asyncio loop thread.
//!
//! [`Invoker::drain`] is the loop's reader callback for a [`Bridge`]. For each
//! queued job it builds the arguments, a fresh `contextvars.Context`, an
//! [`Invocation`] (the cancellation handle the worker's code expects) and an
//! eagerly started `asyncio.Task`, all without running Bessemer's Python
//! orchestration code. A handler that never suspends is finished inside the
//! drain, and every response settled during one drain is published together
//! afterwards, so the I/O side is woken once per batch.
//!
//! Routes that qualify for the native lean path (an async handler taking only
//! scalar path, query, header and cookie inputs, without middleware, request
//! metadata or a principal) run through [`Call`], a coroutine written in Rust
//! that drives the handler's coroutine. Their request object is copied from a
//! per-route template and set as `current_request` inside the task's own
//! context, and an exact built-in JSON value or an exact declared Pydantic model
//! returned by the handler is encoded here, straight into the response body.
//! Anything else, and every other route, takes the worker's Python path with
//! exactly the behaviour it had before.

use crate::{
    bridge::{Arguments, Bridge, Job, Pending, Settled, Shared, python_arguments},
    conversion::json_to_python,
    error,
};
use axum::http::StatusCode;
use bessemer_codec::runtime::{
    Guarded, Sent, call_with_keywords, instance_with_dict, new_context, send, set_in_context,
};
use pyo3::{
    exceptions::{PyRuntimeError, PyStopIteration},
    intern,
    prelude::*,
    sync::PyOnceLock,
    types::{PyDict, PyString, PyTuple, PyType},
};
use serde_json::Value;
use std::{
    cmp::Ordering,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering as AtomicOrdering},
    },
};
use tokio::time::Instant;

/// Responses settled while one batch drains, published together after it.
/// Outside a drain (a suspended handler finishing later) they go out at once.
#[derive(Default)]
struct Outbox(Mutex<Batch>);

/// The batch being collected, and whether a drain is collecting one. The
/// buffer keeps its capacity between drains, so a batch allocates only when
/// it outgrows every earlier one.
#[derive(Default)]
struct Batch {
    open: bool,
    settled: Vec<Settled>,
}

impl Outbox {
    fn begin(&self) {
        self.0.lock().unwrap().open = true;
    }

    fn push(&self, settled: Settled) {
        let mut batch = self.0.lock().unwrap();
        if batch.open {
            batch.settled.push(settled);
        } else {
            drop(batch);
            settled.publish();
        }
    }

    fn flush(&self) {
        let mut settled = {
            let mut batch = self.0.lock().unwrap();
            batch.open = false;
            std::mem::take(&mut batch.settled)
        };
        for item in settled.drain(..) {
            item.publish();
        }
        let mut batch = self.0.lock().unwrap();
        if batch.settled.capacity() < settled.capacity() {
            batch.settled = settled;
        }
    }
}

/// A boolean shared between Python and the invoker: the worker keeps its
/// `_accepting` state here so the invoker reads it per job without a Python
/// attribute lookup.
#[pyclass(frozen, module = "bessemer._native")]
pub(crate) struct Switch(AtomicBool);

#[pymethods]
impl Switch {
    #[new]
    #[pyo3(signature = (on = false))]
    fn new(on: bool) -> Self {
        Self(AtomicBool::new(on))
    }

    #[getter]
    fn on(&self) -> bool {
        self.0.load(AtomicOrdering::Acquire)
    }

    #[setter]
    fn set_on(&self, on: bool) {
        self.0.store(on, AtomicOrdering::Release);
    }
}

/// Where the invoker reads whether the worker still accepts work.
enum Accepting {
    /// The worker's native switch.
    Switch(Py<Switch>),
    /// `worker._accepting`, looked up per job.
    Attribute,
}

/// Python objects the invoker calls, looked up once per loop.
struct Hooks {
    loop_: Py<PyAny>,
    /// Whether `loop.get_task_factory()` is asyncio's own, which returns the
    /// loop's `_task_factory` attribute; it is then read directly.
    plain_task_factory: bool,
    worker: Py<PyAny>,
    accepting: Accepting,
    /// `Worker._invoke`, the general path.
    invoke: Py<PyAny>,
    /// `Worker._plain_response`, for results the invoker does not encode.
    plain_response: Py<PyAny>,
    /// `Worker._lean_response`, for streams and responses with background tasks.
    lean_response: Py<PyAny>,
    /// `Worker._lean_failed`, the exception boundary of the lean path.
    lean_failed: Py<PyAny>,
    response_type: Py<PyAny>,
    streaming_type: Py<PyAny>,
    request_type: Py<PyType>,
    current_request: Py<PyAny>,
    task_type: Py<PyAny>,
    /// `asyncio.Task.cancelled`, the type's own method descriptor.
    task_cancelled: Py<PyAny>,
    coroutine_type: Py<PyAny>,
    generator_type: Py<PyAny>,
    max_response_size: usize,
}

fn cancelled_error(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    static TYPE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
    TYPE.get_or_try_init(py, || {
        Ok::<_, PyErr>(py.import("asyncio")?.getattr("CancelledError")?.unbind())
    })
    .map(|value| value.bind(py))
}

/// A numeric bound of a constrained scalar input.
#[derive(Clone, Copy)]
enum Limit {
    Int(i64),
    Float(f64),
}

/// The checks the handler's Python validator would make on a constrained
/// scalar input, made here so the validator can be skipped when they pass.
#[derive(Default)]
struct Checks {
    /// `(limit, strict)`: `ge`/`gt` below, `le`/`lt` above.
    lower: Vec<(Limit, bool)>,
    upper: Vec<(Limit, bool)>,
    min_length: Option<usize>,
    max_length: Option<usize>,
}

/// `inspect.CO_ITERABLE_COROUTINE`.
const CO_ITERABLE_COROUTINE: i64 = 0x100;

const EXACT_FLOAT_INTEGER: u64 = 1 << 53;

fn compare(value: Limit, limit: Limit) -> Option<Ordering> {
    match (value, limit) {
        (Limit::Int(value), Limit::Int(limit)) => Some(value.cmp(&limit)),
        (Limit::Float(value), Limit::Float(limit)) => value.partial_cmp(&limit),
        // Python compares int and float exactly; f64 is exact up to 2**53.
        (Limit::Int(value), Limit::Float(limit)) if value.unsigned_abs() <= EXACT_FLOAT_INTEGER => {
            (value as f64).partial_cmp(&limit)
        }
        (Limit::Float(value), Limit::Int(limit)) if limit.unsigned_abs() <= EXACT_FLOAT_INTEGER => {
            value.partial_cmp(&(limit as f64))
        }
        _ => None,
    }
}

impl Checks {
    fn from_python(constraints: &Bound<'_, PyDict>) -> PyResult<Option<Self>> {
        let mut checks = Checks::default();
        for (name, value) in constraints.iter() {
            let name: String = name.extract()?;
            let limit = || -> Option<Limit> {
                if let Ok(value) = value.cast::<pyo3::types::PyInt>() {
                    value.extract::<i64>().ok().map(Limit::Int)
                } else if let Ok(value) = value.cast::<pyo3::types::PyFloat>() {
                    Some(Limit::Float(value.value()))
                } else {
                    None
                }
            };
            match name.as_str() {
                "ge" | "gt" => match limit() {
                    Some(limit) => checks.lower.push((limit, name == "gt")),
                    None => return Ok(None),
                },
                "le" | "lt" => match limit() {
                    Some(limit) => checks.upper.push((limit, name == "lt")),
                    None => return Ok(None),
                },
                "min_length" => checks.min_length = Some(value.extract()?),
                "max_length" => checks.max_length = Some(value.extract()?),
                _ => return Ok(None),
            }
        }
        Ok(Some(checks))
    }

    /// Whether the Python validator would accept `value` unchanged. False
    /// also when it cannot be decided here; the caller then takes the Python
    /// path, which validates and reports exactly as before.
    fn allows(&self, value: &Value) -> bool {
        match value {
            Value::Null => true,
            Value::Number(number) => {
                if self.min_length.is_some() || self.max_length.is_some() {
                    return false;
                }
                let value = if let Some(value) = number.as_i64() {
                    Limit::Int(value)
                } else if number.as_str().contains(['.', 'e', 'E'])
                    && let Some(value) = number.as_f64().filter(|value| value.is_finite())
                {
                    Limit::Float(value)
                } else {
                    return false;
                };
                self.lower.iter().all(|&(limit, strict)| {
                    matches!(
                        (compare(value, limit), strict),
                        (Some(Ordering::Greater), _) | (Some(Ordering::Equal), false)
                    )
                }) && self.upper.iter().all(|&(limit, strict)| {
                    matches!(
                        (compare(value, limit), strict),
                        (Some(Ordering::Less), _) | (Some(Ordering::Equal), false)
                    )
                })
            }
            Value::String(text) => {
                if !self.lower.is_empty() || !self.upper.is_empty() {
                    return false;
                }
                let length = text.chars().count();
                self.min_length.is_none_or(|minimum| length >= minimum)
                    && self.max_length.is_none_or(|maximum| length <= maximum)
            }
            _ => false,
        }
    }
}

/// One handler input of a native route: its parameter key in the job's
/// arguments, its keyword name and any constraints to check.
struct Binding {
    key: String,
    name: Py<PyString>,
    checks: Option<Checks>,
}

/// A native route's single Pydantic body parameter: the body arrives as raw
/// JSON bytes (`Job::raw_body`) and is built into the model with its plan.
struct BodyBinding {
    name: Py<PyString>,
    plan: Py<crate::ModelPlan>,
}

/// How a native route's return value is encoded.
enum Encode {
    /// No response model: exact built-in JSON values are encoded here.
    Plain,
    /// A declared plain Pydantic model with a compiled plan.
    Model(Py<crate::ModelPlan>),
    /// The worker's `_plain_response` always encodes it.
    Python,
}

/// A route the native lean path can run.
struct NativeRoute {
    call: Py<PyAny>,
    route: Py<PyAny>,
    bindings: Vec<Binding>,
    body: Option<BodyBinding>,
    /// The keyword names of the handler's arguments, in the order
    /// [`NativeRoute::arguments`] produces their values: the body first, then
    /// each binding. None when the handler takes none.
    keywords: Option<Py<PyTuple>>,
    /// The attributes of the route's bare `Request`.
    template: Py<PyDict>,
    status: u16,
    encode: Encode,
}

impl NativeRoute {
    fn traverse(&self, visit: &pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        visit.call(&self.call)?;
        visit.call(&self.route)?;
        visit.call(&self.template)?;
        if let Encode::Model(plan) = &self.encode {
            visit.call(plan)?;
        }
        if let Some(body) = &self.body {
            visit.call(&body.name)?;
            visit.call(&body.plan)?;
        }
        Ok(())
    }

    fn from_python(spec: &Bound<'_, PyAny>) -> PyResult<Option<Self>> {
        let spec = spec.cast::<PyTuple>()?;
        let mut bindings = Vec::new();
        for binding in spec.get_item(2)?.try_iter()? {
            let (key, name, constraints): (String, Bound<'_, PyString>, Option<Bound<'_, PyDict>>) =
                binding?.extract()?;
            let checks = match constraints {
                Some(constraints) => match Checks::from_python(&constraints)? {
                    Some(checks) => Some(checks),
                    None => return Ok(None),
                },
                None => None,
            };
            bindings.push(Binding {
                key,
                name: name.unbind(),
                checks,
            });
        }
        let kind: String = spec.get_item(5)?.extract()?;
        let encode = match kind.as_str() {
            "plain" => Encode::Plain,
            "model" => Encode::Model(spec.get_item(6)?.extract()?),
            _ => Encode::Python,
        };
        let body = match spec.get_item(7)? {
            body if body.is_none() => None,
            body => {
                let (_key, name, plan): (String, Bound<'_, PyString>, Py<crate::ModelPlan>) =
                    body.extract()?;
                Some(BodyBinding {
                    name: name.unbind(),
                    plan,
                })
            }
        };
        let names: Vec<&Bound<'_, PyString>> = body
            .iter()
            .map(|body| body.name.bind(spec.py()))
            .chain(bindings.iter().map(|binding| binding.name.bind(spec.py())))
            .collect();
        let keywords = (!names.is_empty())
            .then(|| PyTuple::new(spec.py(), names).map(Bound::unbind))
            .transpose()?;
        Ok(Some(Self {
            call: spec.get_item(0)?.unbind(),
            route: spec.get_item(1)?.unbind(),
            bindings,
            body,
            keywords,
            template: spec.get_item(3)?.cast_into::<PyDict>()?.unbind(),
            status: spec.get_item(4)?.extract()?,
            encode,
        }))
    }

    /// The values of the handler's keyword arguments, in the order of
    /// `self.keywords`, or None when this request needs the worker's Python
    /// path (a missing input, a constraint to report, or a body the model
    /// plan cannot build). The server has already filled an omitted optional
    /// input with its default.
    ///
    /// A body route's body comes as `raw_body`, already checked by the server
    /// as JSON of an accepted content type within the size limit. It is
    /// built exactly as `bridge::python_arguments` builds it for `_invoke`,
    /// and only an instance of the model is passed: an absent body, or one
    /// whose values Pydantic would still have to coerce or reject, takes the
    /// Python path, which validates it and answers any error as before.
    fn arguments<'py>(
        &self,
        py: Python<'py>,
        values: &Arguments,
        raw_body: Option<&[u8]>,
        parsed_body: &mut Option<Bound<'py, PyAny>>,
    ) -> PyResult<Option<Vec<Py<PyAny>>>> {
        if values.len() != self.bindings.len() {
            return Ok(None);
        }
        let mut arguments =
            Vec::with_capacity(self.bindings.len() + usize::from(self.body.is_some()));
        match (&self.body, raw_body) {
            (None, None) => {}
            (Some(body), Some(bytes)) => {
                let plan = &body.plan.get().0;
                let instance = match bessemer_codec::parse_model(py, plan, bytes) {
                    Some(instance) => instance,
                    None => {
                        let Ok(parsed) = bessemer_codec::parse_value(bytes) else {
                            return Ok(None);
                        };
                        match bessemer_codec::build_model(py, plan, &parsed) {
                            Some(instance) => instance,
                            None => {
                                *parsed_body = Some(json_to_python(py, &parsed)?.into_bound(py));
                                return Ok(None);
                            }
                        }
                    }
                };
                arguments.push(instance.clone().unbind());
                *parsed_body = Some(instance);
            }
            _ => return Ok(None),
        }
        for binding in &self.bindings {
            let Some(value) = values.get(&binding.key) else {
                return Ok(None);
            };
            if let Some(checks) = &binding.checks
                && !checks.allows(value)
            {
                return Ok(None);
            }
            arguments.push(json_to_python(py, value)?);
        }
        Ok(Some(arguments))
    }

    /// The response body for `value`, when it can be encoded natively.
    fn encode(&self, value: &Bound<'_, PyAny>, max_bytes: usize) -> Option<Vec<u8>> {
        match &self.encode {
            Encode::Plain => bessemer_codec::encode_plain_vec(value, max_bytes),
            Encode::Model(plan) => {
                let plan = &plan.get().0;
                if !value.get_type().is(plan.model_class(value.py())) {
                    return None;
                }
                bessemer_codec::encode_model_vec(value, plan, max_bytes)
            }
            Encode::Python => None,
        }
    }
}

struct Control {
    task: Option<Py<PyAny>>,
    cancel_requested: bool,
    cancel_delivered: bool,
    sync_running: bool,
    done: bool,
}

/// The invocation handle of one natively started request: its context and
/// cancellation state, and the response it will publish.
///
/// It has the interface of the worker's `_InvocationControl`: a cancellation
/// request completes only after the work does, and is declined while a
/// synchronous call is running in the thread pool. A lean route's [`Call`]
/// is its own invocation, so each such request allocates one object.
///
/// Its state is [`Guarded`]: every access holds the interpreter lock (the
/// cancel callback runs on the loop through `call_soon_threadsafe`), so a
/// GIL build needs no atomic operations for it, and a free-threaded build
/// takes real locks. Nothing is dropped or called inside an access.
#[pyclass(frozen, subclass, module = "bessemer._native")]
pub(crate) struct Invocation {
    #[pyo3(get)]
    id: u64,
    /// The context its task runs in; the worker's ASGI bridge swaps it while
    /// the route runs inside middleware.
    context: Guarded<Py<PyAny>>,
    loop_: Py<PyAny>,
    control: Guarded<Control>,
    pending: Guarded<Option<Pending>>,
    /// The natively encoded `(status, body)` of a lean handler's result.
    encoded: Guarded<Option<(u16, Vec<u8>)>>,
    outbox: Arc<Outbox>,
}

/// Whether `task.cancelled()`; true if it cannot be asked. An exact
/// `asyncio.Task` is asked through the type's cached method, anything else
/// (a task factory's own type) by name.
fn task_cancelled(hooks: &Hooks, task: &Bound<'_, PyAny>) -> bool {
    let py = task.py();
    let cancelled = if task.get_type().is(hooks.task_type.bind(py)) {
        hooks.task_cancelled.bind(py).call1((task,))
    } else {
        task.call_method0(intern!(py, "cancelled"))
    };
    cancelled
        .and_then(|cancelled| cancelled.is_truthy())
        .unwrap_or(true)
}

impl Invocation {
    /// Publish the finished task's response and drop the task reference.
    fn finish(&self, hooks: &Hooks, task: &Bound<'_, PyAny>) {
        let py = task.py();
        let bound_task = self.control.with(py, |control| {
            control.done = true;
            control.task.take()
        });
        drop(bound_task);
        let Some(pending) = self.pending.with(py, Option::take) else {
            return;
        };
        let mut encoded = self.encoded.with(py, Option::take);
        // A cancellation delivered as the handler returned still wins, as
        // `task.result()` raising CancelledError did on the Python path.
        if encoded.is_some() && task_cancelled(hooks, task) {
            encoded = None;
        }
        let settled = match encoded {
            Some((status, body)) => pending.settle_json(status, body),
            None => pending.settle(task.call_method0(intern!(task.py(), "result"))),
        };
        if let Some(settled) = settled {
            self.outbox.push(settled);
        }
    }
}

#[pymethods]
impl Invocation {
    // Until it finishes, its task refers back to it through the coroutine,
    // so let the garbage collector see that reference. `try_lock`: a
    // collection must never wait on a lock.
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        // The context holds current_request, which the handler may have
        // linked back to its task.
        if let Some(visited) = self
            .context
            .try_with_attached(|context| visit.call(&*context))
        {
            visited?;
        }
        if let Some(visited) = self
            .control
            .try_with_attached(|control| visit.call(&control.task))
        {
            visited?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        // Dropped after the access: releasing it can run Python code.
        let task = self
            .control
            .try_with_attached(|control| control.task.take());
        drop(task);
    }

    /// The `contextvars.Context` the request's task runs in.
    #[getter(_context)]
    fn context(&self, py: Python<'_>) -> Py<PyAny> {
        self.context.with(py, |context| context.clone_ref(py))
    }

    #[setter(_context)]
    fn set_context(&self, py: Python<'_>, context: Py<PyAny>) {
        let previous = self
            .context
            .with(py, |current| std::mem::replace(current, context));
        drop(previous);
    }

    fn done(&self, py: Python<'_>) -> bool {
        self.control.with(py, |control| control.done)
    }

    fn cancel(slf: &Bound<'_, Self>) -> PyResult<bool> {
        let this = slf.get();
        let decided = this.control.with(slf.py(), |control| {
            if control.done {
                return Err(false);
            }
            if control.sync_running {
                // The running call cannot be interrupted. Remember the deadline
                // so later dependencies/awaitables do not start fresh user work.
                control.cancel_requested = true;
                return Err(false);
            }
            if control.cancel_requested {
                return Err(true);
            }
            control.cancel_requested = true;
            Ok(control.task.is_some())
        });
        let bound = match decided {
            Ok(bound) => bound,
            Err(answer) => return Ok(answer),
        };
        if bound {
            this.loop_.bind(slf.py()).call_method1(
                intern!(slf.py(), "call_soon_threadsafe"),
                (slf.getattr(intern!(slf.py(), "_cancel_task"))?,),
            )?;
        }
        Ok(true)
    }

    fn _cancel_task(&self, py: Python<'_>) -> PyResult<()> {
        let task = self.control.with(py, |control| {
            let deliver =
                control.cancel_requested && !control.sync_running && !control.cancel_delivered;
            if deliver {
                control.cancel_delivered = true;
            }
            control
                .task
                .as_ref()
                .filter(|_| deliver)
                .map(|task| task.clone_ref(py))
        });
        if let Some(task) = task {
            let task = task.bind(py);
            if !task.call_method0(intern!(py, "done"))?.is_truthy()? {
                task.call_method0(intern!(py, "cancel"))?;
            }
        }
        Ok(())
    }

    fn _bind(&self, py: Python<'_>, task: Py<PyAny>) -> PyResult<()> {
        let previous = self.control.with(py, |control| control.task.replace(task));
        drop(previous);
        self._cancel_task(py)
    }

    fn _begin_sync(&self, py: Python<'_>) -> bool {
        self.control.with(py, |control| {
            if control.cancel_requested {
                return false;
            }
            control.sync_running = true;
            true
        })
    }

    fn _end_sync(&self, py: Python<'_>) {
        self.control
            .with(py, |control| control.sync_running = false);
    }

    fn _raise_if_cancel_requested(&self, py: Python<'_>) -> PyResult<()> {
        if self.control.with(py, |control| control.cancel_requested) {
            return Err(PyErr::from_value(cancelled_error(py)?.call0()?));
        }
        Ok(())
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Stage {
    /// Driving the handler's own coroutine.
    Handler,
    /// Driving a worker coroutine that finishes the response or the error.
    Fallback,
    Done,
}

/// The handler call a lean task makes on its first step, inside its own
/// task and context, as `_invoke` did: the handler and its keyword arguments.
type HandlerCall = (Py<PyAny>, Vec<Py<PyAny>>);

struct Step {
    stage: Stage,
    /// The handler call not made yet.
    start: Option<HandlerCall>,
    /// The coroutine being driven; None before the first step and once done.
    coroutine: Option<Py<PyAny>>,
    /// Whether it is executing right now, for reentry and `cr_running`.
    running: bool,
}

enum Resume<'py> {
    Send(Bound<'py, PyAny>),
    Throw(Bound<'py, PyTuple>),
}

/// What follows the handler's coroutine.
enum Next<'py> {
    /// The task's result: an envelope, or None when encoded natively.
    Done(Option<Py<PyAny>>),
    /// A worker coroutine to drive next.
    Delegate(Bound<'py, PyAny>),
}

enum Outcome {
    Yielded(Py<PyAny>),
    Returned(Option<Py<PyAny>>),
}

/// The coroutine a lean route's task runs: the handler's coroutine, then its
/// result encoded natively, or the worker's Python code for anything else.
///
/// It behaves as the worker's `_invoke` does for the same route: a
/// cancellation propagates, any other exception from the handler goes through
/// the application's exception handlers, and the task's result is the
/// response envelope (None when the body was encoded here).
///
/// It is also the request's [`Invocation`], which it extends.
#[pyclass(frozen, extends = Invocation, module = "bessemer._native")]
pub(crate) struct Call {
    step: Guarded<Step>,
    route: Arc<NativeRoute>,
    /// The invoker, for its hooks: a reference the garbage collector sees,
    /// unlike a shared `Arc`, so cycles through the worker stay collectable.
    invoker: Py<Invoker>,
    request: Py<PyAny>,
}

impl Call {
    fn hooks(&self) -> &Hooks {
        &self.invoker.get().hooks
    }

    fn resume<'py>(
        &self,
        invocation: &Bound<'py, Invocation>,
        mut input: Resume<'py>,
    ) -> PyResult<Outcome> {
        let py = invocation.py();
        loop {
            let taken = self.step.with(py, |step| {
                if step.running {
                    return Err("coroutine is already executing");
                }
                let start = step.start.take();
                let coroutine = step.coroutine.as_ref().map(|item| item.clone_ref(py));
                if start.is_none() && coroutine.is_none() {
                    return Err("cannot reuse already awaited coroutine");
                }
                step.running = true;
                Ok((step.stage, start, coroutine))
            });
            let (stage, start, coroutine) = match taken {
                Ok((stage, start, coroutine)) => {
                    (stage, start, coroutine.map(|item| item.into_bound(py)))
                }
                Err(message) => return Err(PyRuntimeError::new_err(message)),
            };
            let sent = match (start, input, coroutine) {
                (Some(_), Resume::Throw(arguments), _) => {
                    // Thrown into before it started: nothing ran, so it ends here.
                    self.finish(py);
                    return Err(match arguments.get_item(0) {
                        Ok(error) => PyErr::from_value(error),
                        Err(error) => error,
                    });
                }
                (Some((call, arguments)), Resume::Send(value), _) => {
                    match self.call_handler(py, call, arguments) {
                        Ok(coroutine) => {
                            let previous = self.step.with(py, |step| {
                                step.coroutine.replace(coroutine.clone().unbind())
                            });
                            drop(previous);
                            send(&coroutine, &value)
                        }
                        Err(error) => Sent::Raised(error),
                    }
                }
                (None, _, None) => unreachable!("checked above"),
                (None, input, Some(coroutine)) => match input {
                    Resume::Send(value) => send(&coroutine, &value),
                    // As `await` does, an iterator without `throw` gets the
                    // exception raised at the suspension point instead.
                    Resume::Throw(arguments)
                        if !coroutine.hasattr(intern!(py, "throw")).unwrap_or(false) =>
                    {
                        Sent::Raised(match arguments.get_item(0) {
                            Ok(error) => PyErr::from_value(error),
                            Err(error) => error,
                        })
                    }
                    Resume::Throw(arguments) => {
                        match coroutine.call_method1(intern!(py, "throw"), arguments) {
                            Ok(value) => Sent::Yielded(value),
                            Err(error) if error.is_instance_of::<PyStopIteration>(py) => {
                                match error.value(py).getattr(intern!(py, "value")) {
                                    Ok(value) => Sent::Returned(value),
                                    Err(error) => Sent::Raised(error),
                                }
                            }
                            Err(error) => Sent::Raised(error),
                        }
                    }
                },
            };
            let next = match sent {
                Sent::Yielded(value) => {
                    self.step.with(py, |step| step.running = false);
                    return Ok(Outcome::Yielded(value.unbind()));
                }
                Sent::Returned(value) if stage == Stage::Handler => {
                    self.returned(invocation, &value)
                }
                Sent::Returned(value) => Ok(Next::Done(Some(value.unbind()))),
                Sent::Raised(error) if stage == Stage::Handler => {
                    match cancelled_error(py).map(|cancelled| error.is_instance(py, cancelled)) {
                        Ok(false) => self.failed(invocation, error).map(Next::Delegate),
                        _ => Err(error),
                    }
                }
                Sent::Raised(error) => Err(error),
            };
            let next = match next {
                Ok(Next::Delegate(next)) => next,
                Ok(Next::Done(envelope)) => {
                    self.finish(py);
                    return Ok(Outcome::Returned(envelope));
                }
                Err(error) => {
                    self.finish(py);
                    return Err(error);
                }
            };
            let previous = self.step.with(py, |step| {
                step.stage = Stage::Fallback;
                step.running = false;
                step.coroutine.replace(next.unbind())
            });
            drop(previous);
            input = Resume::Send(py.None().into_bound(py));
        }
    }

    /// Call the handler and return what to drive: its coroutine, or the
    /// iterator of any other awaitable it returned, as `await` would.
    fn call_handler<'py>(
        &self,
        py: Python<'py>,
        call: Py<PyAny>,
        arguments: Vec<Py<PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        // Every argument goes by keyword, as `_invoke`'s `**kwargs` call
        // passed it, so the binding is the same; only the dict is gone.
        let awaitable = match &self.route.keywords {
            Some(keywords) if !arguments.is_empty() => {
                call_with_keywords(call.bind(py), &arguments, keywords.bind(py))?
            }
            _ => call.bind(py).call0()?,
        };
        if awaitable.is_exact_instance(self.hooks().coroutine_type.bind(py)) {
            return Ok(awaitable);
        }
        // A generator-based coroutine (`types.coroutine`) is awaitable through
        // its code flag rather than `__await__`.
        if awaitable.is_exact_instance(self.hooks().generator_type.bind(py))
            && awaitable
                .getattr(intern!(py, "gi_code"))?
                .getattr(intern!(py, "co_flags"))?
                .extract::<i64>()?
                & CO_ITERABLE_COROUTINE
                != 0
        {
            return Ok(awaitable);
        }
        awaitable.call_method0(intern!(py, "__await__"))
    }

    /// Whether it has returned or raised.
    fn finished(&self, py: Python<'_>) -> bool {
        self.step.with(py, |step| step.stage == Stage::Done)
    }

    fn finish(&self, py: Python<'_>) {
        let released = self.step.with(py, |step| {
            step.stage = Stage::Done;
            step.running = false;
            (step.start.take(), step.coroutine.take())
        });
        drop(released);
    }

    /// An attribute of the coroutine being driven, or `default` when done.
    fn delegated(
        &self,
        py: Python<'_>,
        name: &Bound<'_, PyString>,
        default: Py<PyAny>,
    ) -> Py<PyAny> {
        let coroutine = self.step.with(py, |step| {
            step.coroutine
                .as_ref()
                .map(|coroutine| coroutine.clone_ref(py))
        });
        coroutine
            .and_then(|coroutine| coroutine.bind(py).getattr(name).ok())
            .map_or(default, Bound::unbind)
    }

    /// Handle the handler's return value: encode it natively, turn it into
    /// an envelope with `_plain_response`, or hand it to a worker coroutine.
    fn returned<'py>(
        &self,
        invocation: &Bound<'py, Invocation>,
        value: &Bound<'py, PyAny>,
    ) -> PyResult<Next<'py>> {
        let py = value.py();
        let hooks = self.hooks();
        if let Some(body) = self.route.encode(value, hooks.max_response_size) {
            let previous = invocation
                .get()
                .encoded
                .with(py, |encoded| encoded.replace((self.route.status, body)));
            drop(previous);
            return Ok(Next::Done(None));
        }
        let route = self.route.route.bind(py);
        let attempt = || -> PyResult<Next<'py>> {
            if value.is_instance(hooks.response_type.bind(py))?
                && (value.is_instance(hooks.streaming_type.bind(py))?
                    || !value.getattr(intern!(py, "background"))?.is_none())
            {
                return Ok(Next::Delegate(hooks.lean_response.bind(py).call1((
                    route,
                    value,
                    self.request.bind(py),
                    invocation,
                ))?));
            }
            let envelope = hooks.plain_response.bind(py).call1((route, value))?;
            Ok(Next::Done(Some(envelope.unbind())))
        };
        match attempt() {
            Ok(next) => Ok(next),
            // As in the worker, a failure here is the route's exception.
            Err(error) => Ok(Next::Delegate(self.failed(invocation, error)?)),
        }
    }

    /// The worker coroutine that answers the handler's exception.
    fn failed<'py>(
        &self,
        invocation: &Bound<'py, Invocation>,
        error: PyErr,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = invocation.py();
        self.hooks().lean_failed.bind(py).call1((
            self.route.route.bind(py),
            self.request.bind(py),
            error.into_value(py),
            invocation,
        ))
    }
}

#[pymethods]
impl Call {
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        visit.call(&self.invoker)?;
        visit.call(&self.request)?;
        let visited = self.step.try_with_attached(|step| {
            if let Some((call, arguments)) = &step.start {
                visit.call(call)?;
                for argument in arguments {
                    visit.call(argument)?;
                }
            }
            visit.call(&step.coroutine)
        });
        visited.unwrap_or(Ok(()))
    }

    // Declared so the type has a `tp_clear`: the invocation's, which runs
    // first and drops the task, is what breaks a cycle through it.
    fn __clear__(&self) {}

    fn __await__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __next__(slf: &Bound<'_, Self>) -> PyResult<Option<Py<PyAny>>> {
        let none = slf.py().None().into_bound(slf.py());
        match slf.get().resume(slf.as_super(), Resume::Send(none))? {
            Outcome::Yielded(value) => Ok(Some(value)),
            Outcome::Returned(None) => Ok(None),
            Outcome::Returned(Some(value)) => Err(PyStopIteration::new_err((value,))),
        }
    }

    fn send(slf: &Bound<'_, Self>, value: Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        match slf.get().resume(slf.as_super(), Resume::Send(value))? {
            Outcome::Yielded(value) => Ok(value),
            Outcome::Returned(value) => Err(PyStopIteration::new_err((value,))),
        }
    }

    #[pyo3(signature = (*arguments))]
    fn throw(slf: &Bound<'_, Self>, arguments: Bound<'_, PyTuple>) -> PyResult<Py<PyAny>> {
        match slf.get().resume(slf.as_super(), Resume::Throw(arguments))? {
            Outcome::Yielded(value) => Ok(value),
            Outcome::Returned(value) => Err(PyStopIteration::new_err((value,))),
        }
    }

    fn close(&self, py: Python<'_>) -> PyResult<()> {
        let coroutine = self.step.with(py, |step| {
            if step.running {
                return None;
            }
            step.stage = Stage::Done;
            Some(step.coroutine.take())
        });
        let Some(coroutine) = coroutine else {
            return Err(PyRuntimeError::new_err("coroutine is already executing"));
        };
        if let Some(coroutine) = coroutine {
            coroutine.bind(py).call_method0(intern!(py, "close"))?;
        }
        Ok(())
    }

    // Coroutine introspection, as `inspect.getcoroutinestate` and libraries
    // such as anyio read it from `Task.get_coro()`: the state is that of the
    // coroutine being driven.

    #[getter]
    fn cr_running(&self, py: Python<'_>) -> bool {
        self.step.with(py, |step| step.running)
    }

    #[getter]
    fn cr_suspended(&self, py: Python<'_>) -> PyResult<bool> {
        if self.cr_running(py) {
            return Ok(false);
        }
        self.delegated(
            py,
            intern!(py, "cr_suspended"),
            false.into_pyobject(py)?.to_owned().into_any().unbind(),
        )
        .bind(py)
        .is_truthy()
    }

    #[getter]
    fn cr_frame(&self, py: Python<'_>) -> Py<PyAny> {
        self.delegated(py, intern!(py, "cr_frame"), py.None())
    }

    #[getter]
    fn cr_await(&self, py: Python<'_>) -> Py<PyAny> {
        self.delegated(py, intern!(py, "cr_await"), py.None())
    }

    #[getter]
    fn cr_code(&self, py: Python<'_>) -> Py<PyAny> {
        self.delegated(py, intern!(py, "cr_code"), py.None())
    }

    #[getter]
    fn __name__(&self, py: Python<'_>) -> Py<PyAny> {
        self.delegated(
            py,
            intern!(py, "__name__"),
            intern!(py, "Call").clone().into_any().unbind(),
        )
    }

    #[getter]
    fn __qualname__(&self, py: Python<'_>) -> Py<PyAny> {
        self.delegated(
            py,
            intern!(py, "__qualname__"),
            intern!(py, "Call").clone().into_any().unbind(),
        )
    }
}

/// Runs the jobs of one [`Bridge`] on its asyncio loop.
#[pyclass(frozen, module = "bessemer._native")]
pub(crate) struct Invoker {
    shared: Arc<Shared>,
    hooks: Hooks,
    routes: Vec<Option<Arc<NativeRoute>>>,
    /// The lane's `{task: invocation}` of requests still running.
    tasks: Py<PyDict>,
    /// The lane's `{job id: invocation}`, for cancellation requests.
    invocations: Py<PyDict>,
    outbox: Arc<Outbox>,
    /// `{"loop": loop, "eager_start": True, "context": None}`, the options
    /// of an eager task. `asyncio.Task` has no vectorcall, so a call builds
    /// its keyword dict anyway; reusing this one costs a single store per
    /// task. The context is stored for each task and cleared when the batch
    /// ends ([`Invoker::release_context`]), not after every task.
    eager_options: Py<PyDict>,
    /// Whether `eager_options` holds a context.
    eager_dirty: AtomicBool,
    /// The batch buffers, swapped with the bridge's on each drain so neither
    /// side allocates for a new batch.
    spare: Mutex<(Vec<Job>, Vec<u64>)>,
}

/// A job ready to start: its handler call and invocation.
struct Start<'py> {
    coroutine: Bound<'py, PyAny>,
    /// The coroutine, when it is a lean route's [`Call`].
    call: Option<Bound<'py, Call>>,
    invocation: Bound<'py, Invocation>,
}

impl Invoker {
    fn start_job<'py>(
        &self,
        invoker: &Bound<'py, Self>,
        job: Job,
        now: Instant,
    ) -> Result<Start<'py>, Option<Pending>> {
        let py = invoker.py();
        let Job {
            id,
            route_id,
            arguments,
            raw_body,
            uploads,
            metadata,
            deadline,
            pending,
        } = job;
        let native = self
            .routes
            .get(route_id)
            .and_then(Option::as_ref)
            .filter(|_| {
                uploads.is_empty()
                    && metadata.is_none()
                    && !arguments
                        .iter()
                        .any(|(key, _)| key.starts_with("__bessemer_"))
            });
        let mut parsed_body = None;
        let prepared = match native {
            Some(route) => route
                .arguments(py, &arguments, raw_body.as_deref(), &mut parsed_body)
                .map(|values| values.map(|values| (route, values))),
            None => Ok(None),
        };
        let (native, values) = match prepared {
            Ok(Some((route, values))) => (Some((route, values)), None),
            Ok(None) => {
                let body_plan = self.shared.body_plan(route_id);
                match python_arguments(
                    py,
                    &arguments,
                    raw_body,
                    parsed_body,
                    uploads,
                    metadata,
                    body_plan,
                ) {
                    Ok(values) => (None, Some(values)),
                    Err(_) => return Err(Some(pending)),
                }
            }
            Err(_) => return Err(Some(pending)),
        };
        drop(arguments);
        // The clock was read once for the batch, when it began; a waiter
        // that has gone, or a deadline that has passed by then, ends the job.
        if now >= deadline || pending.sender.is_closed() {
            self.outbox
                .push(pending.settled(error(StatusCode::GATEWAY_TIMEOUT, "Request timed out")));
            return Err(None);
        }
        let hooks = &self.hooks;
        let context = match new_context(py) {
            Ok(context) => context,
            Err(_) => return Err(Some(pending)),
        };
        let invocation = Invocation {
            id,
            context: Guarded::new(context.clone().unbind()),
            loop_: hooks.loop_.clone_ref(py),
            control: Guarded::new(Control {
                task: None,
                cancel_requested: false,
                cancel_delivered: false,
                sync_running: false,
                done: false,
            }),
            pending: Guarded::new(Some(pending)),
            encoded: Guarded::new(None),
            outbox: self.outbox.clone(),
        };
        if let Some((route, values)) = native {
            let call = self.native_call(invoker, route, values, &context, invocation)?;
            return Ok(Start {
                coroutine: call.clone().into_any(),
                invocation: call.as_super().clone(),
                call: Some(call),
            });
        }
        let invocation = Bound::new(py, invocation).map_err(|_| None)?;
        let values = values.expect("the Python path has its arguments");
        match hooks.invoke.bind(py).call1((route_id, values, &invocation)) {
            Ok(coroutine) => Ok(Start {
                coroutine,
                call: None,
                invocation,
            }),
            Err(_) => Err(invocation.get().pending.with(py, Option::take)),
        }
    }

    /// The [`Call`] of a lean route, which is also the request's invocation.
    fn native_call<'py>(
        &self,
        invoker: &Bound<'py, Self>,
        route: &Arc<NativeRoute>,
        values: Vec<Py<PyAny>>,
        context: &Bound<'py, PyAny>,
        invocation: Invocation,
    ) -> Result<Bound<'py, Call>, Option<Pending>> {
        let py = invoker.py();
        let hooks = &self.hooks;
        // The bare request, copied from the route's template; the context is
        // new, so setting current_request in it needs no reset afterwards.
        let request = || -> PyResult<Bound<'py, PyAny>> {
            let request_type = hooks.request_type.bind(py);
            let attributes = route.template.bind(py).copy()?;
            // The type is checked on every call (a pointer comparison), so a
            // `Request.__new__` added later still runs.
            let request = match instance_with_dict(request_type, &attributes)? {
                Some(request) => request,
                None => {
                    let request =
                        request_type.call_method1(intern!(py, "__new__"), (request_type,))?;
                    request.setattr(intern!(py, "__dict__"), attributes)?;
                    request
                }
            };
            set_in_context(context, hooks.current_request.bind(py), &request)?;
            Ok(request)
        };
        let request = match request() {
            Ok(request) => request,
            Err(_) => return Err(invocation.pending.into_inner()),
        };
        let call = Call {
            step: Guarded::new(Step {
                stage: Stage::Handler,
                start: Some((route.call.clone_ref(py), values)),
                coroutine: None,
                running: false,
            }),
            route: route.clone(),
            invoker: invoker.clone().unbind(),
            request: request.unbind(),
        };
        Bound::new(py, PyClassInitializer::from(invocation).add_subclass(call)).map_err(|_| None)
    }

    /// Start one job's task, finishing it at once when it never suspends.
    fn run<'py>(
        slf: &Bound<'py, Self>,
        start: Start<'py>,
        eager: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<()> {
        let py = slf.py();
        let this = slf.get();
        let Start {
            coroutine,
            call,
            invocation,
        } = start;
        let context = invocation.get().context(py).into_bound(py);
        let context = &context;
        let task = match eager {
            Some(options) => {
                // Replaced in place rather than added and removed, so the
                // reused dict never fills with deleted slots and resizes.
                options.set_item(intern!(py, "context"), context)?;
                this.eager_dirty.store(true, AtomicOrdering::Relaxed);
                this.hooks
                    .task_type
                    .bind(py)
                    .call((coroutine,), Some(options))?
            }
            None => {
                let options = PyDict::new(py);
                options.set_item(intern!(py, "context"), context)?;
                this.hooks.loop_.bind(py).call_method(
                    intern!(py, "create_task"),
                    (coroutine,),
                    Some(&options),
                )?
            }
        };
        // An eagerly started lean task is done exactly when its call is: a
        // call that finished returned or raised into the task, and one that
        // yielded left it pending. Anything else asks the task.
        let done = match (&call, eager) {
            (Some(call), Some(_)) => call.get().finished(py),
            _ => task.call_method0(intern!(py, "done"))?.is_truthy()?,
        };
        if done {
            invocation.get().finish(&this.hooks, &task);
            return Ok(());
        }
        this.tasks.bind(py).set_item(&task, &invocation)?;
        this.invocations
            .bind(py)
            .set_item(invocation.get().id, &invocation)?;
        invocation.get()._bind(py, task.clone().unbind())?;
        // Looked up per task: a bound method cached on the invoker would be a
        // reference cycle through it that the garbage collector cannot see.
        let callback = slf.getattr(intern!(py, "_done"))?;
        task.call_method1(intern!(py, "add_done_callback"), (callback,))?;
        Ok(())
    }

    /// Drop the context the eager options hold, if any.
    fn release_context(&self, py: Python<'_>) -> PyResult<()> {
        if self.eager_dirty.swap(false, AtomicOrdering::Relaxed) {
            self.eager_options
                .bind(py)
                .set_item(intern!(py, "context"), py.None())?;
        }
        Ok(())
    }

    fn reject(&self, pending: Pending, route_id: usize) {
        diagnostic!("Bessemer could not start the handler for route {route_id}");
        self.outbox.push(pending.settled(error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Internal Server Error",
        )));
    }
}

#[pymethods]
impl Invoker {
    /// `routes` has one entry per route id: None, or the native lean spec
    /// `(call, route, ((key, name, constraints), ...), request_attributes,
    /// status, encode, response_plan)` built by the worker.
    #[new]
    fn new(
        bridge: &Bound<'_, Bridge>,
        worker: &Bound<'_, PyAny>,
        loop_: &Bound<'_, PyAny>,
        tasks: Bound<'_, PyDict>,
        invocations: Bound<'_, PyDict>,
        routes: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        let py = worker.py();
        let request = py.import("bessemer.request")?;
        let responses = py.import("bessemer.responses")?;
        let request_type = request.getattr("Request")?.cast_into::<PyType>()?;
        let base_loop = py.import("asyncio.base_events")?.getattr("BaseEventLoop")?;
        let plain_task_factory = loop_
            .get_type()
            .getattr("get_task_factory")?
            .is(base_loop.getattr("get_task_factory")?);
        let accepting = match worker.getattr("_accepting_switch") {
            Ok(switch) => match switch.cast_into::<Switch>() {
                Ok(switch) => Accepting::Switch(switch.unbind()),
                Err(_) => Accepting::Attribute,
            },
            Err(_) => Accepting::Attribute,
        };
        let hooks = Hooks {
            loop_: loop_.clone().unbind(),
            plain_task_factory,
            worker: worker.clone().unbind(),
            accepting,
            invoke: worker.getattr("_invoke")?.unbind(),
            plain_response: worker.getattr("_plain_response")?.unbind(),
            lean_response: worker.getattr("_lean_response")?.unbind(),
            lean_failed: worker.getattr("_lean_failed")?.unbind(),
            response_type: responses.getattr("Response")?.unbind(),
            streaming_type: responses.getattr("StreamingResponse")?.unbind(),
            request_type: request_type.unbind(),
            current_request: request.getattr("current_request")?.unbind(),
            task_type: py.import("asyncio")?.getattr("Task")?.unbind(),
            task_cancelled: py
                .import("asyncio")?
                .getattr("Task")?
                .getattr("cancelled")?
                .unbind(),
            coroutine_type: py.import("types")?.getattr("CoroutineType")?.unbind(),
            generator_type: py.import("types")?.getattr("GeneratorType")?.unbind(),
            max_response_size: worker.getattr("max_response_size")?.extract()?,
        };
        let mut native = Vec::new();
        for spec in routes.try_iter()? {
            let spec = spec?;
            native.push(if spec.is_none() {
                None
            } else {
                NativeRoute::from_python(&spec)?.map(Arc::new)
            });
        }
        let eager_options = PyDict::new(py);
        eager_options.set_item(intern!(py, "loop"), loop_)?;
        eager_options.set_item(intern!(py, "eager_start"), true)?;
        eager_options.set_item(intern!(py, "context"), py.None())?;
        Ok(Self {
            eager_options: eager_options.unbind(),
            eager_dirty: AtomicBool::new(false),
            shared: bridge.get().shared.clone(),
            spare: Mutex::default(),
            hooks,
            routes: native,
            tasks: tasks.unbind(),
            invocations: invocations.unbind(),
            outbox: Arc::new(Outbox::default()),
        })
    }

    /// Take and start all queued work; the loop's reader callback.
    fn drain(slf: &Bound<'_, Self>) -> PyResult<()> {
        let this = slf.get();
        let (mut jobs, mut cancels) = std::mem::take(&mut *this.spare.lock().unwrap());
        this.shared.swap(&mut jobs, &mut cancels);
        if jobs.is_empty() && cancels.is_empty() {
            *this.spare.lock().unwrap() = (jobs, cancels);
            return Ok(());
        }
        this.outbox.begin();
        let started = Self::start_all(slf, &mut jobs, &mut cancels);
        // Holding the last context would keep that request's context
        // variables alive until the next batch.
        let released = this.release_context(slf.py());
        // Publish everything settled in this batch even if starting failed.
        this.outbox.flush();
        // Jobs left by a failed start are dropped here, as before; the empty
        // buffers keep their capacity for the next batch.
        jobs.clear();
        cancels.clear();
        *this.spare.lock().unwrap() = (jobs, cancels);
        started.and(released)
    }

    // Everything the invoker owns, so that cycles through the worker (which
    // holds the lane that holds the reader callback) can be collected. Each
    // route is visited here only: calls share it through an `Arc` but hold
    // the invoker, so no reference is counted twice.
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        let hooks = &self.hooks;
        for item in [
            &hooks.loop_,
            &hooks.worker,
            &hooks.invoke,
            &hooks.plain_response,
            &hooks.lean_response,
            &hooks.lean_failed,
            &hooks.response_type,
            &hooks.streaming_type,
            &hooks.current_request,
            &hooks.task_type,
            &hooks.task_cancelled,
            &hooks.coroutine_type,
            &hooks.generator_type,
        ] {
            visit.call(item)?;
        }
        visit.call(&hooks.request_type)?;
        if let Accepting::Switch(switch) = &hooks.accepting {
            visit.call(switch)?;
        }
        visit.call(&self.tasks)?;
        visit.call(&self.invocations)?;
        visit.call(&self.eager_options)?;
        for route in self.routes.iter().flatten() {
            route.traverse(&visit)?;
        }
        Ok(())
    }

    /// The done callback of a request task that suspended.
    fn _done(&self, py: Python<'_>, task: &Bound<'_, PyAny>) -> PyResult<()> {
        let tasks = self.tasks.bind(py);
        let Some(invocation) = tasks.get_item(task)? else {
            return Ok(());
        };
        tasks.del_item(task)?;
        let invocation = invocation.cast_into::<Invocation>()?;
        let invocations = self.invocations.bind(py);
        let id = invocation.get().id;
        if invocations.contains(id)? {
            invocations.del_item(id)?;
        }
        invocation.get().finish(&self.hooks, task);
        Ok(())
    }
}

impl Invoker {
    fn start_all(
        slf: &Bound<'_, Self>,
        jobs: &mut Vec<Job>,
        cancelled: &mut Vec<u64>,
    ) -> PyResult<()> {
        let py = slf.py();
        let this = slf.get();
        // A batch rarely cancels anything, so a list beats a hashed set.
        cancelled.sort_unstable();
        cancelled.dedup();
        if !jobs.is_empty() {
            // A task factory installed by the application still gets every
            // task; otherwise the handler starts eagerly inside its own task.
            let loop_ = this.hooks.loop_.bind(py);
            let factory = if this.hooks.plain_task_factory {
                loop_.getattr(intern!(py, "_task_factory"))?
            } else {
                loop_.call_method0(intern!(py, "get_task_factory"))?
            };
            let eager = if factory.is_none() {
                Some(this.eager_options.bind(py))
            } else {
                None
            };
            // One clock read for the batch: it starts within microseconds,
            // far inside any request deadline.
            let now = Instant::now();
            for job in jobs.drain(..) {
                let cancel = cancelled.binary_search(&job.id);
                if let Ok(index) = cancel {
                    cancelled.remove(index);
                }
                if cancel.is_ok() || job.pending.sender.is_closed() {
                    // Never started and the waiter has gone: release capacity now.
                    continue;
                }
                if now >= job.deadline {
                    this.outbox.push(
                        job.pending
                            .settled(error(StatusCode::GATEWAY_TIMEOUT, "Request timed out")),
                    );
                    continue;
                }
                let accepting = match &this.hooks.accepting {
                    Accepting::Switch(switch) => switch.get().on(),
                    Accepting::Attribute => this
                        .hooks
                        .worker
                        .bind(py)
                        .getattr(intern!(py, "_accepting"))?
                        .is_truthy()?,
                };
                if !accepting {
                    this.outbox.push(job.pending.settled(error(
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "Internal Server Error",
                    )));
                    continue;
                }
                let route_id = job.route_id;
                match this.start_job(slf, job, now) {
                    Ok(start) => {
                        let invocation = start.invocation.clone();
                        if let Err(failure) = Self::run(slf, start, eager) {
                            failure.print(py);
                            if let Some(pending) = invocation.get().pending.with(py, Option::take) {
                                this.reject(pending, route_id);
                            }
                        }
                    }
                    Err(Some(pending)) => this.reject(pending, route_id),
                    Err(None) => {}
                }
            }
        }
        if !cancelled.is_empty() {
            let invocations = this.invocations.bind(py);
            for &id in cancelled.iter() {
                if let Some(invocation) = invocations.get_item(id)? {
                    Invocation::cancel(invocation.cast::<Invocation>()?)?;
                }
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn checks(lower: &[(Limit, bool)], upper: &[(Limit, bool)]) -> Checks {
        Checks {
            lower: lower.to_vec(),
            upper: upper.to_vec(),
            ..Checks::default()
        }
    }

    fn number(raw: &str) -> Value {
        serde_json::from_str(raw).unwrap()
    }

    #[test]
    fn numeric_checks_match_python_comparisons_or_defer() {
        let between = checks(&[(Limit::Int(1), false)], &[(Limit::Int(10), true)]);
        assert!(between.allows(&number("1")));
        assert!(between.allows(&number("9")));
        assert!(!between.allows(&number("10")));
        assert!(!between.allows(&number("0")));
        assert!(between.allows(&number("9.5")));
        assert!(between.allows(&Value::Null));
        let strict = checks(&[(Limit::Float(0.5), true)], &[]);
        assert!(strict.allows(&number("1")));
        assert!(!strict.allows(&number("0.5")));
        // Beyond 2**53 an int and a float do not compare exactly as f64.
        assert!(!strict.allows(&number("9007199254740993")));
        // Integers outside i64 always take the Python path.
        assert!(!between.allows(&number("99999999999999999999")));
        // A bound of the wrong kind is never decided here.
        assert!(!between.allows(&Value::String("5".into())));
    }

    #[test]
    fn length_checks_count_characters() {
        let short = Checks {
            min_length: Some(2),
            max_length: Some(3),
            ..Checks::default()
        };
        // Three characters, six bytes.
        assert!(short.allows(&Value::String("\u{e9}\u{e9}\u{e9}".into())));
        assert!(!short.allows(&Value::String("a".into())));
        assert!(!short.allows(&Value::String("abcd".into())));
        assert!(!short.allows(&number("2")));
    }
}
