#![forbid(unsafe_code)]

mod budget;
mod controls;
mod conversion;
mod cookies;
mod jwt;
mod streaming;
mod telemetry;
mod transport;

use conversion::{
    json_to_python, parse_integer, parse_json, python_to_json, python_to_json_limited,
    validate_input_numbers,
};

use axum::{
    Json, Router,
    body::Body,
    extract::{ConnectInfo, MatchedPath, Path, Request, State, rejection::PathRejection},
    http::{HeaderMap, HeaderName, HeaderValue, Method, StatusCode, header},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{MethodFilter, on},
};
use pyo3::{
    exceptions::{PyTypeError, PyValueError},
    prelude::*,
    types::{PyBytes, PyCFunction, PyDict, PyList, PyTuple},
};
use serde_json::{Map, Value, json};
use std::{
    borrow::Cow,
    collections::{HashMap, HashSet},
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    sync::{OwnedSemaphorePermit, Semaphore, oneshot},
    time::Instant,
};

#[derive(Clone)]
struct Parameter {
    name: String,
    alias: String,
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
    static_body: Option<(Arc<[u8]>, HeaderValue)>,
    needs_request: bool,
    needs_principal: bool,
    status: u16,
    timeout: Option<Duration>,
    parameters: Vec<Parameter>,
}

struct ServerState {
    dispatch: Py<PyAny>,
    capacity: Arc<Semaphore>,
    max_body_size: usize,
    max_response_size: usize,
    request_bytes: budget::ByteBudget,
    response_bytes: budget::ByteBudget,
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
        if kind != "health" && kind != "python" && kind != "static" {
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
            if !["path", "query", "body", "header", "cookie"].contains(&source.as_str())
                || !["str", "int", "float", "bool", "json"].contains(&kind.as_str())
            {
                return Err(PyValueError::new_err(
                    "unsupported parameter source or type",
                ));
            }
            parameters.push(Parameter {
                name: field(&parameter, "name")?.extract()?,
                alias: parameter
                    .get_item("alias")
                    .or_else(|_| parameter.get_item("name"))?
                    .extract()?,
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
            static_body: if kind == "static" {
                let body: Vec<u8> = field(&item, "content")?
                    .cast::<PyBytes>()?
                    .as_bytes()
                    .to_vec();
                let content_type: String = field(&item, "content_type")?.extract()?;
                Some((
                    body.into(),
                    HeaderValue::from_str(&content_type).map_err(|_| {
                        PyValueError::new_err("invalid static response content type")
                    })?,
                ))
            } else {
                None
            },
            needs_request: item
                .get_item("needs_request")
                .map_or(Ok(false), |v| v.extract())?,
            needs_principal: item
                .get_item("needs_principal")
                .map_or(Ok(false), |v| v.extract())?,
            status,
            timeout,
            parameters,
        });
    }
    Ok(output)
}

fn error(status: StatusCode, detail: &str) -> Response {
    (status, Json(json!({"detail": detail}))).into_response()
}

fn invalid(parameter: &Parameter, kind: &str, message: &str, _input: Value) -> Response {
    (
        StatusCode::UNPROCESSABLE_ENTITY,
        Json(json!({"detail": [{
            "type": kind, "loc": [&parameter.source, &parameter.alias], "msg": message
        }]})),
    )
        .into_response()
}

fn scalar(parameter: &Parameter, value: &str) -> Result<Value, Box<Response>> {
    let parsed = match parameter.kind.as_str() {
        "str" => Some(Value::String(value.to_owned())),
        "int" => parse_integer(value),
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

pub(crate) struct Admission {
    _request: OwnedSemaphorePermit,
    _input: budget::BytePermit,
}

type Completion = (oneshot::Sender<Response>, Admission);

#[derive(Debug)]
enum DispatchError {
    Invalid,
    Expired,
}

impl From<PyErr> for DispatchError {
    fn from(_: PyErr) -> Self {
        Self::Invalid
    }
}

fn finish_before_deadline(
    result: Result<Response, tokio::time::error::Elapsed>,
    deadline: Instant,
) -> Response {
    // Tokio polls the inner future before its timer. After reactor starvation,
    // an already-ready response must not become a successful late response.
    match result {
        Ok(response) if Instant::now() < deadline => response,
        _ => error(StatusCode::GATEWAY_TIMEOUT, "Request timed out"),
    }
}

fn capacity_response(detail: &str, close: bool) -> Response {
    let mut response = error(StatusCode::SERVICE_UNAVAILABLE, detail);
    response
        .headers_mut()
        .insert(header::RETRY_AFTER, HeaderValue::from_static("1"));
    if close {
        response
            .headers_mut()
            .insert(header::CONNECTION, HeaderValue::from_static("close"));
    }
    response
}

#[derive(Debug)]
enum ResponseError {
    Invalid,
    Full,
}

impl From<PyErr> for ResponseError {
    fn from(_: PyErr) -> Self {
        Self::Invalid
    }
}

fn response_headers(headers: &Bound<'_, PyAny>) -> PyResult<HeaderMap> {
    let mut output = HeaderMap::new();
    let mut bytes = 0usize;
    let entries: Vec<(String, String)> = if let Ok(dict) = headers.cast::<PyDict>() {
        if dict.len() > 64 {
            return Err(PyValueError::new_err(
                "response cannot contain more than 64 headers",
            ));
        }
        dict.iter()
            .map(|(key, value)| Ok((key.extract()?, value.extract()?)))
            .collect::<PyResult<_>>()?
    } else {
        if !headers.is_instance_of::<PyList>() && !headers.is_instance_of::<PyTuple>() {
            return Err(PyTypeError::new_err(
                "response headers must be a dict or list of pairs",
            ));
        }
        if headers.len()? > 64 {
            return Err(PyValueError::new_err(
                "response cannot contain more than 64 headers",
            ));
        }
        headers.extract()?
    };
    for (name, value) in entries {
        bytes = bytes.saturating_add(name.len()).saturating_add(value.len());
        if bytes > 16_384 {
            return Err(PyValueError::new_err("response headers exceed 16 KiB"));
        }
        let name = HeaderName::from_bytes(name.as_bytes())
            .map_err(|_| PyValueError::new_err("invalid response header name"))?;
        if matches!(
            name.as_str(),
            "connection"
                | "content-length"
                | "transfer-encoding"
                | "keep-alive"
                | "proxy-authenticate"
                | "proxy-authorization"
                | "te"
                | "trailer"
                | "upgrade"
        ) {
            return Err(PyValueError::new_err(
                "hop-by-hop and framing response headers are managed by the server",
            ));
        }
        let value = HeaderValue::from_str(&value)
            .map_err(|_| PyValueError::new_err("invalid response header value"))?;
        output.append(name, value);
    }
    Ok(output)
}

fn decode_response(
    result: &Bound<'_, PyAny>,
    max_bytes: usize,
    budget: &budget::ByteBudget,
    head: bool,
    admission: &mut Option<Admission>,
) -> Result<Response, ResponseError> {
    let result = result
        .cast::<PyTuple>()
        .map_err(|_| PyTypeError::new_err("invalid internal response envelope"))?;
    if result.len() != 2 && result.len() != 4 {
        return Err(PyValueError::new_err("invalid internal response envelope").into());
    }
    let status: u16 = result.get_item(0)?.extract()?;
    if !(200..=599).contains(&status) {
        return Err(PyValueError::new_err("invalid handler response status").into());
    }
    let mut headers = if result.len() == 4 {
        response_headers(&result.get_item(2)?)?
    } else {
        HeaderMap::new()
    };
    let kind: String = if result.len() == 4 {
        result.get_item(3)?.extract()?
    } else {
        "json".to_owned()
    };
    if kind == "stream" {
        // The Tokio request task starts (or, for HEAD, closes) the stream; the
        // admission travels with it until the Python iterator is finished.
        let handle = result.get_item(1)?.unbind();
        let admission = admission
            .take()
            .ok_or_else(|| PyValueError::new_err("stream admission already released"))?;
        let mut response = Response::new(Body::empty());
        *response.status_mut() = StatusCode::from_u16(status).unwrap();
        *response.headers_mut() = headers;
        response
            .extensions_mut()
            .insert(streaming::StreamStart::new(handle, admission));
        return Ok(response);
    }
    if kind != "json" && kind != "bytes" {
        return Err(PyValueError::new_err("unsupported internal response kind").into());
    }
    let body = if matches!(status, 204 | 205 | 304) {
        headers.remove(header::CONTENT_TYPE);
        Vec::new()
    } else if kind == "bytes" {
        let payload = result.get_item(1)?;
        let bytes = payload
            .cast::<PyBytes>()
            .map_err(|_| PyTypeError::new_err("raw responses must contain bytes"))?
            .as_bytes();
        if bytes.len() > max_bytes {
            return Err(PyValueError::new_err("response exceeds the configured size limit").into());
        }
        bytes.to_vec()
    } else {
        let payload = python_to_json_limited(&result.get_item(1)?, 0, max_bytes)?;
        let body = serde_json::to_vec(&payload)
            .map_err(|_| PyValueError::new_err("invalid JSON response"))?;
        if body.len() > max_bytes {
            return Err(PyValueError::new_err("response exceeds the configured size limit").into());
        }
        headers
            .entry(header::CONTENT_TYPE)
            .or_insert(HeaderValue::from_static("application/json"));
        body
    };
    let body = if head {
        Body::empty()
    } else {
        Body::from(budget.own(body).map_err(|_| ResponseError::Full)?)
    };
    let mut response = Response::new(body);
    *response.status_mut() = StatusCode::from_u16(status).unwrap();
    *response.headers_mut() = headers;
    Ok(response)
}

struct RequestMetadata {
    method: String,
    path: String,
    query_string: String,
    headers: Vec<(String, String)>,
    client: Option<String>,
    body: Vec<u8>,
    request_id: Option<String>,
    path_params: HashMap<String, String>,
    cookies: HashMap<String, String>,
}

#[derive(Clone)]
struct RequestInfo {
    request_id: Option<String>,
    client: Option<String>,
    claims: Option<Arc<Value>>,
}

fn header_text(value: &HeaderValue) -> Cow<'_, str> {
    match value.to_str() {
        Ok(value) => Cow::Borrowed(value),
        Err(_) => Cow::Owned(
            value
                .as_bytes()
                .iter()
                .map(|value| char::from(*value))
                .collect(),
        ),
    }
}

async fn native_controls(
    State(controls): State<Arc<controls::Controls>>,
    mut request: Request,
    next: Next,
) -> Response {
    let peer = request
        .extensions()
        .get::<ConnectInfo<std::net::SocketAddr>>()
        .map(|value| value.0);
    let template = request
        .extensions()
        .get::<MatchedPath>()
        .map(|path| path.as_str().to_owned())
        .unwrap_or_else(|| "<unmatched>".to_owned());
    let controls::Begin { context, response } =
        controls.begin(request.method(), request.uri(), request.headers(), peer);
    if let Some(request_id) = &context.request_id {
        request
            .headers_mut()
            .insert("x-request-id", HeaderValue::from_str(request_id).unwrap());
    }
    request.extensions_mut().insert(RequestInfo {
        request_id: context.request_id.clone(),
        client: context.client_ip.map(|ip| ip.to_string()),
        claims: context.claims.clone(),
    });
    let mut response = match response {
        Some(response) => response,
        None => next.run(request).await,
    };
    response = controls.resolve_response(response).await;
    controls.finish(&context, &mut response, &template);
    response
}

fn dispatch(
    state: &ServerState,
    route_id: usize,
    argument_values: &Map<String, Value>,
    admission: Admission,
    metadata: Option<RequestMetadata>,
    head: bool,
    deadline: Instant,
) -> Result<(oneshot::Receiver<Response>, CancelOnDrop), DispatchError> {
    Python::attach(|py| {
        let arguments = PyDict::new(py);
        // Construct arguments directly without an intermediate cloned JSON tree.
        for (key, value) in argument_values {
            arguments.set_item(key, json_to_python(py, value)?)?;
        }
        if let Some(metadata) = metadata {
            let data = PyDict::new(py);
            data.set_item("method", metadata.method)?;
            data.set_item("path", metadata.path)?;
            data.set_item("query_string", metadata.query_string)?;
            data.set_item("headers", metadata.headers)?;
            data.set_item("client", metadata.client)?;
            data.set_item("scheme", "http")?;
            data.set_item("body", PyBytes::new(py, &metadata.body))?;
            data.set_item("request_id", metadata.request_id)?;
            data.set_item("path_params", metadata.path_params)?;
            data.set_item("cookies", metadata.cookies)?;
            arguments.set_item("__bessemer_request__", data)?;
        }
        if Instant::now() >= deadline {
            return Err(DispatchError::Expired);
        }
        let future = state.dispatch.bind(py).call1((route_id, arguments))?;
        let guard = CancelOnDrop {
            future: future.clone().unbind(),
            armed: true,
        };
        let (sender, receiver) = oneshot::channel();
        // The permit lives until the Python task actually completes, including cancellation
        // cleanup. The Python bridge deliberately does not mark a cancellation request done.
        let completion: Mutex<Option<Completion>> = Mutex::new(Some((sender, admission)));
        let max_response_size = state.max_response_size;
        let response_bytes = state.response_bytes.clone();
        let callback = PyCFunction::new_closure(
            py,
            None,
            None,
            move |args: &Bound<'_, PyTuple>, _kwargs: Option<&Bound<'_, PyDict>>| -> PyResult<()> {
                let Some((sender, admission)) = completion.lock().unwrap().take() else {
                    return Ok(());
                };
                if sender.is_closed() {
                    return Ok(());
                }
                let mut admission = Some(admission);
                let result = (|| -> Result<Response, ResponseError> {
                    decode_response(
                        &args.get_item(0)?.call_method0("result")?,
                        max_response_size,
                        &response_bytes,
                        head,
                        &mut admission,
                    )
                })();
                let response = match result {
                    Ok(response) => response,
                    Err(ResponseError::Full) => {
                        capacity_response("Response byte capacity is full", false)
                    }
                    Err(ResponseError::Invalid) => {
                        eprintln!("Bessemer could not encode the response for route {route_id}");
                        error(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error")
                    }
                };
                // Publish only after admission is available again. The Python future
                // and all cleanup are complete; waking Tokio first can otherwise
                // make a subsequent sequential request observe a spurious 503.
                drop(admission);
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
    let head = request.method() == Method::HEAD;
    if route.health {
        return (
            StatusCode::from_u16(route.status).unwrap(),
            Json(json!({"ok": true})),
        )
            .into_response();
    }
    if let Some((body, content_type)) = &route.static_body {
        let representation_length = body.len();
        let body = if head || matches!(route.status, 204 | 205 | 304) {
            Body::empty()
        } else {
            match state.response_bytes.own(body.clone()) {
                Ok(body) => Body::from(body),
                Err(_) => return capacity_response("Response byte capacity is full", false),
            }
        };
        let mut response = Response::new(body);
        *response.status_mut() = StatusCode::from_u16(route.status).unwrap();
        response
            .headers_mut()
            .insert(header::CONTENT_TYPE, content_type.clone());
        if head && !matches!(route.status, 204 | 205 | 304) {
            response.headers_mut().insert(
                header::CONTENT_LENGTH,
                HeaderValue::from_str(&representation_length.to_string()).unwrap(),
            );
        }
        return response;
    }
    let permit = match state.capacity.clone().try_acquire_owned() {
        Ok(permit) => permit,
        Err(_) => return capacity_response("Server is at capacity", true),
    };
    let timeout = route.timeout.unwrap_or(state.request_timeout);
    let deadline = Instant::now() + timeout;
    let mut body_complete = false;
    let result = tokio::time::timeout_at(deadline, async {
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
        let headers = request.headers().clone();
        let request_info = request.extensions().get::<RequestInfo>().cloned();
        let request_method = request.method().to_string();
        let request_path = request.uri().path().to_owned();
        let query_string = request.uri().query().unwrap_or("").to_owned();
        let cookies = if route.needs_request || route.parameters.iter().any(|parameter| parameter.source == "cookie") {
            match cookies::parse(&headers) { Ok(value) => value, Err(detail) => return error(StatusCode::BAD_REQUEST, detail) }
        } else { HashMap::new() };
        let content_type = request
            .headers()
            .get(header::CONTENT_TYPE)
            .map(|value| value.to_str().unwrap_or("").to_owned());
        let (body, input_permit) = match budget::buffer_body(request.into_body(), state.max_body_size, &state.request_bytes).await {
            Ok(value) => value,
            Err(budget::ReadError::Full) => return capacity_response("Request byte capacity is full", true),
            Err(read_error) => {
                let mut response = match read_error {
                    budget::ReadError::TooLarge => error(StatusCode::PAYLOAD_TOO_LARGE, "Request body exceeds the configured limit"),
                    _ => error(StatusCode::BAD_REQUEST, "Invalid request body"),
                };
                response.headers_mut().insert(header::CONNECTION, HeaderValue::from_static("close"));
                return response;
            }
        };
        body_complete = true;
        let needs_body = route
            .parameters
            .iter()
            .any(|parameter| parameter.source == "body");
        let mut body_value = if needs_body && !body.is_empty() {
            if let Some(content_type) = content_type {
                let media_type = content_type.parse::<mime::Mime>().ok();
                if !media_type.is_some_and(|value| value.type_() == mime::APPLICATION
                    && (value.subtype() == mime::JSON || value.suffix() == Some(mime::JSON))
                    && value.get_param(mime::CHARSET).is_none_or(|charset| charset.as_str().eq_ignore_ascii_case("utf-8") || charset.as_str().eq_ignore_ascii_case("utf8")))
                {
                    return error(
                        StatusCode::UNSUPPORTED_MEDIA_TYPE,
                        "Expected an application/json body",
                    );
                }
            }
            match parse_json(&body) {
                Ok(value) if validate_input_numbers(&value) => Some(value),
                Ok(_) => return (StatusCode::UNPROCESSABLE_ENTITY, Json(json!({"detail": [{
                    "type": "json_invalid", "loc": ["body"], "msg": "JSON numbers must be finite and integers cannot exceed 4096 digits", "input": null
                }]}))).into_response(),
                Err(_) => return (StatusCode::UNPROCESSABLE_ENTITY, Json(json!({"detail": [{
                    "type": "json_invalid", "loc": ["body"], "msg": "Invalid JSON", "input": null
                }]}))).into_response(),
            }
        } else {
            None
        };
        let mut arguments = Map::new();
        let mut body_bindings = route.parameters.iter().filter(|parameter| parameter.source == "body").count();
        for parameter in &route.parameters {
            let value = if parameter.source == "body" {
                body_bindings -= 1;
                if body_bindings == 0 { body_value.take() } else { body_value.clone() }
            } else {
                let header_value = if parameter.source == "header" { headers.get(&parameter.alias).map(header_text) } else { None };
                let input = if parameter.source == "path" {
                    path.get(&parameter.alias).map(String::as_str)
                } else if parameter.source == "header" {
                    if headers.get_all(&parameter.alias).iter().count() > 1 {
                        return invalid(parameter, "multiple_header_values", "A scalar header parameter must have exactly one value", Value::Null);
                    }
                    header_value.as_deref()
                } else if parameter.source == "cookie" {
                    cookies.get(&parameter.alias).map(String::as_str)
                } else {
                    query.get(&parameter.alias).map(String::as_str)
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
        if route.needs_principal {
            // Verified natively by the JWT control; absent on excluded paths.
            let claims = request_info.as_ref().and_then(|info| info.claims.as_deref()).cloned();
            arguments.insert("__bessemer_principal__".to_owned(), claims.unwrap_or(Value::Null));
        }
        let metadata = if route.needs_request { Some(RequestMetadata {
            method: request_method, path: request_path, query_string,
            headers: headers.iter().map(|(name, value)| (name.to_string(), header_text(value).into_owned())).collect(),
            client: request_info.as_ref().and_then(|info| info.client.clone()),
            request_id: request_info.and_then(|info| info.request_id), body, path_params: path, cookies,
        }) } else { drop(body); None };
        let admission = Admission { _request: permit, _input: input_permit };
        let (receiver, mut guard) = match dispatch(&state, route.id, &arguments, admission, metadata, head, deadline) {
            Ok(value) => value,
            Err(DispatchError::Expired) => return error(StatusCode::GATEWAY_TIMEOUT, "Request timed out"),
            Err(DispatchError::Invalid) => return error(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error"),
        };
        drop(arguments);
        drop(body_value);
        if Instant::now() >= deadline {
            // Submit can overrun too. Its registered callback retains admission
            // and input until actual completion; the armed guard only requests
            // cancellation when this timeout path drops it.
            return error(StatusCode::GATEWAY_TIMEOUT, "Request timed out");
        }
        let mut response = receiver
            .await
            .unwrap_or_else(|_| error(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error"));
        guard.armed = false;
        if let Some(start) = response.extensions_mut().remove::<streaming::StreamStart>()
            && let Some(stream) = start.take()
            && !head
            && !matches!(response.status().as_u16(), 204 | 205 | 304)
        {
            // Each chunk must arrive within the route timeout of the previous one.
            *response.body_mut() = Body::new(streaming::start(stream, state.response_bytes.clone(), timeout));
        }
        response
    })
    .await;
    let mut response = finish_before_deadline(result, deadline);
    if !body_complete && response.status() == StatusCode::GATEWAY_TIMEOUT {
        response
            .headers_mut()
            .insert(header::CONNECTION, HeaderValue::from_static("close"));
    }
    if head {
        *response.body_mut() = Body::empty();
    }
    response
}

#[pyfunction]
#[pyo3(signature = (routes, dispatch, host, port, *, max_body_size=1_048_576, max_in_flight=128, request_timeout=30.0, graceful_timeout=30.0, max_response_size=4_194_304, max_request_bytes_in_flight=33_554_432, max_response_bytes_in_flight=33_554_432, max_connections=1024, header_timeout=10.0, max_headers=64, max_header_size=32768, max_request_target_size=8192, listener_fd=None, ready_fd=None, max_requests=0, controls=None))]
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
    max_response_size: usize,
    max_request_bytes_in_flight: usize,
    max_response_bytes_in_flight: usize,
    max_connections: usize,
    header_timeout: f64,
    max_headers: usize,
    max_header_size: usize,
    max_request_target_size: usize,
    listener_fd: Option<i32>,
    ready_fd: Option<i32>,
    max_requests: u64,
    controls: Option<Bound<'_, PyAny>>,
) -> PyResult<()> {
    for (name, value, maximum) in [
        ("max_body_size", max_body_size, 1_073_741_824usize),
        ("max_response_size", max_response_size, 67_108_864),
        (
            "max_request_bytes_in_flight",
            max_request_bytes_in_flight,
            1_073_741_824,
        ),
        (
            "max_response_bytes_in_flight",
            max_response_bytes_in_flight,
            1_073_741_824,
        ),
        ("max_in_flight", max_in_flight, 1_000_000),
        ("max_connections", max_connections, 100_000),
        ("max_headers", max_headers, 1024),
        (
            "max_request_target_size",
            max_request_target_size,
            1_048_576,
        ),
    ] {
        if value == 0 || value > maximum {
            return Err(PyValueError::new_err(format!(
                "{name} must be between 1 and {maximum}"
            )));
        }
    }
    if max_request_bytes_in_flight < max_body_size {
        return Err(PyValueError::new_err(
            "max_request_bytes_in_flight must be at least max_body_size",
        ));
    }
    if max_response_bytes_in_flight < max_response_size {
        return Err(PyValueError::new_err(
            "max_response_bytes_in_flight must be at least max_response_size",
        ));
    }
    if !(8192..=1_048_576).contains(&max_header_size) {
        return Err(PyValueError::new_err(
            "max_header_size must be between 8192 and 1048576",
        ));
    }
    if listener_fd.is_some_and(|fd| fd < 0) || ready_fd.is_some_and(|fd| fd < 0) {
        return Err(PyValueError::new_err(
            "inherited descriptors must be nonnegative",
        ));
    }
    let routes = routes_from_python(routes)?;
    if routes.len() > 10_000 {
        return Err(PyValueError::new_err(
            "at most 10000 compiled routes are supported",
        ));
    }
    for route in &routes {
        if route
            .static_body
            .as_ref()
            .is_some_and(|(body, _)| body.len() > max_response_size)
        {
            return Err(PyValueError::new_err(
                "static response exceeds max_response_size",
            ));
        }
    }
    let request_timeout = duration(request_timeout, "request_timeout")?;
    let graceful_timeout = duration(graceful_timeout, "graceful_timeout")?;
    let header_timeout = duration(header_timeout, "header_timeout")?;
    if !dispatch.bind(py).is_callable() {
        return Err(PyTypeError::new_err("dispatch must be callable"));
    }
    let config = controls.map_or(Ok(Value::Null), |value| python_to_json(&value, 0))?;
    let controls = Arc::new(controls::Controls::from_json(&config).map_err(PyValueError::new_err)?);
    controls
        .register_routes(
            &routes
                .iter()
                .map(|route| route.path.clone())
                .collect::<Vec<_>>(),
        )
        .map_err(PyValueError::new_err)?;
    let state = Arc::new(ServerState {
        dispatch,
        capacity: Arc::new(Semaphore::new(max_in_flight)),
        max_body_size,
        max_response_size,
        request_bytes: budget::ByteBudget::new(max_request_bytes_in_flight),
        response_bytes: budget::ByteBudget::new(max_response_bytes_in_flight),
        request_timeout,
    });
    let mut app = Router::new();
    for route in routes {
        let path = route.path.clone();
        let filter = MethodFilter::try_from(route.method.clone()).unwrap();
        let state = state.clone();
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
        })
        .layer(middleware::from_fn_with_state(controls, native_controls));
    let limits = transport::Limits {
        max_connections,
        header_timeout,
        max_headers,
        max_header_size,
        max_request_target_size,
        graceful_timeout,
        max_requests,
    };
    py.detach(move || -> std::io::Result<()> {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()?;
        runtime.block_on(async move {
            let listener = if let Some(fd) = listener_fd {
                tokio::net::TcpListener::from_std(bessemer_ffi::inherited_listener(fd)?)?
            } else {
                tokio::net::TcpListener::bind((host.as_str(), port)).await?
            };
            eprintln!("Bessemer listening on {}", listener.local_addr()?);
            transport::run(listener, app, limits, ready_fd).await
        })
    })
    .map_err(PyErr::from)
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(serve, module)?)?;
    module.add_function(wrap_pyfunction!(_set_parent_death_signal, module)?)?;
    module.add_function(wrap_pyfunction!(_parse_json, module)?)?;
    Ok(())
}

#[pyfunction]
fn _set_parent_death_signal(expected_parent_pid: u32) -> PyResult<()> {
    bessemer_ffi::set_parent_death_signal(expected_parent_pid).map_err(PyErr::from)
}

#[pyfunction]
fn _parse_json(py: Python<'_>, data: &Bound<'_, PyBytes>) -> PyResult<Py<PyAny>> {
    let input = data.as_bytes();
    let value = py
        .detach(|| parse_json(input))
        .map_err(|_| PyValueError::new_err("Invalid JSON request body"))?;
    json_to_python(py, &value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_conversion_rejects_cycles_and_invalid_values() {
        Python::attach(|py| {
            for expression in [c"float('nan')", c"{1: 'wrong key'}"] {
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

    #[tokio::test]
    async fn completion_releases_input_and_admission_before_publishing_output() {
        for output_capacity in [64, 1] {
            let request_bytes = budget::ByteBudget::new(8);
            let response_bytes = budget::ByteBudget::new(output_capacity);
            let (body, input_permit) =
                budget::buffer_body(Body::from("12345678"), 8, &request_bytes)
                    .await
                    .unwrap();
            drop(body);
            let capacity = Arc::new(Semaphore::new(1));
            let permit = capacity.clone().try_acquire_owned().unwrap();
            let (receiver, mut guard) = Python::attach(|py| {
                let module = PyModule::from_code(
                    py,
                    c"from concurrent.futures import Future\ndef dispatch(route_id, arguments):\n    future = Future()\n    future.set_result((200, {'ok': True}))\n    return future\n",
                    c"budget_test.py",
                    c"budget_test",
                )
                .unwrap();
                let state = ServerState {
                    dispatch: module.getattr("dispatch").unwrap().unbind(),
                    capacity: capacity.clone(),
                    max_body_size: 8,
                    max_response_size: 64,
                    request_bytes: request_bytes.clone(),
                    response_bytes: response_bytes.clone(),
                    request_timeout: Duration::from_secs(1),
                };
                dispatch(
                    &state,
                    0,
                    &Map::new(),
                    Admission {
                        _request: permit,
                        _input: input_permit,
                    },
                    None,
                    false,
                    Instant::now() + Duration::from_secs(60),
                )
                .unwrap()
            });
            // The ready response is still in its oneshot queue at this point.
            assert_eq!(capacity.available_permits(), 1);
            assert_eq!(request_bytes.available(), 8);
            let response = receiver.await.unwrap();
            guard.armed = false;
            if output_capacity == 1 {
                assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
                assert_eq!(response.headers()[header::RETRY_AFTER], "1");
                assert_eq!(response_bytes.available(), 1);
            } else {
                assert_eq!(response.status(), StatusCode::OK);
                let bytes = axum::body::to_bytes(response.into_body(), 64)
                    .await
                    .unwrap();
                assert_eq!(&bytes[..], br#"{"ok":true}"#);
                assert_eq!(response_bytes.available(), output_capacity - bytes.len());
                drop(bytes);
                assert_eq!(response_bytes.available(), output_capacity);
            }
        }
    }

    #[test]
    fn head_responses_do_not_reserve_unsent_payload_bytes() {
        let budget = budget::ByteBudget::new(1);
        let held = budget.own(vec![0u8]).unwrap();
        Python::attach(|py| {
            let result = py.eval(c"(200, {'ok': True})", None, None).unwrap();
            assert!(matches!(
                decode_response(&result, 64, &budget, false, &mut None),
                Err(ResponseError::Full)
            ));
            let head = decode_response(&result, 64, &budget, true, &mut None).unwrap();
            assert_eq!(head.status(), StatusCode::OK);
            assert_eq!(head.headers()[header::CONTENT_TYPE], "application/json");
        });
        assert_eq!(budget.available(), 0);
        drop(held);
        assert_eq!(budget.available(), 1);
    }

    #[tokio::test]
    async fn ready_responses_after_absolute_deadline_are_timeouts() {
        let deadline = Instant::now() - Duration::from_millis(1);
        let result =
            tokio::time::timeout_at(deadline, std::future::ready(Response::new(Body::empty())))
                .await;
        // This is the Tokio inner-future-first race, without scheduling or sleeps.
        assert!(result.is_ok());
        assert_eq!(
            finish_before_deadline(result, deadline).status(),
            StatusCode::GATEWAY_TIMEOUT
        );
        let deadline = Instant::now() + Duration::from_secs(60);
        let result =
            tokio::time::timeout_at(deadline, std::future::ready(Response::new(Body::empty())))
                .await;
        assert_eq!(
            finish_before_deadline(result, deadline).status(),
            StatusCode::OK
        );
    }

    #[tokio::test]
    async fn expired_requests_do_not_submit_python_work_and_release_input() {
        let request_bytes = budget::ByteBudget::new(8);
        let (_, input_permit) = budget::buffer_body(Body::from("12345678"), 8, &request_bytes)
            .await
            .unwrap();
        let capacity = Arc::new(Semaphore::new(1));
        let admission = Admission {
            _request: capacity.clone().try_acquire_owned().unwrap(),
            _input: input_permit,
        };
        Python::attach(|py| {
            let state = ServerState {
                dispatch: py
                    .eval(
                        c"lambda *args: (_ for _ in ()).throw(AssertionError('must not submit'))",
                        None,
                        None,
                    )
                    .unwrap()
                    .unbind(),
                capacity: capacity.clone(),
                max_body_size: 8,
                max_response_size: 64,
                request_bytes: request_bytes.clone(),
                response_bytes: budget::ByteBudget::new(64),
                request_timeout: Duration::from_secs(1),
            };
            let result = dispatch(
                &state,
                0,
                &Map::new(),
                admission,
                None,
                false,
                Instant::now() - Duration::from_millis(1),
            );
            assert!(matches!(result, Err(DispatchError::Expired)));
        });
        assert_eq!(capacity.available_permits(), 1);
        assert_eq!(request_bytes.available(), 8);
    }

    #[tokio::test]
    async fn input_and_admission_survive_client_cancellation_until_actual_completion() {
        let request_bytes = budget::ByteBudget::new(8);
        let response_bytes = budget::ByteBudget::new(64);
        let (_, input_permit) = budget::buffer_body(Body::from("12345678"), 8, &request_bytes)
            .await
            .unwrap();
        let capacity = Arc::new(Semaphore::new(1));
        let admission = Admission {
            _request: capacity.clone().try_acquire_owned().unwrap(),
            _input: input_permit,
        };
        let module = Python::attach(|py| {
            PyModule::from_code(
            py,
            c"from concurrent.futures import Future\nclass Running(Future):\n    def cancel(self):\n        self.cancel_requested = True\n        return False\nfuture = Running()\ndef dispatch(route_id, arguments):\n    return future\ndef complete():\n    future.set_result((200, {'ok': True}))\n",
            c"running_budget_test.py", c"running_budget_test",
        ).unwrap().unbind()
        });
        let (receiver, guard) = Python::attach(|py| {
            let state = ServerState {
                dispatch: module.bind(py).getattr("dispatch").unwrap().unbind(),
                capacity: capacity.clone(),
                max_body_size: 8,
                max_response_size: 64,
                request_bytes: request_bytes.clone(),
                response_bytes: response_bytes.clone(),
                request_timeout: Duration::from_secs(1),
            };
            dispatch(
                &state,
                0,
                &Map::new(),
                admission,
                None,
                false,
                Instant::now() + Duration::from_secs(60),
            )
            .unwrap()
        });
        drop(receiver);
        drop(guard);
        assert_eq!(capacity.available_permits(), 0);
        assert_eq!(request_bytes.available(), 0);
        Python::attach(|py| {
            assert!(
                module
                    .bind(py)
                    .getattr("future")
                    .unwrap()
                    .getattr("cancel_requested")
                    .unwrap()
                    .extract::<bool>()
                    .unwrap()
            );
            module
                .bind(py)
                .getattr("complete")
                .unwrap()
                .call0()
                .unwrap();
        });
        assert_eq!(capacity.available_permits(), 1);
        assert_eq!(request_bytes.available(), 8);
        assert_eq!(response_bytes.available(), 64);
    }
}
