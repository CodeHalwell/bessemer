#![forbid(unsafe_code)]

use axum::{
    Json, Router,
    body::{Body, to_bytes},
    extract::{Path, Request, rejection::PathRejection},
    http::{Method, StatusCode, header},
    response::{IntoResponse, Response},
    routing::{MethodFilter, on},
};
use pyo3::{
    exceptions::{PyTypeError, PyValueError},
    prelude::*,
    types::{PyBool, PyCFunction, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple},
};
use serde_json::{Map, Value, json};
use std::{
    collections::{HashMap, HashSet},
    error::Error as _,
    future::IntoFuture,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::sync::{OwnedSemaphorePermit, Semaphore, oneshot, watch};

#[derive(Clone)]
struct Parameter {
    name: String,
    source: String,
    kind: String,
    required: bool,
    default: Value,
}

#[derive(Clone)]
struct Route {
    id: usize,
    path: String,
    method: Method,
    health: bool,
    status: u16,
    timeout: Option<Duration>,
    parameters: Vec<Parameter>,
}

struct ServerState {
    dispatch: Py<PyAny>,
    capacity: Arc<Semaphore>,
    max_body_size: usize,
    request_timeout: Duration,
}

fn duration(value: f64, name: &str) -> PyResult<Duration> {
    if !value.is_finite() || value <= 0.0 || value > 86_400.0 {
        return Err(PyValueError::new_err(format!(
            "{name} must be finite and between 0 and 86400 seconds"
        )));
    }
    let duration = Duration::from_secs_f64(value);
    if duration.is_zero() {
        return Err(PyValueError::new_err(format!(
            "{name} must be at least one nanosecond"
        )));
    }
    Ok(duration)
}

fn field<'py>(item: &Bound<'py, PyAny>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    item.get_item(name)
}

fn routes_from_python(routes: &Bound<'_, PyAny>) -> PyResult<Vec<Route>> {
    let mut output = Vec::new();
    let mut seen = HashSet::new();
    let mut route_ids = HashSet::new();
    for item in routes.try_iter()? {
        let item = item?;
        let path: String = field(&item, "path")?.extract()?;
        if !path.starts_with('/') || path.contains(['?', '#', '*']) {
            return Err(PyValueError::new_err(
                "route paths must start with / and cannot contain ?, # or *",
            ));
        }
        let method: String = field(&item, "method")?.extract()?;
        let method = Method::from_bytes(method.as_bytes())
            .map_err(|_| PyValueError::new_err("invalid HTTP method"))?;
        MethodFilter::try_from(method.clone())
            .map_err(|_| PyValueError::new_err("unsupported HTTP method"))?;
        let canonical = path
            .split('/')
            .map(|part| {
                if part.starts_with('{') && part.ends_with('}') {
                    "{}"
                } else {
                    part
                }
            })
            .collect::<Vec<_>>()
            .join("/");
        if !seen.insert((method.clone(), canonical)) {
            return Err(PyValueError::new_err("duplicate or ambiguous route"));
        }
        let status: u16 = field(&item, "status_code")?.extract()?;
        if !(200..=599).contains(&status) {
            return Err(PyValueError::new_err(
                "response status must be between 200 and 599",
            ));
        }
        let id: usize = field(&item, "id")?.extract()?;
        if !route_ids.insert(id) {
            return Err(PyValueError::new_err("route ids must be unique"));
        }
        let kind: String = field(&item, "kind")?.extract()?;
        if kind != "health" && kind != "python" {
            return Err(PyValueError::new_err("unsupported route kind"));
        }
        let timeout = match item.get_item("timeout") {
            Ok(value) if !value.is_none() => Some(duration(value.extract()?, "route timeout")?),
            _ => None,
        };
        let mut parameters = Vec::new();
        for parameter in field(&item, "parameters")?.try_iter()? {
            let parameter = parameter?;
            let source: String = field(&parameter, "source")?.extract()?;
            let kind: String = field(&parameter, "type")?.extract()?;
            if !["path", "query", "body"].contains(&source.as_str())
                || !["str", "int", "float", "bool", "json"].contains(&kind.as_str())
            {
                return Err(PyValueError::new_err(
                    "unsupported parameter source or type",
                ));
            }
            parameters.push(Parameter {
                name: field(&parameter, "name")?.extract()?,
                source,
                kind,
                required: field(&parameter, "required")?.extract()?,
                default: parameter
                    .get_item("default")
                    .map_or(Ok(Value::Null), |v| python_to_json(&v, 0))?,
            });
        }
        output.push(Route {
            id,
            path,
            method,
            health: kind == "health",
            status,
            timeout,
            parameters,
        });
    }
    Ok(output)
}

fn python_to_json(value: &Bound<'_, PyAny>, depth: usize) -> PyResult<Value> {
    if depth > 64 {
        return Err(PyValueError::new_err(
            "JSON response nesting exceeds 64 levels",
        ));
    }
    if value.is_none() {
        return Ok(Value::Null);
    }
    if value.is_instance_of::<PyBool>() {
        return Ok(Value::Bool(value.extract()?));
    }
    if value.is_instance_of::<PyInt>() {
        if let Ok(number) = value.extract::<i64>() {
            return Ok(number.into());
        }
        return Ok(value.extract::<u64>()?.into());
    }
    if value.is_instance_of::<PyFloat>() {
        let number: f64 = value.extract()?;
        return serde_json::Number::from_f64(number)
            .map(Value::Number)
            .ok_or_else(|| PyValueError::new_err("non-finite floats are not valid JSON"));
    }
    if value.is_instance_of::<PyString>() {
        return Ok(Value::String(value.extract()?));
    }
    if let Ok(dict) = value.cast::<PyDict>() {
        let mut output = Map::new();
        for (key, value) in dict.iter() {
            output.insert(key.extract::<String>()?, python_to_json(&value, depth + 1)?);
        }
        return Ok(Value::Object(output));
    }
    if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
        let mut output = Vec::new();
        for item in value.try_iter()? {
            output.push(python_to_json(&item?, depth + 1)?);
        }
        return Ok(Value::Array(output));
    }
    Err(PyTypeError::new_err(
        "handler must return JSON-compatible values",
    ))
}

fn json_to_python(py: Python<'_>, value: &Value) -> PyResult<Py<PyAny>> {
    match value {
        Value::Null => Ok(py.None()),
        Value::Bool(v) => Ok(v.into_pyobject(py)?.to_owned().into_any().unbind()),
        Value::String(v) => Ok(v.into_pyobject(py)?.into_any().unbind()),
        Value::Number(v) => {
            if let Some(v) = v.as_i64() {
                return Ok(v.into_pyobject(py)?.into_any().unbind());
            }
            if let Some(v) = v.as_u64() {
                return Ok(v.into_pyobject(py)?.into_any().unbind());
            }
            Ok(v.as_f64().unwrap().into_pyobject(py)?.into_any().unbind())
        }
        Value::Array(values) => {
            let list = PyList::empty(py);
            for value in values {
                list.append(json_to_python(py, value)?)?;
            }
            Ok(list.into_any().unbind())
        }
        Value::Object(values) => {
            let dict = PyDict::new(py);
            for (key, value) in values {
                dict.set_item(key, json_to_python(py, value)?)?;
            }
            Ok(dict.into_any().unbind())
        }
    }
}

fn error(status: StatusCode, detail: &str) -> Response {
    (status, Json(json!({"detail": detail}))).into_response()
}

fn invalid(parameter: &Parameter, kind: &str, message: &str, input: Value) -> Response {
    (StatusCode::UNPROCESSABLE_ENTITY, Json(json!({"detail": [{
        "type": kind, "loc": [&parameter.source, &parameter.name], "msg": message, "input": input
    }]}))).into_response()
}

fn scalar(parameter: &Parameter, value: &str) -> Result<Value, Box<Response>> {
    let parsed = match parameter.kind.as_str() {
        "str" => Some(Value::String(value.to_owned())),
        "int" => value.parse::<i64>().ok().map(Value::from),
        "float" => value
            .parse::<f64>()
            .ok()
            .and_then(serde_json::Number::from_f64)
            .map(Value::Number),
        "bool" => match value.to_ascii_lowercase().as_str() {
            "true" | "1" | "yes" | "on" => Some(Value::Bool(true)),
            "false" | "0" | "no" | "off" => Some(Value::Bool(false)),
            _ => None,
        },
        _ => None,
    };
    parsed.ok_or_else(|| {
        Box::new(invalid(
            parameter,
            &format!("{}_parsing", parameter.kind),
            &format!("Input should be a valid {}", parameter.kind),
            Value::String(value.to_owned()),
        ))
    })
}

struct CancelOnDrop {
    future: Py<PyAny>,
    armed: bool,
}

impl Drop for CancelOnDrop {
    fn drop(&mut self) {
        if self.armed {
            Python::attach(|py| {
                let _ = self.future.bind(py).call_method0("cancel");
            });
        }
    }
}

type Completion = (oneshot::Sender<Response>, OwnedSemaphorePermit);

fn dispatch(
    state: &ServerState,
    route_id: usize,
    arguments: &Map<String, Value>,
    permit: OwnedSemaphorePermit,
) -> PyResult<(oneshot::Receiver<Response>, CancelOnDrop)> {
    Python::attach(|py| {
        let arguments = json_to_python(py, &Value::Object(arguments.clone()))?;
        let future = state.dispatch.bind(py).call1((route_id, arguments))?;
        let guard = CancelOnDrop {
            future: future.clone().unbind(),
            armed: true,
        };
        let (sender, receiver) = oneshot::channel();
        // The permit lives until the Python task actually completes, including cancellation
        // cleanup. The Python bridge deliberately does not mark a cancellation request done.
        let completion: Mutex<Option<Completion>> = Mutex::new(Some((sender, permit)));
        let callback = PyCFunction::new_closure(
            py,
            None,
            None,
            move |args: &Bound<'_, PyTuple>, _kwargs: Option<&Bound<'_, PyDict>>| -> PyResult<()> {
                let Some((sender, _permit)) = completion.lock().unwrap().take() else {
                    return Ok(());
                };
                if sender.is_closed() {
                    return Ok(());
                }
                let result = (|| -> PyResult<Response> {
                    let result = args.get_item(0)?.call_method0("result")?;
                    let status: u16 = result.get_item(0)?.extract()?;
                    if !(200..=599).contains(&status) {
                        return Err(PyValueError::new_err("invalid handler response status"));
                    }
                    let payload = python_to_json(&result.get_item(1)?, 0)?;
                    let mut response =
                        (StatusCode::from_u16(status).unwrap(), Json(payload)).into_response();
                    if status == 204 || status == 304 {
                        *response.body_mut() = Body::empty();
                        response.headers_mut().remove(header::CONTENT_TYPE);
                    }
                    Ok(response)
                })();
                let response = result.unwrap_or_else(|_| {
                    eprintln!("Bessemer could not encode the response for route {route_id}");
                    error(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error")
                });
                let _ = sender.send(response);
                Ok(())
            },
        )?;
        future.call_method1("add_done_callback", (callback,))?;
        Ok((receiver, guard))
    })
}

async fn handle(
    state: Arc<ServerState>,
    route: Route,
    path: Result<Path<HashMap<String, String>>, PathRejection>,
    request: Request,
) -> Response {
    if route.health {
        return (
            StatusCode::from_u16(route.status).unwrap(),
            Json(json!({"ok": true})),
        )
            .into_response();
    }
    let permit = match state.capacity.clone().try_acquire_owned() {
        Ok(permit) => permit,
        Err(_) => {
            let mut response = error(StatusCode::SERVICE_UNAVAILABLE, "Server is at capacity");
            response
                .headers_mut()
                .insert(header::RETRY_AFTER, "1".parse().unwrap());
            return response;
        }
    };
    let head = request.method() == Method::HEAD;
    let timeout = route.timeout.unwrap_or(state.request_timeout);
    let result = tokio::time::timeout(timeout, async {
        let path = match path {
            Ok(Path(value)) => value,
            Err(_) => return error(StatusCode::BAD_REQUEST, "Invalid path parameters"),
        };
        let query: Vec<(String, String)> =
            match serde_urlencoded::from_str(request.uri().query().unwrap_or("")) {
                Ok(value) => value,
                Err(_) => return error(StatusCode::BAD_REQUEST, "Invalid query string"),
            };
        let query: HashMap<_, _> = query.into_iter().collect();
        let content_type = request
            .headers()
            .get(header::CONTENT_TYPE)
            .map(|value| value.to_str().unwrap_or("").to_owned());
        let body = match to_bytes(request.into_body(), state.max_body_size).await {
            Ok(value) => value,
            Err(error_value)
                if error_value
                    .source()
                    .is_some_and(|cause| cause.is::<http_body_util::LengthLimitError>()) =>
            {
                return error(
                    StatusCode::PAYLOAD_TOO_LARGE,
                    "Request body exceeds the configured limit",
                );
            }
            Err(_) => return error(StatusCode::BAD_REQUEST, "Invalid request body"),
        };
        let needs_body = route
            .parameters
            .iter()
            .any(|parameter| parameter.source == "body");
        let body_value = if needs_body && !body.is_empty() {
            if let Some(content_type) = content_type {
                let media_type = content_type
                    .split(';')
                    .next()
                    .unwrap_or("")
                    .trim()
                    .to_ascii_lowercase();
                if media_type != "application/json"
                    && !(media_type.starts_with("application/") && media_type.ends_with("+json"))
                {
                    return error(
                        StatusCode::UNSUPPORTED_MEDIA_TYPE,
                        "Expected an application/json body",
                    );
                }
            }
            match serde_json::from_slice(&body) {
                Ok(value) => Some(value),
                Err(_) => return (StatusCode::UNPROCESSABLE_ENTITY, Json(json!({"detail": [{
                    "type": "json_invalid", "loc": ["body"], "msg": "Invalid JSON", "input": null
                }]}))).into_response(),
            }
        } else {
            None
        };
        let mut arguments = Map::new();
        for parameter in &route.parameters {
            let value = if parameter.source == "body" {
                body_value.clone()
            } else {
                let input = if parameter.source == "path" {
                    path.get(&parameter.name)
                } else {
                    query.get(&parameter.name)
                };
                match input {
                    Some(value) => match scalar(parameter, value) {
                        Ok(v) => Some(v),
                        Err(response) => return *response,
                    },
                    None => None,
                }
            };
            match value {
                Some(value) => {
                    arguments.insert(parameter.name.clone(), value);
                }
                None if parameter.required => {
                    return invalid(parameter, "missing", "Field required", Value::Null);
                }
                None => {
                    arguments.insert(parameter.name.clone(), parameter.default.clone());
                }
            }
        }
        let (receiver, mut guard) = match dispatch(&state, route.id, &arguments, permit) {
            Ok(value) => value,
            Err(_) => return error(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error"),
        };
        let response = receiver
            .await
            .unwrap_or_else(|_| error(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error"));
        guard.armed = false;
        response
    })
    .await;
    let mut response =
        result.unwrap_or_else(|_| error(StatusCode::GATEWAY_TIMEOUT, "Request timed out"));
    if head {
        *response.body_mut() = Body::empty();
    }
    response
}

async fn shutdown_signal() {
    #[cfg(unix)]
    {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                .expect("install SIGTERM handler");
        tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = terminate.recv() => {} }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}

#[pyfunction]
#[pyo3(signature = (routes, dispatch, host, port, *, max_body_size=1_048_576, max_in_flight=128, request_timeout=30.0, graceful_timeout=30.0))]
#[allow(clippy::too_many_arguments)]
fn serve(
    py: Python<'_>,
    routes: &Bound<'_, PyAny>,
    dispatch: Py<PyAny>,
    host: String,
    port: u16,
    max_body_size: usize,
    max_in_flight: usize,
    request_timeout: f64,
    graceful_timeout: f64,
) -> PyResult<()> {
    if max_body_size == 0 || max_in_flight == 0 || max_in_flight > 1_000_000 {
        return Err(PyValueError::new_err(
            "body and concurrency limits must be positive; concurrency cannot exceed 1000000",
        ));
    }
    let routes = routes_from_python(routes)?;
    let request_timeout = duration(request_timeout, "request_timeout")?;
    let graceful_timeout = duration(graceful_timeout, "graceful_timeout")?;
    if !dispatch.bind(py).is_callable() {
        return Err(PyTypeError::new_err("dispatch must be callable"));
    }
    let state = Arc::new(ServerState {
        dispatch,
        capacity: Arc::new(Semaphore::new(max_in_flight)),
        max_body_size,
        request_timeout,
    });
    let mut app = Router::new();
    for route in routes {
        let path = route.path.clone();
        let filter = MethodFilter::try_from(route.method.clone()).unwrap();
        let state = state.clone();
        // Axum rejects malformed/conflicting patterns with a panic; translate this startup
        // validation into a Python error before starting any listener.
        app = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            app.route(
                &path,
                on(
                    filter,
                    move |path: Result<Path<HashMap<String, String>>, PathRejection>,
                          request: Request| {
                        handle(state.clone(), route.clone(), path, request)
                    },
                ),
            )
        }))
        .map_err(|_| PyValueError::new_err(format!("invalid or conflicting route: {path}")))?;
    }
    app = app
        .fallback(|| async { error(StatusCode::NOT_FOUND, "Not Found") })
        .method_not_allowed_fallback(|| async {
            error(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed")
        });
    py.detach(move || -> std::io::Result<()> {
        let runtime = tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build()?;
        runtime.block_on(async move {
            let listener = tokio::net::TcpListener::bind((host.as_str(), port)).await?;
            eprintln!("Bessemer listening on {}", listener.local_addr()?);
            let (sender, mut receiver) = watch::channel(false);
            let server = axum::serve(listener, app).with_graceful_shutdown(async move {
                shutdown_signal().await;
                sender.send_replace(true);
            }).into_future();
            tokio::pin!(server);
            tokio::select! {
                result = &mut server => result,
                _ = receiver.changed() => {
                    match tokio::time::timeout(graceful_timeout, &mut server).await {
                        Ok(result) => result,
                        Err(_) => { eprintln!("Bessemer graceful shutdown deadline exceeded"); Ok(()) }
                    }
                }
            }
        })
    }).map_err(PyErr::from)
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(serve, module)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_conversion_rejects_cycles_and_invalid_values() {
        Python::attach(|py| {
            for expression in [c"float('nan')", c"{1: 'wrong key'}", c"2**100"] {
                assert!(python_to_json(&py.eval(expression, None, None).unwrap(), 0).is_err());
            }
            let list = PyList::empty(py);
            list.append(&list).unwrap();
            assert!(python_to_json(list.as_any(), 0).is_err());
            list.call_method0("clear").unwrap();
            let value = py
                .eval(
                    c"{'bool': True, 'items': [None, -2, 1.5, 'text']}",
                    None,
                    None,
                )
                .unwrap();
            assert_eq!(
                python_to_json(&value, 0).unwrap(),
                json!({"bool":true,"items":[null,-2,1.5,"text"]})
            );
        });
    }

    #[test]
    fn limits_reject_invalid_durations() {
        for value in [0.0, -1.0, f64::NAN, f64::INFINITY, 86_401.0] {
            assert!(duration(value, "test").is_err());
        }
    }
}
