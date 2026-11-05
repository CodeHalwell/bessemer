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
use bessemer_codec::runtime::{Sent, new_context, send, set_in_context};
use pyo3::{
    exceptions::{PyRuntimeError, PyStopIteration},
    intern,
    prelude::*,
    sync::PyOnceLock,
    types::{PyDict, PyString, PyTuple},
};
use serde_json::Value;
use std::{
    cmp::Ordering,
    sync::{Arc, Mutex},
};
use tokio::time::Instant;

/// Responses settled while one batch drains, published together after it.
/// Outside a drain (a suspended handler finishing later) they go out at once.
#[derive(Default)]
struct Outbox(Mutex<Option<Vec<Settled>>>);

impl Outbox {
    fn begin(&self) {
        *self.0.lock().unwrap() = Some(Vec::new());
    }

    fn push(&self, settled: Settled) {
        let mut batch = self.0.lock().unwrap();
        match batch.as_mut() {
            Some(batch) => batch.push(settled),
            None => {
                drop(batch);
                settled.publish();
            }
        }
    }

    fn flush(&self) {
        let batch = self.0.lock().unwrap().take();
        for settled in batch.into_iter().flatten() {
            settled.publish();
        }
    }
}

/// Python objects the invoker calls, looked up once per loop.
struct Hooks {
    loop_: Py<PyAny>,
    worker: Py<PyAny>,
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
    request_type: Py<PyAny>,
    current_request: Py<PyAny>,
    task_type: Py<PyAny>,
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
        Ok(Some(Self {
            call: spec.get_item(0)?.unbind(),
            route: spec.get_item(1)?.unbind(),
            bindings,
            template: spec.get_item(3)?.cast_into::<PyDict>()?.unbind(),
            status: spec.get_item(4)?.extract()?,
            encode,
        }))
    }

    /// The handler's keyword arguments, or None when this request needs the
    /// worker's Python path (a missing input or a constraint to report).
    fn arguments<'py>(
        &self,
        py: Python<'py>,
        values: &Arguments,
    ) -> PyResult<Option<Bound<'py, PyDict>>> {
        if values.len() != self.bindings.len() {
            return Ok(None);
        }
        let arguments = PyDict::new(py);
        for binding in &self.bindings {
            let Some(value) = values.get(&binding.key) else {
                return Ok(None);
            };
            if let Some(checks) = &binding.checks
                && !checks.allows(value)
            {
                return Ok(None);
            }
            arguments.set_item(binding.name.bind(py), json_to_python(py, value)?)?;
        }
        Ok(Some(arguments))
    }

    /// The response body for `value`, when it can be encoded natively.
    fn encode(&self, value: &Bound<'_, PyAny>, max_bytes: usize) -> Option<Vec<u8>> {
        match &self.encode {
            Encode::Plain => bessemer_codec::encode_plain(value, max_bytes, <[u8]>::to_vec),
            Encode::Model(plan) => {
                let plan = &plan.get().0;
                if !value.get_type().is(plan.model_class(value.py())) {
                    return None;
                }
                bessemer_codec::encode_model(value, plan, max_bytes, <[u8]>::to_vec)
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
/// synchronous call is running in the thread pool.
#[pyclass(frozen, module = "bessemer._native")]
pub(crate) struct Invocation {
    #[pyo3(get)]
    id: u64,
    /// The context its task runs in; the worker's ASGI bridge swaps it while
    /// the route runs inside middleware.
    context: Mutex<Py<PyAny>>,
    loop_: Py<PyAny>,
    control: Mutex<Control>,
    pending: Mutex<Option<Pending>>,
    /// The natively encoded `(status, body)` of a lean handler's result.
    encoded: Mutex<Option<(u16, Vec<u8>)>>,
    outbox: Arc<Outbox>,
}

impl Invocation {
    /// Publish the finished task's response and drop the task reference.
    fn finish(&self, task: &Bound<'_, PyAny>) {
        {
            let mut control = self.control.lock().unwrap();
            control.done = true;
            control.task = None;
        }
        let Some(pending) = self.pending.lock().unwrap().take() else {
            return;
        };
        let mut encoded = self.encoded.lock().unwrap().take();
        // A cancellation delivered as the handler returned still wins, as
        // `task.result()` raising CancelledError did on the Python path.
        if encoded.is_some()
            && task
                .call_method0(intern!(task.py(), "cancelled"))
                .and_then(|cancelled| cancelled.is_truthy())
                .unwrap_or(true)
        {
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
        if let Ok(context) = self.context.try_lock() {
            visit.call(&*context)?;
        }
        if let Ok(control) = self.control.try_lock()
            && let Some(task) = &control.task
        {
            visit.call(task)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        if let Ok(mut control) = self.control.try_lock() {
            control.task = None;
        }
    }

    /// The `contextvars.Context` the request's task runs in.
    #[getter(_context)]
    fn context(&self, py: Python<'_>) -> Py<PyAny> {
        self.context.lock().unwrap().clone_ref(py)
    }

    #[setter(_context)]
    fn set_context(&self, context: Py<PyAny>) {
        *self.context.lock().unwrap() = context;
    }

    fn done(&self) -> bool {
        self.control.lock().unwrap().done
    }

    fn cancel(slf: &Bound<'_, Self>) -> PyResult<bool> {
        let this = slf.get();
        let bound = {
            let mut control = this.control.lock().unwrap();
            if control.done {
                return Ok(false);
            }
            if control.sync_running {
                // The running call cannot be interrupted. Remember the deadline
                // so later dependencies/awaitables do not start fresh user work.
                control.cancel_requested = true;
                return Ok(false);
            }
            if control.cancel_requested {
                return Ok(true);
            }
            control.cancel_requested = true;
            control.task.is_some()
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
        let task = {
            let mut control = self.control.lock().unwrap();
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
        };
        if let Some(task) = task {
            let task = task.bind(py);
            if !task.call_method0(intern!(py, "done"))?.is_truthy()? {
                task.call_method0(intern!(py, "cancel"))?;
            }
        }
        Ok(())
    }

    fn _bind(&self, py: Python<'_>, task: Py<PyAny>) -> PyResult<()> {
        self.control.lock().unwrap().task = Some(task);
        self._cancel_task(py)
    }

    fn _begin_sync(&self) -> bool {
        let mut control = self.control.lock().unwrap();
        if control.cancel_requested {
            return false;
        }
        control.sync_running = true;
        true
    }

    fn _end_sync(&self) {
        self.control.lock().unwrap().sync_running = false;
    }

    fn _raise_if_cancel_requested(&self, py: Python<'_>) -> PyResult<()> {
        if self.control.lock().unwrap().cancel_requested {
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
type HandlerCall = (Py<PyAny>, Option<Py<PyDict>>);

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
#[pyclass(frozen, module = "bessemer._native")]
pub(crate) struct Call {
    step: Mutex<Step>,
    route: Arc<NativeRoute>,
    /// The invoker, for its hooks: a reference the garbage collector sees,
    /// unlike a shared `Arc`, so cycles through the worker stay collectable.
    invoker: Py<Invoker>,
    request: Py<PyAny>,
    invocation: Py<Invocation>,
}

impl Call {
    fn hooks(&self) -> &Hooks {
        &self.invoker.get().hooks
    }

    fn resume<'py>(&self, py: Python<'py>, mut input: Resume<'py>) -> PyResult<Outcome> {
        loop {
            let (stage, start, coroutine) = {
                let mut step = self.step.lock().unwrap();
                if step.running {
                    return Err(PyRuntimeError::new_err("coroutine is already executing"));
                }
                let start = step.start.take();
                let coroutine = step.coroutine.as_ref().map(|item| item.clone_ref(py));
                if start.is_none() && coroutine.is_none() {
                    return Err(PyRuntimeError::new_err(
                        "cannot reuse already awaited coroutine",
                    ));
                }
                step.running = true;
                (step.stage, start, coroutine.map(|item| item.into_bound(py)))
            };
            let sent = match (start, input, coroutine) {
                (Some(_), Resume::Throw(arguments), _) => {
                    // Thrown into before it started: nothing ran, so it ends here.
                    self.finish();
                    return Err(match arguments.get_item(0) {
                        Ok(error) => PyErr::from_value(error),
                        Err(error) => error,
                    });
                }
                (Some((call, arguments)), Resume::Send(value), _) => {
                    match self.call_handler(py, call, arguments) {
                        Ok(coroutine) => {
                            self.step.lock().unwrap().coroutine = Some(coroutine.clone().unbind());
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
                    self.step.lock().unwrap().running = false;
                    return Ok(Outcome::Yielded(value.unbind()));
                }
                Sent::Returned(value) if stage == Stage::Handler => self.returned(&value),
                Sent::Returned(value) => Ok(Next::Done(Some(value.unbind()))),
                Sent::Raised(error) if stage == Stage::Handler => {
                    match cancelled_error(py).map(|cancelled| error.is_instance(py, cancelled)) {
                        Ok(false) => self.failed(py, error).map(Next::Delegate),
                        _ => Err(error),
                    }
                }
                Sent::Raised(error) => Err(error),
            };
            let next = match next {
                Ok(Next::Delegate(next)) => next,
                Ok(Next::Done(envelope)) => {
                    self.finish();
                    return Ok(Outcome::Returned(envelope));
                }
                Err(error) => {
                    self.finish();
                    return Err(error);
                }
            };
            let mut step = self.step.lock().unwrap();
            step.stage = Stage::Fallback;
            step.coroutine = Some(next.unbind());
            step.running = false;
            drop(step);
            input = Resume::Send(py.None().into_bound(py));
        }
    }

    /// Call the handler and return what to drive: its coroutine, or the
    /// iterator of any other awaitable it returned, as `await` would.
    fn call_handler<'py>(
        &self,
        py: Python<'py>,
        call: Py<PyAny>,
        arguments: Option<Py<PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let awaitable = match arguments {
            Some(arguments) => call.bind(py).call((), Some(arguments.bind(py)))?,
            None => call.bind(py).call0()?,
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

    fn finish(&self) {
        let mut step = self.step.lock().unwrap();
        step.stage = Stage::Done;
        step.start = None;
        step.coroutine = None;
        step.running = false;
    }

    /// An attribute of the coroutine being driven, or `default` when done.
    fn delegated(
        &self,
        py: Python<'_>,
        name: &Bound<'_, PyString>,
        default: Py<PyAny>,
    ) -> Py<PyAny> {
        let coroutine = self
            .step
            .lock()
            .unwrap()
            .coroutine
            .as_ref()
            .map(|coroutine| coroutine.clone_ref(py));
        coroutine
            .and_then(|coroutine| coroutine.bind(py).getattr(name).ok())
            .map_or(default, Bound::unbind)
    }

    /// Handle the handler's return value: encode it natively, turn it into
    /// an envelope with `_plain_response`, or hand it to a worker coroutine.
    fn returned<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Next<'py>> {
        let py = value.py();
        let hooks = self.hooks();
        if let Some(body) = self.route.encode(value, hooks.max_response_size) {
            *self.invocation.get().encoded.lock().unwrap() = Some((self.route.status, body));
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
                    self.invocation.bind(py),
                ))?));
            }
            let envelope = hooks.plain_response.bind(py).call1((route, value))?;
            Ok(Next::Done(Some(envelope.unbind())))
        };
        match attempt() {
            Ok(next) => Ok(next),
            // As in the worker, a failure here is the route's exception.
            Err(error) => Ok(Next::Delegate(self.failed(py, error)?)),
        }
    }

    /// The worker coroutine that answers the handler's exception.
    fn failed<'py>(&self, py: Python<'py>, error: PyErr) -> PyResult<Bound<'py, PyAny>> {
        self.hooks().lean_failed.bind(py).call1((
            self.route.route.bind(py),
            self.request.bind(py),
            error.into_value(py),
            self.invocation.bind(py),
        ))
    }
}

#[pymethods]
impl Call {
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        visit.call(&self.invoker)?;
        visit.call(&self.invocation)?;
        visit.call(&self.request)?;
        if let Ok(step) = self.step.try_lock() {
            if let Some((call, arguments)) = &step.start {
                visit.call(call)?;
                visit.call(arguments)?;
            }
            visit.call(&step.coroutine)?;
        }
        Ok(())
    }

    fn __await__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __next__(&self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        match self.resume(py, Resume::Send(py.None().into_bound(py)))? {
            Outcome::Yielded(value) => Ok(Some(value)),
            Outcome::Returned(None) => Ok(None),
            Outcome::Returned(Some(value)) => Err(PyStopIteration::new_err((value,))),
        }
    }

    fn send(&self, py: Python<'_>, value: Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        match self.resume(py, Resume::Send(value))? {
            Outcome::Yielded(value) => Ok(value),
            Outcome::Returned(value) => Err(PyStopIteration::new_err((value,))),
        }
    }

    #[pyo3(signature = (*arguments))]
    fn throw(&self, py: Python<'_>, arguments: Bound<'_, PyTuple>) -> PyResult<Py<PyAny>> {
        match self.resume(py, Resume::Throw(arguments))? {
            Outcome::Yielded(value) => Ok(value),
            Outcome::Returned(value) => Err(PyStopIteration::new_err((value,))),
        }
    }

    fn close(&self, py: Python<'_>) -> PyResult<()> {
        let coroutine = {
            let mut step = self.step.lock().unwrap();
            if step.running {
                return Err(PyRuntimeError::new_err("coroutine is already executing"));
            }
            step.stage = Stage::Done;
            step.coroutine.take()
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
    fn cr_running(&self) -> bool {
        self.step.lock().unwrap().running
    }

    #[getter]
    fn cr_suspended(&self, py: Python<'_>) -> PyResult<bool> {
        if self.cr_running() {
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
    /// `{"loop": loop, "eager_start": True}`, the options of an eager task.
    eager_options: Py<PyDict>,
    /// The batch buffers, swapped with the bridge's on each drain so neither
    /// side allocates for a new batch.
    spare: Mutex<(Vec<Job>, Vec<u64>)>,
}

/// A job ready to start: its handler call and invocation.
struct Start<'py> {
    coroutine: Bound<'py, PyAny>,
    invocation: Bound<'py, Invocation>,
}

impl Invoker {
    fn start_job<'py>(
        &self,
        invoker: &Bound<'py, Self>,
        job: Job,
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
                raw_body.is_none()
                    && uploads.is_empty()
                    && metadata.is_none()
                    && !arguments
                        .iter()
                        .any(|(key, _)| key.starts_with("__bessemer_"))
            });
        let prepared = match native {
            Some(route) => route
                .arguments(py, &arguments)
                .map(|values| values.map(|values| (route, values))),
            None => Ok(None),
        };
        let (native, values) = match prepared {
            Ok(Some((route, values))) => (Some(route), values),
            Ok(None) => {
                let body_plan = self.shared.body_plan(route_id);
                match python_arguments(py, &arguments, raw_body, uploads, metadata, body_plan) {
                    Ok(values) => (None, values),
                    Err(_) => return Err(Some(pending)),
                }
            }
            Err(_) => return Err(Some(pending)),
        };
        drop(arguments);
        // Converting this or earlier jobs in the batch can outlast a short
        // deadline. Recheck so expired work never starts.
        if Instant::now() >= deadline || pending.sender.is_closed() {
            self.outbox
                .push(pending.settled(error(StatusCode::GATEWAY_TIMEOUT, "Request timed out")));
            return Err(None);
        }
        let hooks = &self.hooks;
        let context = match new_context(py) {
            Ok(context) => context,
            Err(_) => return Err(Some(pending)),
        };
        let invocation = Bound::new(
            py,
            Invocation {
                id,
                context: Mutex::new(context.clone().unbind()),
                loop_: hooks.loop_.clone_ref(py),
                control: Mutex::new(Control {
                    task: None,
                    cancel_requested: false,
                    cancel_delivered: false,
                    sync_running: false,
                    done: false,
                }),
                pending: Mutex::new(Some(pending)),
                encoded: Mutex::new(None),
                outbox: self.outbox.clone(),
            },
        )
        .map_err(|_| None)?;
        let coroutine = match native {
            Some(route) => self.native_call(invoker, route, &values, &context, &invocation),
            None => hooks.invoke.bind(py).call1((route_id, values, &invocation)),
        };
        match coroutine {
            Ok(coroutine) => Ok(Start {
                coroutine,
                invocation,
            }),
            Err(_) => Err(invocation.get().pending.lock().unwrap().take()),
        }
    }

    fn native_call<'py>(
        &self,
        invoker: &Bound<'py, Self>,
        route: &Arc<NativeRoute>,
        values: &Bound<'py, PyDict>,
        context: &Bound<'py, PyAny>,
        invocation: &Bound<'py, Invocation>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = invoker.py();
        let hooks = &self.hooks;
        // The bare request, copied from the route's template; the context is
        // new, so setting current_request in it needs no reset afterwards.
        let request_type = hooks.request_type.bind(py);
        let request = request_type.call_method1(intern!(py, "__new__"), (request_type,))?;
        let attributes = route.template.bind(py).copy()?;
        request.setattr(intern!(py, "__dict__"), attributes)?;
        set_in_context(context, hooks.current_request.bind(py), &request)?;
        let arguments = (!values.is_empty()).then(|| values.clone().unbind());
        Ok(Bound::new(
            py,
            Call {
                step: Mutex::new(Step {
                    stage: Stage::Handler,
                    start: Some((route.call.clone_ref(py), arguments)),
                    coroutine: None,
                    running: false,
                }),
                route: route.clone(),
                invoker: invoker.clone().unbind(),
                request: request.unbind(),
                invocation: invocation.clone().unbind(),
            },
        )?
        .into_any())
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
            invocation,
        } = start;
        let context = invocation.get().context(py).into_bound(py);
        let context = &context;
        let task = match eager {
            Some(options) => {
                options.set_item(intern!(py, "context"), context)?;
                let task = this
                    .hooks
                    .task_type
                    .bind(py)
                    .call((coroutine,), Some(options));
                // The options are reused; holding the context would keep the
                // last request's context variables alive until the next one.
                options.del_item(intern!(py, "context"))?;
                task?
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
        if task.call_method0(intern!(py, "done"))?.is_truthy()? {
            invocation.get().finish(&task);
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
        let hooks = Hooks {
            loop_: loop_.clone().unbind(),
            worker: worker.clone().unbind(),
            invoke: worker.getattr("_invoke")?.unbind(),
            plain_response: worker.getattr("_plain_response")?.unbind(),
            lean_response: worker.getattr("_lean_response")?.unbind(),
            lean_failed: worker.getattr("_lean_failed")?.unbind(),
            response_type: responses.getattr("Response")?.unbind(),
            streaming_type: responses.getattr("StreamingResponse")?.unbind(),
            request_type: request.getattr("Request")?.unbind(),
            current_request: request.getattr("current_request")?.unbind(),
            task_type: py.import("asyncio")?.getattr("Task")?.unbind(),
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
        Ok(Self {
            shared: bridge.get().shared.clone(),
            eager_options: eager_options.unbind(),
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
        // Publish everything settled in this batch even if starting failed.
        this.outbox.flush();
        // Jobs left by a failed start are dropped here, as before; the empty
        // buffers keep their capacity for the next batch.
        jobs.clear();
        cancels.clear();
        *this.spare.lock().unwrap() = (jobs, cancels);
        started
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
            &hooks.request_type,
            &hooks.current_request,
            &hooks.task_type,
            &hooks.coroutine_type,
            &hooks.generator_type,
        ] {
            visit.call(item)?;
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
        invocation.get().finish(task);
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
            let eager = if this
                .hooks
                .loop_
                .bind(py)
                .call_method0(intern!(py, "get_task_factory"))?
                .is_none()
            {
                Some(this.eager_options.bind(py))
            } else {
                None
            };
            for job in jobs.drain(..) {
                let cancel = cancelled.binary_search(&job.id);
                if let Ok(index) = cancel {
                    cancelled.remove(index);
                }
                if cancel.is_ok() || job.pending.sender.is_closed() {
                    // Never started and the waiter has gone: release capacity now.
                    continue;
                }
                if Instant::now() >= job.deadline {
                    this.outbox.push(
                        job.pending
                            .settled(error(StatusCode::GATEWAY_TIMEOUT, "Request timed out")),
                    );
                    continue;
                }
                if !this
                    .hooks
                    .worker
                    .bind(py)
                    .getattr(intern!(py, "_accepting"))?
                    .is_truthy()?
                {
                    this.outbox.push(job.pending.settled(error(
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "Internal Server Error",
                    )));
                    continue;
                }
                let route_id = job.route_id;
                match this.start_job(slf, job) {
                    Ok(start) => {
                        let invocation = start.invocation.clone();
                        if let Err(failure) = Self::run(slf, start, eager) {
                            failure.print(py);
                            if let Some(pending) = invocation.get().pending.lock().unwrap().take() {
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
