#![forbid(unsafe_code)]

/// Write one diagnostic line to stderr in a single write. Worker processes
/// share stderr with each other and with their access logs, and `eprintln!`
/// writes each formatted piece separately, which lets another process's
/// record land in the middle of the line (and tear both).
macro_rules! diagnostic {
    ($($argument:tt)*) => {
        $crate::telemetry::write_diagnostic(&format!($($argument)*))
    };
}

mod bridge;
mod invoker;

// The native side allocates per request (HTTP parsing, headers, JSON values,
// queue entries). mimalloc's thread-local free lists are markedly cheaper
// than glibc malloc for this pattern. Python's own allocations are unaffected.
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

mod budget;
mod compression;
mod controls;
mod conversion;
mod cookies;
mod forms;
mod http1;
mod jwks;
mod jwt;
mod otel;

mod permessage_deflate;
mod redis;
mod router;
mod static_files;
mod streaming;
mod telemetry;
mod tls;
mod transport;
mod uploads;
mod websocket;

use conversion::{parse_integer, python_to_json, python_to_json_limited};

use axum::{
    Json,
    body::Body,
    extract::Request,
    http::{HeaderMap, HeaderName, HeaderValue, Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use pyo3::{
    exceptions::{PyTypeError, PyValueError},
    prelude::*,
    types::{PyBytes, PyCFunction, PyDict, PyList, PyTuple},
};
use serde_json::{Value, json};
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

/// Where a parameter's value comes from, resolved once when routes load.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Source {
    Path,
    Query,
    Header,
    Cookie,
    Form,
    Body,
    File,
}

impl Source {
    fn parse(source: &str) -> Option<Self> {
        Some(match source {
            "path" => Self::Path,
            "query" => Self::Query,
            "header" => Self::Header,
            "cookie" => Self::Cookie,
            "form" => Self::Form,
            "body" => Self::Body,
            "file" => Self::File,
            _ => return None,
        })
    }

    /// The error location, as FastAPI reports it: form fields and files are
    /// under `body`.
    fn location(self) -> &'static str {
        match self {
            Self::Path => "path",
            Self::Query => "query",
            Self::Header => "header",
            Self::Cookie => "cookie",
            Self::Form | Self::File | Self::Body => "body",
        }
    }
}

/// A parameter's declared type, resolved once when routes load.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Kind {
    Str,
    Int,
    Float,
    Bool,
    Json,
    Upload,
    Bytes,
}

impl Kind {
    fn parse(kind: &str) -> Option<Self> {
        Some(match kind {
            "str" => Self::Str,
            "int" => Self::Int,
            "float" => Self::Float,
            "bool" => Self::Bool,
            "json" => Self::Json,
            "upload" => Self::Upload,
            "bytes" => Self::Bytes,
            _ => return None,
        })
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Str => "str",
            Self::Int => "int",
            Self::Float => "float",
            Self::Bool => "bool",
            Self::Json => "json",
            Self::Upload => "upload",
            Self::Bytes => "bytes",
        }
    }
}

#[derive(Clone)]
struct Parameter {
    /// Shared with each request's arguments, so binding it never copies.
    name: Arc<str>,
    alias: String,
    /// The alias as a header name, for header parameters with a valid one.
    header: Option<HeaderName>,
    source: Source,
    kind: Kind,
    required: bool,
    default: Value,
    /// File parameters that bind every matching part as a list.
    many: bool,
}

#[derive(Clone)]
struct Route {
    id: usize,
    path: String,
    method: Method,
    health: bool,
    static_body: Option<(Arc<[u8]>, HeaderValue)>,
    files: Option<Arc<static_files::StaticDir>>,
    needs_request: bool,
    needs_principal: bool,
    websocket: bool,
    status: u16,
    timeout: Option<Duration>,
    parameters: Vec<Parameter>,
    /// Starlette-style converters on path parameters (`{id:int}`), checked by
    /// the router. Empty for most routes.
    converters: Vec<(String, router::Converter)>,
    /// What the request path needs from the parameters, worked out once.
    plan: RoutePlan,
}

/// Per-route facts the request path would otherwise recompute from the
/// parameter list on every request.
#[derive(Clone, Copy, Default)]
struct RoutePlan {
    /// Parse the `Cookie` header: a cookie parameter, or the full request.
    reads_cookies: bool,
    /// A file parameter: multipart bodies may be spooled.
    has_files: bool,
    /// A form or file parameter: the body is parsed as a form.
    reads_form: bool,
    /// Parameters bound to the JSON body.
    body_count: usize,
}

impl RoutePlan {
    fn new(parameters: &[Parameter], needs_request: bool) -> Self {
        let has = |source| {
            parameters
                .iter()
                .any(|parameter| parameter.source == source)
        };
        Self {
            reads_cookies: needs_request || has(Source::Cookie),
            has_files: has(Source::File),
            reads_form: has(Source::Form) || has(Source::File),
            body_count: parameters
                .iter()
                .filter(|parameter| parameter.source == Source::Body)
                .count(),
        }
    }
}

struct ServerState {
    dispatch: Py<PyAny>,
    /// The native handoff queues, one per worker event loop; when present,
    /// `dispatch` is not called.
    native: bridge::Lanes,
    capacity: Arc<Semaphore>,
    max_body_size: usize,
    max_response_size: usize,
    request_bytes: budget::ByteBudget,
    response_bytes: budget::ByteBudget,
    request_timeout: Duration,
    /// Accept `permessage-deflate` when a WebSocket client offers it.
    websocket_compression: bool,
    /// Disk spooling for multipart bodies over `max_body_size`, when enabled.
    uploads: Option<uploads::Settings>,
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
        let converters = match item.get_item("converters") {
            Ok(value) if !value.is_none() => {
                // `[name, converter]` pairs.
                let pairs: Vec<Vec<String>> = value.extract()?;
                pairs
                    .into_iter()
                    .map(|pair| match <[String; 2]>::try_from(pair) {
                        Ok([name, converter]) => router::Converter::parse(&converter)
                            .map(|converter| (name, converter))
                            .ok_or_else(|| {
                                PyValueError::new_err(format!(
                                    "unknown path converter: {converter}"
                                ))
                            }),
                        Err(_) => Err(PyValueError::new_err(
                            "path converters must be [name, converter] pairs",
                        )),
                    })
                    .collect::<PyResult<Vec<_>>>()?
            }
            _ => Vec::new(),
        };
        // A static-files mount, a mounted ASGI app (a Python route) and a
        // `{name:path}` parameter end in a catch-all segment; nothing else
        // may use one.
        let tail = converters
            .iter()
            .find(|(_, converter)| *converter == router::Converter::Path)
            .map(|(name, _)| format!("/{{*{name}}}"));
        let kind = field(&item, "kind")?.extract::<String>()?;
        let literal = if let Some(tail) = &tail {
            path.strip_suffix(tail.as_str()).ok_or_else(|| {
                PyValueError::new_err("a {name:path} parameter must be the last segment")
            })?
        } else if kind == "files" || kind == "python" {
            path.strip_suffix("/{*path}").unwrap_or(&path)
        } else {
            &path
        };
        if !path.starts_with('/') || literal.contains(['?', '#', '*']) {
            return Err(PyValueError::new_err(
                "route paths must start with / and cannot contain ?, # or *",
            ));
        }
        for (name, _) in &converters {
            if !path.contains(&format!("{{{name}}}")) && !path.contains(&format!("{{*{name}}}")) {
                return Err(PyValueError::new_err(format!(
                    "path converter for an undeclared parameter: {name}"
                )));
            }
        }
        let method: String = field(&item, "method")?.extract()?;
        let method = Method::from_bytes(method.as_bytes())
            .map_err(|_| PyValueError::new_err("invalid HTTP method"))?;
        if router::allowed(&method).is_empty() {
            return Err(PyValueError::new_err("unsupported HTTP method"));
        }
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
            return Err(PyValueError::new_err(format!(
                "duplicate or ambiguous route: {method} {path}"
            )));
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
        if !["health", "python", "static", "websocket", "files"].contains(&kind.as_str()) {
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
            let (Some(source), Some(kind)) = (Source::parse(&source), Kind::parse(&kind)) else {
                return Err(PyValueError::new_err(
                    "unsupported parameter source or type",
                ));
            };
            let valid = match source {
                Source::Path | Source::Query | Source::Header | Source::Cookie | Source::Form => {
                    matches!(kind, Kind::Str | Kind::Int | Kind::Float | Kind::Bool)
                }
                Source::Body => !matches!(kind, Kind::Upload | Kind::Bytes),
                Source::File => matches!(kind, Kind::Upload | Kind::Bytes),
            };
            if !valid {
                return Err(PyValueError::new_err(
                    "unsupported parameter source or type",
                ));
            }
            let alias: String = parameter
                .get_item("alias")
                .or_else(|_| parameter.get_item("name"))?
                .extract()?;
            // An alias that is not a valid header name never matches one.
            let header = (source == Source::Header)
                .then(|| HeaderName::from_bytes(alias.as_bytes()).ok())
                .flatten();
            parameters.push(Parameter {
                name: field(&parameter, "name")?.extract::<String>()?.into(),
                alias,
                header,
                source,
                kind,
                required: field(&parameter, "required")?.extract()?,
                default: parameter
                    .get_item("default")
                    .map_or(Ok(Value::Null), |v| python_to_json(&v, 0))?,
                many: parameter
                    .get_item("many")
                    .map_or(Ok(false), |v| v.extract())?,
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
            files: if kind == "files" {
                let directory: String = field(&item, "directory")?.extract()?;
                let html: bool = field(&item, "html")?.extract()?;
                let cache_control: Option<String> = field(&item, "cache_control")?.extract()?;
                Some(Arc::new(
                    static_files::StaticDir::new(&directory, html, cache_control.as_deref())
                        .map_err(PyValueError::new_err)?,
                ))
            } else {
                None
            },
            needs_request: item
                .get_item("needs_request")
                .map_or(Ok(false), |v| v.extract())?,
            websocket: kind == "websocket",
            needs_principal: item
                .get_item("needs_principal")
                .map_or(Ok(false), |v| v.extract())?,
            status,
            timeout,
            plan: RoutePlan::default(),
            parameters,
            converters,
        });
        let route = output.last_mut().expect("just pushed");
        route.plan = RoutePlan::new(&route.parameters, route.needs_request);
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
            "type": kind, "loc": [location(parameter), &parameter.alias], "msg": message
        }]})),
    )
        .into_response()
}

/// Form fields and files are reported under `body`, as FastAPI does.
fn location(parameter: &Parameter) -> &str {
    parameter.source.location()
}

fn scalar(parameter: &Parameter, value: &str) -> Result<Value, Box<Response>> {
    let parsed = match parameter.kind {
        Kind::Str => Some(Value::String(value.to_owned())),
        Kind::Int => parse_integer(value),
        Kind::Float => value
            .parse::<f64>()
            .ok()
            .and_then(serde_json::Number::from_f64)
            .map(Value::Number),
        Kind::Bool => match value.to_ascii_lowercase().as_str() {
            "true" | "1" | "yes" | "on" => Some(Value::Bool(true)),
            "false" | "0" | "no" | "off" => Some(Value::Bool(false)),
            _ => None,
        },
        _ => None,
    };
    parsed.ok_or_else(|| {
        Box::new(invalid(
            parameter,
            &format!("{}_parsing", parameter.kind.as_str()),
            &format!("Input should be a valid {}", parameter.kind.as_str()),
            Value::String(value.to_owned()),
        ))
    })
}

pub(crate) enum CancelTarget {
    Python(Py<PyAny>),
    /// A job on the native queue; cancelling it never takes the GIL.
    Native(Arc<bridge::Shared>, u64),
}

pub(crate) struct CancelOnDrop {
    pub(crate) target: CancelTarget,
    pub(crate) armed: bool,
}

impl Drop for CancelOnDrop {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        match &self.target {
            CancelTarget::Python(future) => Python::attach(|py| {
                let _ = future.bind(py).call_method0("cancel");
            }),
            CancelTarget::Native(shared, id) => shared.cancel(*id),
        }
    }
}

pub(crate) struct Admission {
    _request: OwnedSemaphorePermit,
    _input: budget::BytePermit,
    /// Disk budget of a spooled upload, released with the request.
    _spool: budget::BytePermit,
    /// The event loop's share of admitted requests, for picking a loop.
    _lane: bridge::LaneLoad,
}

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

/// The absolute processing deadline of a request, carried on its response so
/// later native work such as buffered compression stays inside the budget.
#[derive(Clone, Copy, Debug)]
struct ProcessingDeadline(Instant);

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
        // Room for content-type plus the request id, security headers and
        // content-length added later, so none of them reallocates the map.
        HeaderMap::with_capacity(8)
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
    if kind != "json" && kind != "bytes" && kind != "json_bytes" {
        return Err(PyValueError::new_err("unsupported internal response kind").into());
    }
    let body = if matches!(status, 204 | 205 | 304) {
        headers.remove(header::CONTENT_TYPE);
        Vec::new()
    } else if kind == "bytes" || kind == "json_bytes" {
        let payload = result.get_item(1)?;
        let bytes = payload
            .cast::<PyBytes>()
            .map_err(|_| PyTypeError::new_err("raw responses must contain bytes"))?
            .as_bytes();
        if bytes.len() > max_bytes {
            return Err(PyValueError::new_err("response exceeds the configured size limit").into());
        }
        if kind == "json_bytes" {
            // Already encoded by _encode_plain_json from exact JSON values.
            headers
                .entry(header::CONTENT_TYPE)
                .or_insert(HeaderValue::from_static("application/json"));
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
    secure: bool,
    client_certificate: Option<Arc<[u8]>>,
    /// The body went to a spool file, so it is not available as bytes.
    spooled: bool,
}

#[derive(Clone)]
struct RequestInfo {
    /// Header-safe ASCII; converted to text only for handlers that read it.
    request_id: Option<HeaderValue>,
    client: Option<std::net::IpAddr>,
    claims: Option<Arc<Value>>,
    /// The connection is TLS terminated by this server.
    secure: bool,
    /// The verified TLS client certificate (leaf, DER), when one was presented.
    client_certificate: Option<Arc<[u8]>>,
    /// The `traceparent` and `tracestate` handlers run under, when tracing.
    trace: Option<(String, Option<String>)>,
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

#[allow(clippy::too_many_arguments)]
fn dispatch(
    state: &ServerState,
    route_id: usize,
    argument_values: &bridge::Arguments,
    uploads: Vec<forms::Upload>,
    admission: Admission,
    metadata: Option<RequestMetadata>,
    head: bool,
    deadline: Instant,
) -> Result<(oneshot::Receiver<Response>, CancelOnDrop), DispatchError> {
    Python::attach(|py| {
        // Construct arguments directly without an intermediate cloned JSON tree.
        let arguments =
            bridge::python_arguments(py, argument_values, None, uploads, metadata, None)?;
        if Instant::now() >= deadline {
            return Err(DispatchError::Expired);
        }
        let future = state.dispatch.bind(py).call1((route_id, arguments))?;
        let guard = CancelOnDrop {
            target: CancelTarget::Python(future.clone().unbind()),
            armed: true,
        };
        let (sender, receiver) = oneshot::channel();
        // The permit lives until the Python task actually completes, including cancellation
        // cleanup. The Python bridge deliberately does not mark a cancellation request done.
        let pending: Mutex<Option<bridge::Pending>> = Mutex::new(Some(bridge::Pending {
            sender,
            admission,
            head,
            route_id,
            max_response_size: state.max_response_size,
            response_bytes: state.response_bytes.clone(),
        }));
        let callback = PyCFunction::new_closure(
            py,
            None,
            None,
            move |args: &Bound<'_, PyTuple>, _kwargs: Option<&Bound<'_, PyDict>>| -> PyResult<()> {
                if let Some(pending) = pending.lock().unwrap().take() {
                    pending.finish(
                        args.get_item(0)
                            .and_then(|future| future.call_method0("result")),
                    );
                }
                Ok(())
            },
        )?;
        future.call_method1("add_done_callback", (callback,))?;
        Ok((receiver, guard))
    })
}

/// Queue a request for the asyncio worker without taking the GIL.
#[allow(clippy::too_many_arguments)]
fn submit_native(
    state: &ServerState,
    shared: &Arc<bridge::Shared>,
    route_id: usize,
    arguments: bridge::Arguments,
    raw_body: Option<Vec<u8>>,
    uploads: Vec<forms::Upload>,
    mut admission: Admission,
    metadata: Option<RequestMetadata>,
    head: bool,
    deadline: Instant,
) -> Result<(oneshot::Receiver<Response>, CancelOnDrop), DispatchError> {
    if Instant::now() >= deadline {
        return Err(DispatchError::Expired);
    }
    shared.enter(&mut admission);
    let (sender, receiver) = oneshot::channel();
    let pending = bridge::Pending {
        sender,
        admission,
        head,
        route_id,
        max_response_size: state.max_response_size,
        response_bytes: state.response_bytes.clone(),
    };
    let id = shared
        .submit(
            route_id, arguments, raw_body, uploads, metadata, deadline, pending,
        )
        .map_err(|_| DispatchError::Invalid)?;
    Ok((
        receiver,
        CancelOnDrop {
            target: CancelTarget::Native(shared.clone(), id),
            armed: true,
        },
    ))
}

async fn handle(
    state: &ServerState,
    route: &Route,
    path: router::PathParams,
    info: RequestInfo,
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
    if let Some(files) = &route.files {
        let relative = path.ok().and_then(|mut path| path.remove("path"));
        let Some(relative) = relative.or_else(|| {
            // The bare mount prefix (`/site`) has no catch-all segment.
            request.uri().path().ends_with('/').then(String::new)
        }) else {
            return static_files::redirect_to_directory(request.uri());
        };
        return Box::pin(files.serve(&relative, request.uri(), request.headers())).await;
    }
    if route.websocket {
        return Box::pin(handle_websocket(state, route, path, info, request)).await;
    }
    let permit = match state.capacity.clone().try_acquire_owned() {
        Ok(permit) => permit,
        Err(_) => return capacity_response("Server is at capacity", true),
    };
    let timeout = route.timeout.unwrap_or(state.request_timeout);
    let deadline = Instant::now() + timeout;
    let mut body_complete = false;
    // Admission and byte budgets move into each blocking spool operation, so
    // a cancelled upload keeps them until the thread has finished.
    let mut held = uploads::Held {
        admission: Some(permit),
        ..uploads::Held::default()
    };
    // Boxed: the request's own future is over a kilobyte, and the timeout
    // would otherwise be copied with it into this future.
    let result = tokio::time::timeout_at(deadline, Box::pin(async {
        let path = match path {
            Ok(value) => value,
            Err(_) => return error(StatusCode::BAD_REQUEST, "Invalid path parameters"),
        };
        // Split once: headers and extensions are borrowed from the head while
        // the body is buffered, so nothing is cloned for routes that only
        // read their declared inputs.
        let (parts, body) = request.into_parts();
        let raw_query = parts.uri.query().unwrap_or("");
        // Decoded pairs borrow the target unless they needed decoding. A
        // repeated key keeps its last value, so lookups search from the end.
        // Decoding is lossy, as it was through serde_urlencoded.
        let query: Vec<(Cow<'_, str>, Cow<'_, str>)> =
            form_urlencoded::parse(raw_query.as_bytes()).collect();
        let headers = &parts.headers;
        let secure = info.secure;
        let client_certificate = info.client_certificate.clone();
        let request_info = Some(info);
        let cookies = if route.plan.reads_cookies {
            match cookies::parse(headers) {
                Ok(value) => value,
                Err(detail) => return error(StatusCode::BAD_REQUEST, detail),
            }
        } else {
            HashMap::new()
        };
        let content_type = headers
            .get(header::CONTENT_TYPE)
            .map(|value| value.to_str().unwrap_or(""));
        // A file route spools a multipart body to disk unless it declares a
        // length that fits the in-memory limit.
        let spool_boundary = match (&state.uploads, content_type.and_then(forms::form_kind)) {
            (Some(_), Some(forms::FormKind::Multipart(boundary)))
                if route.plan.has_files
                    && headers
                        .get(header::CONTENT_LENGTH)
                        .and_then(|value| value.to_str().ok())
                        .and_then(|value| value.parse::<usize>().ok())
                        .is_none_or(|length| length > state.max_body_size) =>
            {
                Some(boundary)
            }
            _ => None,
        };
        let mut spool_file: Option<Arc<std::fs::File>> = None;
        let mut spooled_fields = Vec::new();
        let mut spooled_files = Vec::new();
        let read = match (&spool_boundary, &state.uploads) {
            (Some(boundary), Some(settings)) => {
                match Box::pin(uploads::spool(
                    body,
                    boundary,
                    settings,
                    state.max_body_size,
                    &state.request_bytes,
                    &mut held,
                ))
                .await
                {
                    Ok(spooled) => {
                        spool_file = spooled.file;
                        spooled_fields = spooled.fields;
                        spooled_files = spooled.files;
                        Ok((Vec::new(), budget::BytePermit::default()))
                    }
                    Err(uploads::Error::Full) => {
                        return capacity_response("Upload capacity is full", true);
                    }
                    Err(spool_error) => {
                        let (status, detail) = match spool_error {
                            uploads::Error::TooLarge => (
                                StatusCode::PAYLOAD_TOO_LARGE,
                                "Request body exceeds the configured upload limit",
                            ),
                            uploads::Error::FieldsTooLarge => (
                                StatusCode::PAYLOAD_TOO_LARGE,
                                "Form fields exceed the configured body limit",
                            ),
                            uploads::Error::Malformed(detail) => (StatusCode::BAD_REQUEST, detail),
                            uploads::Error::Io => {
                                diagnostic!("bessemer: could not write an upload spool file");
                                (
                                    StatusCode::INTERNAL_SERVER_ERROR,
                                    "Could not store the upload",
                                )
                            }
                            _ => (StatusCode::BAD_REQUEST, "Invalid request body"),
                        };
                        let mut response = error(status, detail);
                        response
                            .headers_mut()
                            .insert(header::CONNECTION, HeaderValue::from_static("close"));
                        return response;
                    }
                }
            }
            _ => budget::buffer_body(body, state.max_body_size, &state.request_bytes).await,
        };
        let (body, input_permit) = match read {
            Ok(value) => value,
            Err(budget::ReadError::Full) => {
                return capacity_response("Request byte capacity is full", true);
            }
            Err(read_error) => {
                let mut response = match read_error {
                    budget::ReadError::TooLarge => error(
                        StatusCode::PAYLOAD_TOO_LARGE,
                        "Request body exceeds the configured limit",
                    ),
                    _ => error(StatusCode::BAD_REQUEST, "Invalid request body"),
                };
                response
                    .headers_mut()
                    .insert(header::CONNECTION, HeaderValue::from_static("close"));
                return response;
            }
        };
        body_complete = true;
        let mut body = body;
        let needs_form = route.plan.reads_form;
        let mut form_fields: HashMap<String, String> = HashMap::new();
        let mut form_files: HashMap<String, Vec<forms::Part>> = HashMap::new();
        let mut shared_body: Option<bytes::Bytes> = None;
        // A spooled body was parsed as it arrived.
        form_fields.extend(spooled_fields);
        for part in spooled_files {
            if part.filename.as_deref() == Some("") && part.data.is_empty() {
                continue;
            }
            form_files.entry(part.name.clone()).or_default().push(part);
        }
        if needs_form && !body.is_empty() {
            let kind = content_type.and_then(forms::form_kind);
            let parsed = match kind {
                Some(forms::FormKind::UrlEncoded) => {
                    forms::parse_urlencoded(&body).map(|fields| (fields, Vec::new()))
                }
                Some(forms::FormKind::Multipart(boundary)) => {
                    forms::parse_multipart(&body, &boundary).and_then(|parts| {
                        let mut fields = Vec::new();
                        let mut files = Vec::new();
                        for part in parts {
                            if part.filename.is_some() {
                                files.push(part);
                            } else {
                                let value = std::str::from_utf8(&body[part.data.clone()])
                                    .map_err(|_| "Form fields must be UTF-8")?;
                                fields.push((part.name, value.to_owned()));
                            }
                        }
                        Ok((fields, files))
                    })
                }
                None => {
                    return error(
                        StatusCode::UNSUPPORTED_MEDIA_TYPE,
                        "Expected an application/x-www-form-urlencoded or multipart/form-data body",
                    );
                }
            };
            let (fields, files) = match parsed {
                Ok(value) => value,
                Err(detail) => return error(StatusCode::BAD_REQUEST, detail),
            };
            // Repeated scalar fields keep the last value, as query strings do.
            form_fields.extend(fields);
            for part in files {
                // A browser submits an empty, unnamed part for "no file chosen".
                if part.filename.as_deref() == Some("") && part.data.is_empty() {
                    continue;
                }
                form_files.entry(part.name.clone()).or_default().push(part);
            }
            if !form_files.is_empty() {
                // Uploads borrow the body without copying it.
                shared_body = Some(bytes::Bytes::from(std::mem::take(&mut body)));
            }
        }
        let body_count = route.plan.body_count;
        let needs_body = body_count > 0;
        let lane = state.native.choose();
        // A single body bound to a model with a native plan travels to the
        // loop as bytes and is parsed straight into the model there. Here it
        // is only checked, so invalid JSON is still refused before any other
        // parameter, with the same 422.
        let raw_body = body_count == 1
            && !route.needs_request
            && !body.is_empty()
            && lane.is_some_and(|lane| lane.has_body_plan(route.id));
        let mut body_value = if needs_body && !body.is_empty() {
            if let Some(content_type) = content_type {
                let media_type = content_type.parse::<mime::Mime>().ok();
                if !media_type.is_some_and(|value| {
                    value.type_() == mime::APPLICATION
                        && (value.subtype() == mime::JSON || value.suffix() == Some(mime::JSON))
                        && value.get_param(mime::CHARSET).is_none_or(|charset| {
                            charset.as_str().eq_ignore_ascii_case("utf-8")
                                || charset.as_str().eq_ignore_ascii_case("utf8")
                        })
                }) {
                    return error(
                        StatusCode::UNSUPPORTED_MEDIA_TYPE,
                        "Expected an application/json body",
                    );
                }
            }
            let parsed = if raw_body {
                bessemer_codec::validate_json(&body).map(|()| None)
            } else {
                bessemer_codec::parse_value(&body).map(Some)
            };
            match parsed {
                Ok(value) => value,
                Err(_) => return (StatusCode::UNPROCESSABLE_ENTITY, Json(json!({"detail": [{
                    "type": "json_invalid", "loc": ["body"], "msg": "Invalid JSON", "input": null
                }]}))).into_response(),
            }
        } else {
            None
        };
        let mut arguments = bridge::Arguments::with_capacity(route.parameters.len());
        let mut body_bindings = body_count;
        let mut uploads = Vec::new();
        // Spooled parts bound to `bytes` are read into memory, so they share
        // the field values' body limit and request byte budget.
        let mut spooled_bytes = 0usize;
        for parameter in &route.parameters {
            if parameter.source == Source::File {
                // Read without consuming: a dependency and its handler may
                // both bind the same file field.
                let parts = form_files
                    .get(&parameter.alias)
                    .cloned()
                    .unwrap_or_default();
                if parts.is_empty() {
                    if parameter.required {
                        return invalid(parameter, "missing", "Field required", Value::Null);
                    }
                    arguments.insert(parameter.name.clone(), parameter.default.clone());
                    continue;
                }
                if !parameter.many && parts.len() > 1 {
                    return invalid(
                        parameter,
                        "too_many_files",
                        "Expected a single file; declare a list to accept several",
                        Value::Null,
                    );
                }
                for part in parts {
                    if spool_boundary.is_some() && parameter.kind == Kind::Bytes {
                        spooled_bytes = spooled_bytes.saturating_add(part.data.len());
                        if spooled_bytes > state.max_body_size {
                            return error(
                                StatusCode::PAYLOAD_TOO_LARGE,
                                "A bytes file parameter exceeds the configured body limit; \
                                 use UploadFile for large files",
                            );
                        }
                        if held
                            .memory
                            .extend(&state.request_bytes, part.data.len())
                            .is_err()
                        {
                            return capacity_response("Request byte capacity is full", true);
                        }
                    }
                    let data = match (&spool_file, &shared_body) {
                        (Some(file), _) => forms::UploadData::Spooled {
                            file: file.clone(),
                            range: part.data,
                        },
                        (None, Some(body)) => forms::UploadData::Memory(body.slice(part.data)),
                        // A spooled body whose file parts were all empty.
                        (None, None) => forms::UploadData::Memory(bytes::Bytes::new()),
                    };
                    uploads.push(forms::Upload {
                        key: parameter.name.to_string(),
                        many: parameter.many,
                        raw: parameter.kind == Kind::Bytes,
                        filename: part.filename.unwrap_or_default(),
                        content_type: part.content_type,
                        headers: part.headers,
                        data,
                    });
                }
                continue;
            }
            if raw_body && parameter.source == Source::Body {
                continue;
            }
            let value = if parameter.source == Source::Body {
                body_bindings -= 1;
                if body_bindings == 0 {
                    body_value.take()
                } else {
                    body_value.clone()
                }
            } else {
                let header_value = match (&parameter.header, parameter.source) {
                    (Some(name), Source::Header) => headers.get(name).map(header_text),
                    _ => None,
                };
                let input = if parameter.source == Source::Path {
                    path.get(&parameter.alias).map(String::as_str)
                } else if parameter.source == Source::Header {
                    if parameter
                        .header
                        .as_ref()
                        .is_some_and(|name| headers.get_all(name).iter().nth(1).is_some())
                    {
                        return invalid(
                            parameter,
                            "multiple_header_values",
                            "A scalar header parameter must have exactly one value",
                            Value::Null,
                        );
                    }
                    header_value.as_deref()
                } else if parameter.source == Source::Cookie {
                    cookies.get(&parameter.alias).map(String::as_str)
                } else if parameter.source == Source::Form {
                    form_fields.get(&parameter.alias).map(String::as_str)
                } else {
                    query
                        .iter()
                        .rev()
                        .find(|(key, _)| *key == parameter.alias)
                        .map(|(_, value)| &**value)
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
        if uploads
            .iter()
            .any(|upload| upload.raw && matches!(upload.data, forms::UploadData::Spooled { .. }))
        {
            match Box::pin(uploads::materialize(
                std::mem::take(&mut uploads),
                &mut held,
            ))
            .await
            {
                Ok(read) => uploads = read,
                Err(_) => {
                    diagnostic!("bessemer: could not read an upload spool file");
                    return error(
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "Could not read the upload",
                    );
                }
            }
        }
        if let Some((parent, state)) = request_info.as_ref().and_then(|info| info.trace.clone()) {
            // The worker attaches it as the OpenTelemetry context and removes it.
            arguments.insert(
                "__bessemer_trace__".into(),
                json!({"traceparent": parent, "tracestate": state}),
            );
        }
        if route.needs_principal {
            // Verified natively by the JWT control; absent on excluded paths.
            let claims = request_info
                .as_ref()
                .and_then(|info| info.claims.as_deref())
                .cloned();
            arguments.insert(
                "__bessemer_principal__".into(),
                claims.unwrap_or(Value::Null),
            );
        }
        // `needs_request` routes never take the raw path, so they keep the body.
        let raw_body = raw_body.then(|| std::mem::take(&mut body));
        let metadata = if route.needs_request {
            Some(RequestMetadata {
                method: parts.method.to_string(),
                path: parts.uri.path().to_owned(),
                query_string: raw_query.to_owned(),
                headers: headers
                    .iter()
                    .map(|(name, value)| (name.to_string(), header_text(value).into_owned()))
                    .collect(),
                client: request_info
                    .as_ref()
                    .and_then(|info| info.client.map(|ip| ip.to_string())),
                request_id: request_info
                    .and_then(|info| info.request_id)
                    .and_then(|value| value.to_str().ok().map(str::to_owned)),
                body: match &shared_body {
                    Some(shared) => shared.to_vec(),
                    None => body,
                },
                path_params: path,
                cookies,
                secure,
                client_certificate,
                spooled: spool_boundary.is_some(),
            })
        } else {
            drop(body);
            None
        };
        held.memory.absorb(input_permit);
        let admission = Admission {
            _request: held
                .admission
                .take()
                .expect("blocking spool operations return the admission permit"),
            _input: std::mem::take(&mut held.memory),
            _spool: std::mem::take(&mut held.disk),
            _lane: bridge::LaneLoad::default(),
        };
        let dispatched = match lane {
            Some(shared) => submit_native(
                state, shared, route.id, arguments, raw_body, uploads, admission, metadata, head,
                deadline,
            ),
            None => {
                let result = dispatch(
                    state, route.id, &arguments, uploads, admission, metadata, head, deadline,
                );
                drop(arguments);
                result
            }
        };
        let (receiver, mut guard) = match dispatched {
            Ok(value) => value,
            Err(DispatchError::Expired) => {
                return error(StatusCode::GATEWAY_TIMEOUT, "Request timed out");
            }
            Err(DispatchError::Invalid) => {
                return error(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error");
            }
        };
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
            *response.body_mut() = Body::new(streaming::start(
                stream,
                state.response_bytes.clone(),
                timeout,
            ));
        }
        response
    }))
    .await;
    let mut response = finish_before_deadline(result, deadline);
    response
        .extensions_mut()
        .insert(ProcessingDeadline(deadline));
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

/// WebSocket sessions are bounded by the connection limit rather than request
/// capacity; the handler reads its inputs from the WebSocket's request view.
async fn handle_websocket(
    state: &ServerState,
    route: &Route,
    path: router::PathParams,
    info: RequestInfo,
    request: Request,
) -> Response {
    let Ok(path_params) = path else {
        return error(StatusCode::BAD_REQUEST, "Invalid path parameters");
    };
    let headers = request.headers();
    let secure = info.secure;
    let info = Some(info);
    let header_pairs: Vec<(String, String)> = headers
        .iter()
        .map(|(name, value)| (name.to_string(), header_text(value).into_owned()))
        .collect();
    let cookies = match cookies::parse(headers) {
        Ok(cookies) => cookies,
        Err(detail) => return error(StatusCode::BAD_REQUEST, detail),
    };
    let request_path = request.uri().path().to_owned();
    let query_string = request.uri().query().unwrap_or("").to_owned();
    let timeout = route.timeout.unwrap_or(state.request_timeout);
    let metadata = move |py: Python<'_>| -> PyResult<Py<PyDict>> {
        let data = PyDict::new(py);
        data.set_item("method", "GET")?;
        data.set_item("path", request_path)?;
        data.set_item("query_string", query_string)?;
        data.set_item("headers", header_pairs)?;
        data.set_item(
            "client",
            info.as_ref()
                .and_then(|info| info.client.map(|ip| ip.to_string())),
        )?;
        data.set_item("scheme", if secure { "wss" } else { "ws" })?;
        if let Some(certificate) = info
            .as_ref()
            .and_then(|info| info.client_certificate.as_deref())
        {
            data.set_item("client_certificate", PyBytes::new(py, certificate))?;
        }
        data.set_item(
            "request_id",
            info.as_ref()
                .and_then(|info| info.request_id.as_ref())
                .and_then(|value| value.to_str().ok().map(str::to_owned)),
        )?;
        data.set_item("path_params", path_params)?;
        data.set_item("cookies", cookies)?;
        if let Some(claims) = info.as_ref().and_then(|info| info.claims.as_deref()) {
            data.set_item("claims", conversion::json_to_python(py, claims)?)?;
        }
        Ok(data.unbind())
    };
    websocket::handle(
        &state.dispatch,
        route.id,
        request,
        metadata,
        timeout,
        state.max_body_size,
        state.websocket_compression,
    )
    .await
}

#[pyfunction]
#[pyo3(signature = (routes, dispatch, host, port, *, max_body_size=1_048_576, max_in_flight=128, request_timeout=30.0, graceful_timeout=30.0, max_response_size=4_194_304, max_request_bytes_in_flight=33_554_432, max_response_bytes_in_flight=33_554_432, max_connections=1024, header_timeout=10.0, max_headers=64, max_header_size=32768, max_request_target_size=8192, listener_fd=None, ready_fd=None, max_requests=0, controls=None, shutdown_fd=None, tls_cert=None, tls_key=None, tls_min="1.3", http="auto", tls_client_ca=None, tls_client_crl=None, tls_ocsp_response=None, tls_client_auth="required", tls_reload_interval=60.0, websocket_compression="none", max_upload_size=0, max_upload_bytes_in_flight=4_294_967_296, upload_dir=None, event_loop=None))]
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
    shutdown_fd: Option<i32>,
    tls_cert: Option<String>,
    tls_key: Option<String>,
    tls_min: &str,
    http: &str,
    tls_client_ca: Option<String>,
    tls_client_crl: Option<String>,
    tls_ocsp_response: Option<String>,
    tls_client_auth: &str,
    tls_reload_interval: f64,
    websocket_compression: &str,
    max_upload_size: u64,
    max_upload_bytes_in_flight: u64,
    upload_dir: Option<String>,
    event_loop: Option<Bound<'_, PyAny>>,
) -> PyResult<()> {
    // With the Rust event loop, connections are served on its own thread by
    // its embedded runtime instead of by a separate Tokio runtime.
    #[cfg(target_os = "linux")]
    let embedded = match &event_loop {
        Some(event_loop) => {
            let core = event_loop
                .cast::<bessemer_loop::LoopCore>()
                .map_err(|_| PyTypeError::new_err("event_loop must be a bessemer._loop.Loop"))?;
            Some((core.get().runtime()?, core.clone().unbind()))
        }
        None => None,
    };
    #[cfg(not(target_os = "linux"))]
    if event_loop.is_some() {
        return Err(PyValueError::new_err(
            "event_loop is only supported on Linux",
        ));
    }
    if !tls_reload_interval.is_finite() || !(0.0..=86_400.0).contains(&tls_reload_interval) {
        return Err(PyValueError::new_err(
            "tls_reload_interval must be between 0 and 86400 seconds",
        ));
    }
    let http2 = match http {
        "auto" => true,
        "1.1" => false,
        _ => return Err(PyValueError::new_err("http must be 'auto' or '1.1'")),
    };
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
    let uploads = if max_upload_size == 0 {
        if upload_dir.is_some() {
            return Err(PyValueError::new_err("upload_dir needs max_upload_size"));
        }
        None
    } else {
        // 64 GiB per body, 1 TiB of spool files across the server.
        if max_upload_size > 68_719_476_736 || (max_upload_size as usize) < max_body_size {
            return Err(PyValueError::new_err(
                "max_upload_size must be 0 or between max_body_size and 68719476736",
            ));
        }
        if !(max_upload_size..=1_099_511_627_776).contains(&max_upload_bytes_in_flight) {
            return Err(PyValueError::new_err(
                "max_upload_bytes_in_flight must be between max_upload_size and 1099511627776",
            ));
        }
        let directory = match upload_dir {
            Some(path) => std::path::PathBuf::from(path),
            None => std::env::temp_dir(),
        };
        if !directory.is_dir() {
            return Err(PyValueError::new_err(format!(
                "upload_dir {} is not a directory",
                directory.display()
            )));
        }
        Some(uploads::Settings {
            max_upload_size,
            directory,
            budget: budget::ByteBudget::new(max_upload_bytes_in_flight as usize),
        })
    };
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
    if listener_fd.is_some_and(|fd| fd < 0)
        || ready_fd.is_some_and(|fd| fd < 0)
        || shutdown_fd.is_some_and(|fd| fd < 0)
    {
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
    let client_required = match tls_client_auth {
        "required" => true,
        "optional" => false,
        _ => {
            return Err(PyValueError::new_err(
                "tls_client_auth must be 'required' or 'optional'",
            ));
        }
    };
    if tls_client_crl.is_some() && tls_client_ca.is_none() {
        return Err(PyValueError::new_err("tls_client_crl needs tls_client_ca"));
    }
    let websocket_compression = match websocket_compression {
        "none" => false,
        "deflate" => true,
        _ => {
            return Err(PyValueError::new_err(
                "websocket_compression must be 'none' or 'deflate'",
            ));
        }
    };
    let tls = match (tls_cert, tls_key) {
        (None, None) => {
            if tls_client_ca.is_some() || tls_ocsp_response.is_some() {
                return Err(PyValueError::new_err(
                    "tls_client_ca and tls_ocsp_response need tls_cert and tls_key",
                ));
            }
            None
        }
        (Some(cert), Some(key)) => Some(
            tls::Reloadable::new(tls::Settings {
                cert,
                key,
                minimum: tls_min.to_owned(),
                http2,
                client_ca: tls_client_ca,
                client_crl: tls_client_crl,
                ocsp: tls_ocsp_response,
                client_required,
                reload_interval: Duration::from_secs_f64(tls_reload_interval),
            })
            .map_err(PyValueError::new_err)?,
        ),
        _ => {
            return Err(PyValueError::new_err(
                "tls_cert and tls_key must be given together",
            ));
        }
    };
    let request_timeout = duration(request_timeout, "request_timeout")?;
    let graceful_timeout = duration(graceful_timeout, "graceful_timeout")?;
    let header_timeout = duration(header_timeout, "header_timeout")?;
    // A worker running several event loops passes one Bridge per loop.
    let bridges: Option<Vec<Bound<'_, bridge::Bridge>>> = match dispatch
        .bind(py)
        .cast::<pyo3::types::PyList>()
    {
        Ok(list) => Some(
            list.iter()
                .map(|item| item.cast_into::<bridge::Bridge>().map_err(PyErr::from))
                .collect::<PyResult<_>>()
                .map_err(|_| PyTypeError::new_err("dispatch lists may hold only native Bridges"))?,
        ),
        Err(_) => match dispatch.bind(py).cast::<bridge::Bridge>() {
            Ok(bridge) => Some(vec![bridge.clone()]),
            Err(_) if dispatch.bind(py).is_callable() => None,
            Err(_) => {
                return Err(PyTypeError::new_err(
                    "dispatch must be callable, a native Bridge or a list of Bridges",
                ));
            }
        },
    };
    if bridges.as_ref().is_some_and(Vec::is_empty) {
        return Err(PyValueError::new_err("dispatch needs at least one Bridge"));
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
    let (native, dispatch) = match bridges {
        // Requests use the queues. Paths that hold a Python future (WebSocket
        // sessions) use the first bridge's submit, which runs on the loop that
        // owns the application lifespan.
        Some(bridges) => {
            let submit = match &bridges[0].get().submit {
                Some(submit) => submit.clone_ref(py),
                None => dispatch.clone_ref(py),
            };
            let lanes = bridges
                .iter()
                .map(|bridge| bridge.get().shared.clone())
                .collect();
            (bridge::Lanes::new(lanes), submit)
        }
        None => (bridge::Lanes::new(Vec::new()), dispatch),
    };
    let state = Arc::new(ServerState {
        dispatch,
        native,
        capacity: Arc::new(Semaphore::new(max_in_flight)),
        max_body_size,
        max_response_size,
        request_bytes: budget::ByteBudget::new(max_request_bytes_in_flight),
        response_bytes: budget::ByteBudget::new(max_response_bytes_in_flight),
        request_timeout,
        websocket_compression,
        uploads,
    });
    let tracer = controls.tracer();
    let app =
        Arc::new(router::Dispatcher::new(routes, state, controls).map_err(PyValueError::new_err)?);
    let limits = transport::Limits {
        max_connections,
        header_timeout,
        max_headers,
        max_header_size,
        max_request_target_size,
        graceful_timeout,
        max_requests,
        http2,
    };
    py.detach(move || -> std::io::Result<()> {
        let server = async move {
            let listener = if let Some(fd) = listener_fd {
                tokio::net::TcpListener::from_std(bessemer_ffi::inherited_listener(fd)?)?
            } else {
                tokio::net::TcpListener::bind((host.as_str(), port)).await?
            };
            // An embedded server (the test client) is stopped through its
            // shutdown socket and leaves the process's signals alone.
            let stop = match shutdown_fd {
                Some(fd) => transport::Stop::Socket(bessemer_ffi::inherited_stream(fd)?),
                None => {
                    diagnostic!("Bessemer listening on {}", listener.local_addr()?);
                    transport::Stop::Signals
                }
            };
            transport::run(
                listener,
                transport::Application::Native(app),
                limits,
                ready_fd,
                stop,
                tls,
            )
            .await
        };
        #[cfg(target_os = "linux")]
        let served = match embedded {
            Some((handle, core)) => {
                // The loop's thread drives the server; this thread only waits
                // for it to finish, or for the loop to close first.
                let (sender, receiver) = std::sync::mpsc::channel();
                handle.spawn(async move {
                    let _ = sender.send(server.await);
                });
                loop {
                    match receiver.recv_timeout(std::time::Duration::from_millis(100)) {
                        Ok(served) => break served,
                        Err(std::sync::mpsc::RecvTimeoutError::Timeout)
                            if !core.get().is_closed() =>
                        {
                            continue;
                        }
                        Err(_) => {
                            break Err(std::io::Error::other(
                                "the event loop closed before the server stopped",
                            ));
                        }
                    }
                }
            }
            None => multi_thread_runtime()?.block_on(server),
        };
        #[cfg(not(target_os = "linux"))]
        let served = multi_thread_runtime()?.block_on(server);
        // Send the final spans of a normal exit within the shutdown budget.
        if let Some(tracer) = tracer {
            tracer.flush(graceful_timeout.min(std::time::Duration::from_secs(10)));
        }
        served
    })
    .map_err(PyErr::from)
}

/// The server's own runtime: two I/O threads beside the event loop's thread.
fn multi_thread_runtime() -> std::io::Result<tokio::runtime::Runtime> {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .on_thread_start(bessemer_ffi::python_thread::pin_python_thread_state)
        .on_thread_stop(bessemer_ffi::python_thread::unpin_python_thread_state)
        .enable_all()
        .build()
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(serve, module)?)?;
    module.add_function(wrap_pyfunction!(_set_parent_death_signal, module)?)?;
    module.add_function(wrap_pyfunction!(_parse_json, module)?)?;
    module.add_function(wrap_pyfunction!(_encode_plain_json, module)?)?;
    module.add_function(wrap_pyfunction!(_encode_model_json, module)?)?;
    module.add_function(wrap_pyfunction!(_build_model, module)?)?;
    module.add_function(wrap_pyfunction!(_parse_model, module)?)?;
    module.add_class::<ModelPlan>()?;
    module.add_class::<bridge::Bridge>()?;
    module.add_class::<bridge::Ticket>()?;
    module.add_class::<invoker::Invoker>()?;
    module.add_class::<invoker::Invocation>()?;
    module.add_class::<invoker::Call>()?;
    // The Rust asyncio event loop's classes; `bessemer._loop` builds the loop.
    #[cfg(target_os = "linux")]
    bessemer_loop::register(module)?;
    Ok(())
}

/// Encode a handler result made only of exact built-in JSON values.
///
/// Returns None when the value needs the general normalizing path, so callers
/// keep one behaviour for subclasses, conversions and limit errors.
#[pyfunction]
fn _encode_plain_json<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
    max_bytes: usize,
) -> PyResult<Option<Bound<'py, PyBytes>>> {
    Ok(bessemer_codec::encode_plain(value, max_bytes, |body| {
        PyBytes::new(py, body)
    }))
}

/// A compiled plain Pydantic model; see `bessemer._model_plan`.
#[pyclass(frozen, module = "bessemer._native")]
pub(crate) struct ModelPlan(pub(crate) Arc<bessemer_codec::ModelPlan>);

#[pymethods]
impl ModelPlan {
    #[new]
    fn new(spec: &Bound<'_, PyAny>) -> PyResult<Self> {
        bessemer_codec::ModelPlan::from_spec(spec).map(Self)
    }

    /// The model class this plan describes.
    #[getter]
    fn model<'py>(&self, py: Python<'py>) -> Bound<'py, pyo3::types::PyType> {
        self.0.model_class(py).clone()
    }
}

/// Encode an instance of exactly the plan's model as JSON, or return None
/// when it needs Pydantic's serializer.
#[pyfunction]
fn _encode_model_json<'py>(
    py: Python<'py>,
    plan: &ModelPlan,
    value: &Bound<'py, PyAny>,
    max_bytes: usize,
) -> Option<Bound<'py, PyBytes>> {
    bessemer_codec::encode_model(value, &plan.0, max_bytes, |body| PyBytes::new(py, body))
}

/// Build a model from JSON bytes the way the request path does, or return
/// None when Pydantic must validate it. For tests.
#[pyfunction]
fn _build_model<'py>(
    py: Python<'py>,
    plan: &ModelPlan,
    data: &Bound<'py, PyBytes>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let value = bessemer_codec::parse_value(data.as_bytes())
        .map_err(|_| PyValueError::new_err("Invalid JSON"))?;
    Ok(bessemer_codec::build_model(py, &plan.0, &value))
}

/// Parse JSON bytes straight into a model, as the request path first tries
/// to, or return None when it falls back to [`_build_model`]'s route. Raises
/// `ValueError` for input the request path refuses as invalid JSON. For tests.
#[pyfunction]
fn _parse_model<'py>(
    py: Python<'py>,
    plan: &ModelPlan,
    data: &Bound<'py, PyBytes>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let data = data.as_bytes();
    bessemer_codec::validate_json(data).map_err(|_| PyValueError::new_err("Invalid JSON"))?;
    Ok(bessemer_codec::parse_model(py, &plan.0, data))
}

#[pyfunction]
fn _set_parent_death_signal(expected_parent_pid: u32) -> PyResult<()> {
    bessemer_ffi::set_parent_death_signal(expected_parent_pid).map_err(PyErr::from)
}

#[pyfunction]
fn _parse_json<'py>(py: Python<'py>, data: &Bound<'py, PyBytes>) -> PyResult<Bound<'py, PyAny>> {
    bessemer_codec::parse_python(py, data.as_bytes())
        .map_err(|_| PyValueError::new_err("Invalid JSON request body"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parameter(source: Source, kind: Kind) -> Parameter {
        Parameter {
            name: "p".into(),
            alias: "p".into(),
            header: None,
            source,
            kind,
            required: false,
            default: Value::Null,
            many: false,
        }
    }

    #[test]
    fn route_plans_summarise_the_parameters_once() {
        let plain = RoutePlan::new(&[parameter(Source::Query, Kind::Int)], false);
        assert!(!plain.reads_cookies && !plain.has_files && !plain.reads_form);
        assert_eq!(plain.body_count, 0);
        // The full request reads cookies even without a cookie parameter.
        assert!(RoutePlan::new(&[], true).reads_cookies);
        let mixed = RoutePlan::new(
            &[
                parameter(Source::Cookie, Kind::Str),
                parameter(Source::File, Kind::Upload),
                parameter(Source::Body, Kind::Json),
                parameter(Source::Body, Kind::Int),
            ],
            false,
        );
        assert!(mixed.reads_cookies && mixed.has_files && mixed.reads_form);
        assert_eq!(mixed.body_count, 2);
        assert!(RoutePlan::new(&[parameter(Source::Form, Kind::Str)], false).reads_form);
    }

    #[test]
    fn sources_and_kinds_round_trip_and_report_fastapi_locations() {
        for (name, source, location) in [
            ("path", Source::Path, "path"),
            ("query", Source::Query, "query"),
            ("header", Source::Header, "header"),
            ("cookie", Source::Cookie, "cookie"),
            ("form", Source::Form, "body"),
            ("body", Source::Body, "body"),
            ("file", Source::File, "body"),
        ] {
            assert_eq!(Source::parse(name), Some(source));
            assert_eq!(source.location(), location);
        }
        assert_eq!(Source::parse("other"), None);
        for name in ["str", "int", "float", "bool", "json", "upload", "bytes"] {
            assert_eq!(Kind::parse(name).map(Kind::as_str), Some(name));
        }
        assert_eq!(Kind::parse("list"), None);
    }

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

    #[test]
    fn pinned_native_threads_keep_one_python_thread_state() {
        // CPython thread-local data lives in the thread state, so it survives
        // between GIL acquisitions only when the state is not recreated.
        let local = Python::attach(|py| {
            py.import("threading")
                .unwrap()
                .call_method0("local")
                .unwrap()
                .unbind()
        });
        let retained = |pin: bool| {
            let local = Python::attach(|py| local.clone_ref(py));
            std::thread::spawn(move || {
                if pin {
                    bessemer_ffi::python_thread::pin_python_thread_state();
                }
                Python::attach(|py| local.bind(py).setattr("value", 1).unwrap());
                let retained = Python::attach(|py| local.bind(py).hasattr("value").unwrap());
                bessemer_ffi::python_thread::unpin_python_thread_state();
                retained
            })
            .join()
            .unwrap()
        };
        Python::attach(|py| {
            py.detach(|| {
                assert!(!retained(false));
                assert!(retained(true));
            })
        });
    }

    #[test]
    fn encoded_plain_json_keeps_the_json_content_type_and_limits() {
        Python::attach(|py| {
            let budget = budget::ByteBudget::new(64);
            let envelope = |body: &[u8]| {
                PyTuple::new(
                    py,
                    [
                        200u16.into_pyobject(py).unwrap().into_any(),
                        PyBytes::new(py, body).into_any(),
                        PyTuple::empty(py).into_any(),
                        "json_bytes".into_pyobject(py).unwrap().into_any(),
                    ],
                )
                .unwrap()
            };
            let response = decode_response(
                envelope(b"{\"a\":1}").as_any(),
                16,
                &budget,
                false,
                &mut None,
            )
            .unwrap();
            assert_eq!(
                response.headers().get(header::CONTENT_TYPE).unwrap(),
                "application/json"
            );
            assert!(matches!(
                decode_response(
                    envelope(&[b'1'; 17]).as_any(),
                    16,
                    &budget,
                    false,
                    &mut None
                ),
                Err(ResponseError::Invalid)
            ));
            let plain = py
                .eval(c"{'b': [1, 2.5, None], 'a': True}", None, None)
                .unwrap();
            let encoded = _encode_plain_json(py, &plain, 64).unwrap().unwrap();
            assert_eq!(encoded.as_bytes(), br#"{"a":true,"b":[1,2.5,null]}"#);
            assert!(_encode_plain_json(py, &plain, 8).unwrap().is_none());
            let tuple = py.eval(c"(1, 2)", None, None).unwrap();
            assert!(_encode_plain_json(py, &tuple, 64).unwrap().is_none());
        });
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
                    native: bridge::Lanes::new(Vec::new()),
                    capacity: capacity.clone(),
                    max_body_size: 8,
                    max_response_size: 64,
                    request_bytes: request_bytes.clone(),
                    response_bytes: response_bytes.clone(),
                    request_timeout: Duration::from_secs(1),
                    websocket_compression: false,
                    uploads: None,
                };
                dispatch(
                    &state,
                    0,
                    &bridge::Arguments::default(),
                    Vec::new(),
                    Admission {
                        _request: permit,
                        _input: input_permit,
                        _spool: budget::BytePermit::default(),
                        _lane: bridge::LaneLoad::default(),
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
            _spool: budget::BytePermit::default(),
            _lane: bridge::LaneLoad::default(),
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
                native: bridge::Lanes::new(Vec::new()),
                capacity: capacity.clone(),
                max_body_size: 8,
                max_response_size: 64,
                request_bytes: request_bytes.clone(),
                response_bytes: budget::ByteBudget::new(64),
                request_timeout: Duration::from_secs(1),
                websocket_compression: false,
                uploads: None,
            };
            let result = dispatch(
                &state,
                0,
                &bridge::Arguments::default(),
                Vec::new(),
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
            _spool: budget::BytePermit::default(),
            _lane: bridge::LaneLoad::default(),
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
                native: bridge::Lanes::new(Vec::new()),
                capacity: capacity.clone(),
                max_body_size: 8,
                max_response_size: 64,
                request_bytes: request_bytes.clone(),
                response_bytes: response_bytes.clone(),
                request_timeout: Duration::from_secs(1),
                websocket_compression: false,
                uploads: None,
            };
            dispatch(
                &state,
                0,
                &bridge::Arguments::default(),
                Vec::new(),
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
