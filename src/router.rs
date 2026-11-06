//! Native request routing.
//!
//! This replaces the axum `Router` and its tower layers on the request path. One
//! `matchit` lookup finds the route. The native controls run around it, and the
//! handler runs without boxed futures or a per-request service clone. The
//! observable behaviour matches what the axum router did for Bessemer's routes:
//! - paths are matched with the same `matchit` syntax and percent-decoded the
//!   same way;
//! - `GET` routes also answer `HEAD`;
//! - 404 and 405 come from the same fallbacks, and 405 carries an `Allow`
//!   header;
//! - the matched template labels telemetry;
//! - Starlette's `int`, `float` and `uuid` path converters are checked right
//!   after the lookup, and a value that does not match is a 404; a `path`
//!   converter is a catch-all that also matches an empty remainder.
//! - `Content-Length` is set from an exact body size, and `HEAD` bodies are
//!   removed last.

use crate::{RequestInfo, Route, ServerState, controls, error};
use axum::{
    body::{Body, HttpBody},
    extract::{ConnectInfo, Request},
    http::{HeaderValue, Method, StatusCode, header},
    response::Response,
};
use std::{collections::HashMap, net::SocketAddr, sync::Arc, time::Instant};

/// Path parameters, percent-decoded, or `Err` when a value is not UTF-8.
pub(crate) type PathParams = Result<HashMap<String, String>, ()>;

/// A Starlette path converter other than `str`, which needs nothing.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Converter {
    Int,
    Float,
    Uuid,
    /// The catch-all `{*name}` that `{name:path}` becomes.
    Path,
}

impl Converter {
    pub(crate) fn parse(name: &str) -> Option<Self> {
        match name {
            "int" => Some(Self::Int),
            "float" => Some(Self::Float),
            "uuid" => Some(Self::Uuid),
            "path" => Some(Self::Path),
            _ => None,
        }
    }

    /// Whether a decoded value matches Starlette's pattern for the converter.
    fn matches(self, value: &str) -> bool {
        let digits = |text: &str| !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit());
        match self {
            // `[0-9]+`
            Self::Int => digits(value),
            // `[0-9]+(\.[0-9]+)?`
            Self::Float => match value.split_once('.') {
                Some((whole, fraction)) => digits(whole) && digits(fraction),
                None => digits(value),
            },
            // Lower-case hyphenated hex, 8-4-4-4-12.
            Self::Uuid => {
                value.len() == 36
                    && value.bytes().enumerate().all(|(index, byte)| {
                        if matches!(index, 8 | 13 | 18 | 23) {
                            byte == b'-'
                        } else {
                            matches!(byte, b'0'..=b'9' | b'a'..=b'f')
                        }
                    })
            }
            Self::Path => true,
        }
    }
}

/// A `matchit` value: the path entry, and whether this is the extra path a
/// `{name:path}` route registers for an empty remainder (`/files/`).
#[derive(Clone, Copy)]
struct Target {
    entry: usize,
    empty_tail: bool,
}

struct PathEntry {
    template: Box<str>,
    /// Converters to check after a match; empty for most routes.
    checks: Box<[(Box<str>, Converter)]>,
    /// The `{name:path}` parameter's name, when the path ends in one.
    tail: Option<Box<str>>,
    /// Routes in registration order. Each path has at most one per method.
    routes: Vec<Route>,
    /// The `Allow` value axum built for this path, used on 405 responses.
    allow: HeaderValue,
}

/// Compiled routes: one `matchit` entry per path, with its methods.
struct Table {
    paths: matchit::Router<Target>,
    entries: Vec<PathEntry>,
}

pub(crate) struct Dispatcher {
    table: Table,
    state: Arc<ServerState>,
    controls: Arc<controls::Controls>,
}

enum Selected<'a> {
    Route(&'a Route),
    MethodNotAllowed,
    NotFound,
}

/// The methods one route adds to `Allow`. `GET` routes also answer `HEAD`.
pub(crate) fn allowed(method: &Method) -> &'static [&'static str] {
    match *method {
        Method::GET => &["GET", "HEAD"],
        Method::HEAD => &["HEAD"],
        Method::POST => &["POST"],
        Method::PUT => &["PUT"],
        Method::PATCH => &["PATCH"],
        Method::DELETE => &["DELETE"],
        Method::OPTIONS => &["OPTIONS"],
        Method::TRACE => &["TRACE"],
        Method::CONNECT => &["CONNECT"],
        _ => &[],
    }
}

fn validate_path(path: &str) -> Result<(), String> {
    if !path.starts_with('/') {
        return Err(format!("route path must start with `/`: {path}"));
    }
    // Pre-0.8 axum capture syntax is rejected rather than matched literally.
    if path
        .split('/')
        .any(|segment| segment.starts_with(':') || segment.starts_with('*'))
    {
        return Err(format!("invalid route path: {path}"));
    }
    Ok(())
}

impl Table {
    fn new(routes: Vec<Route>) -> Result<Self, String> {
        let mut paths = matchit::Router::new();
        let mut entries: Vec<PathEntry> = Vec::new();
        let mut by_template: HashMap<String, usize> = HashMap::new();
        for route in routes {
            validate_path(&route.path)?;
            if allowed(&route.method).is_empty() {
                return Err(format!("unsupported route method: {}", route.method));
            }
            // A route's own methods are deduplicated; merging another route
            // into the same path appends its list, as axum did.
            let mut own = String::new();
            for method in allowed(&route.method) {
                if !own.contains(method) {
                    if !own.is_empty() {
                        own.push(',');
                    }
                    own.push_str(method);
                }
            }
            if let Some(&index) = by_template.get(&route.path) {
                let entry = &mut entries[index];
                // Python rejects this first; the check keeps one set of
                // converters per path.
                if entry.routes[0].converters != route.converters {
                    return Err(format!(
                        "routes at {} declare different path converters",
                        route.path
                    ));
                }
                if entry
                    .routes
                    .iter()
                    .any(|existing| existing.method == route.method)
                {
                    return Err(format!(
                        "invalid or conflicting route: {} {}",
                        route.method, route.path
                    ));
                }
                let mut allow = entry.allow.to_str().unwrap_or("").to_owned();
                allow.push(',');
                allow.push_str(&own);
                entry.allow = HeaderValue::from_str(&allow).map_err(|e| e.to_string())?;
                entry.routes.push(route);
                continue;
            }
            let index = entries.len();
            paths
                .insert(
                    route.path.clone(),
                    Target {
                        entry: index,
                        empty_tail: false,
                    },
                )
                .map_err(|_| format!("invalid or conflicting route: {}", route.path))?;
            by_template.insert(route.path.clone(), index);
            let checks = route
                .converters
                .iter()
                .filter(|(_, converter)| *converter != Converter::Path)
                .map(|(name, converter)| (name.clone().into_boxed_str(), *converter))
                .collect();
            let tail = route
                .converters
                .iter()
                .find(|(_, converter)| *converter == Converter::Path)
                .map(|(name, _)| name.clone().into_boxed_str());
            entries.push(PathEntry {
                template: route.path.clone().into_boxed_str(),
                checks,
                tail,
                allow: HeaderValue::from_str(&own).map_err(|e| e.to_string())?,
                routes: vec![route],
            });
        }
        // Starlette's `{name:path}` matches an empty remainder, which a
        // `matchit` catch-all does not: `/files/{*rest}` also registers
        // `/files/`. An explicit route at that path keeps it.
        for (index, entry) in entries.iter().enumerate() {
            let Some(name) = &entry.tail else { continue };
            let Some(bare) = entry.template.strip_suffix(&format!("{{*{name}}}")) else {
                return Err(format!("invalid route path: {}", entry.template));
            };
            if by_template.contains_key(bare) {
                continue;
            }
            paths
                .insert(
                    bare,
                    Target {
                        entry: index,
                        empty_tail: true,
                    },
                )
                .map_err(|_| format!("invalid or conflicting route: {}", entry.template))?;
        }
        Ok(Self { paths, entries })
    }

    /// The path entry and its decoded parameters, when the path matches.
    fn lookup(&self, path: &str) -> Option<(&PathEntry, PathParams)> {
        let matched = self.paths.at(path).ok()?;
        let target = *matched.value;
        let entry = &self.entries[target.entry];
        let mut params = if matched.params.is_empty() {
            Ok(HashMap::new())
        } else {
            decode_params(&matched.params)
        };
        if target.empty_tail
            && let (Ok(values), Some(name)) = (&mut params, &entry.tail)
        {
            values.insert(name.to_string(), String::new());
        }
        // A converter that does not match means the route does not match,
        // as in Starlette. Invalid UTF-8 is left to the handler's 400.
        if !entry.checks.is_empty()
            && let Ok(values) = &params
            && !entry.checks.iter().all(|(name, converter)| {
                values
                    .get(&**name)
                    .is_some_and(|value| converter.matches(value))
            })
        {
            return None;
        }
        Some((entry, params))
    }
}

impl Dispatcher {
    pub(crate) fn new(
        routes: Vec<Route>,
        state: Arc<ServerState>,
        controls: Arc<controls::Controls>,
    ) -> Result<Self, String> {
        Ok(Self {
            table: Table::new(routes)?,
            state,
            controls,
        })
    }

    /// Serve one request. `direct` is given by the HTTP/1 writer, which then
    /// writes the headers it collects itself (see `controls::DirectHeaders`).
    pub(crate) async fn call(
        &self,
        mut request: Request,
        peer: Option<SocketAddr>,
        direct: Option<&mut controls::DirectHeaders>,
    ) -> Response {
        let received = direct
            .as_deref()
            .and_then(|direct| direct.received)
            .unwrap_or_else(Instant::now);
        let method = request.method().clone();
        let (entry, params) = match self.table.lookup(request.uri().path()) {
            Some((entry, params)) => (Some(entry), params),
            None => (None, Ok(HashMap::new())),
        };
        let selected = match entry {
            None => Selected::NotFound,
            Some(entry) => select(&entry.routes, &method),
        };
        let template = entry.map_or("<unmatched>", |entry| &*entry.template);
        let coding = self
            .controls
            .compression()
            .and_then(|compression| compression.negotiate(&method, request.headers()));
        let peer = peer.or_else(|| {
            request
                .extensions()
                .get::<ConnectInfo<SocketAddr>>()
                .map(|value| value.0)
        });
        let controls::Begin { context, response } = self
            .controls
            .begin(
                request.method(),
                request.uri(),
                request.headers(),
                peer,
                received,
            )
            .await;
        if let Some(request_id) = context.request_id_header() {
            request
                .headers_mut()
                .insert(controls::X_REQUEST_ID, request_id.clone());
        }
        let secure = request.extensions().get::<crate::transport::Secure>();
        let info = RequestInfo {
            request_id: context.request_id_header().cloned(),
            client: context.client_ip,
            claims: context.claims.clone(),
            secure: secure.is_some(),
            client_certificate: secure.and_then(|secure| secure.client_certificate.clone()),
            trace: context.trace_parent(),
            received,
        };
        let mut response = match response {
            Some(response) => *response,
            None => match selected {
                Selected::Route(route) => {
                    if route.websocket && !self.controls.websocket_origin_allowed(request.headers())
                    {
                        error(StatusCode::FORBIDDEN, "WebSocket origin is not allowed")
                    } else {
                        Box::pin(crate::handle(
                            &self.state,
                            route,
                            params,
                            info,
                            request,
                            coding.is_some(),
                        ))
                        .await
                    }
                }
                Selected::MethodNotAllowed => {
                    error(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed")
                }
                Selected::NotFound => error(StatusCode::NOT_FOUND, "Not Found"),
            },
        };
        // The rare asynchronous steps are boxed so their futures do not
        // enlarge (and get copied with) every request's future.
        if controls::Controls::needs_resolution(&response) {
            response = Box::pin(self.controls.resolve_response(response)).await;
        }
        if let Some(compression) = self.controls.compression() {
            response = Box::pin(compression.apply(coding, response)).await;
        }
        self.controls
            .finish(&context, &mut response, template, direct);
        // The path matched but the method did not: every response from that
        // fallback carries the path's methods unless it already names some.
        if let (Selected::MethodNotAllowed, Some(entry)) = (&selected, entry)
            && !response.headers().contains_key(header::ALLOW)
        {
            response
                .headers_mut()
                .insert(header::ALLOW, entry.allow.clone());
        }
        finish(&method, &mut response);
        response
    }
}

fn select<'a>(routes: &'a [Route], method: &Method) -> Selected<'a> {
    if let Some(route) = routes.iter().find(|route| route.method == *method) {
        return Selected::Route(route);
    }
    if *method == Method::HEAD
        && let Some(route) = routes.iter().find(|route| route.method == Method::GET)
    {
        return Selected::Route(route);
    }
    Selected::MethodNotAllowed
}

fn decode_params(params: &matchit::Params<'_, '_>) -> PathParams {
    let mut decoded = HashMap::with_capacity(params.len());
    for (name, value) in params.iter() {
        let value = percent_encoding::percent_decode_str(value)
            .decode_utf8()
            .map_err(|_| ())?;
        decoded.insert(name.to_owned(), value.into_owned());
    }
    Ok(decoded)
}

/// Drop the body of a `HEAD` response, first setting `Content-Length` from
/// its exact size so the length still describes the `GET` representation.
/// Other responses keep their size hint, from which both servers write the
/// length, so the header is not built here.
fn finish(method: &Method, response: &mut Response) {
    if *method != Method::HEAD {
        return;
    }
    if !response.headers().contains_key(header::CONTENT_LENGTH)
        && let Some(size) = response.body().size_hint().exact()
    {
        let value = if size == 0 {
            HeaderValue::from_static("0")
        } else {
            HeaderValue::from(size)
        };
        response.headers_mut().insert(header::CONTENT_LENGTH, value);
    }
    *response.body_mut() = Body::empty();
}

#[cfg(test)]
mod tests {
    use super::*;

    fn route(id: usize, method: Method, path: &str) -> Route {
        Route {
            id,
            path: path.to_owned(),
            method,
            health: false,
            static_body: None,
            files: None,
            needs_request: false,
            needs_principal: false,
            websocket: false,
            status: 200,
            timeout: None,
            parameters: Vec::new(),
            converters: Vec::new(),
            plan: Default::default(),
        }
    }

    fn selected_id(table: &Table, method: Method, path: &str) -> Option<usize> {
        let (entry, _) = table.lookup(path)?;
        match select(&entry.routes, &method) {
            Selected::Route(route) => Some(route.id),
            _ => None,
        }
    }

    #[test]
    fn methods_select_routes_and_get_answers_head() {
        let table = Table::new(vec![
            route(0, Method::GET, "/items"),
            route(1, Method::POST, "/items"),
            route(2, Method::HEAD, "/only-head"),
        ])
        .unwrap();
        assert_eq!(selected_id(&table, Method::GET, "/items"), Some(0));
        assert_eq!(selected_id(&table, Method::HEAD, "/items"), Some(0));
        assert_eq!(selected_id(&table, Method::POST, "/items"), Some(1));
        assert_eq!(selected_id(&table, Method::PUT, "/items"), None);
        assert_eq!(selected_id(&table, Method::GET, "/only-head"), None);
        assert!(table.lookup("/missing").is_none());
        // Each route adds its methods; a GET route also allows HEAD.
        let (entry, _) = table.lookup("/items").unwrap();
        assert_eq!(entry.allow, "GET,HEAD,POST");
    }

    #[test]
    fn an_explicit_head_route_wins_over_get() {
        let table = Table::new(vec![
            route(0, Method::GET, "/a"),
            route(1, Method::HEAD, "/a"),
        ])
        .unwrap();
        assert_eq!(selected_id(&table, Method::HEAD, "/a"), Some(1));
        assert_eq!(table.lookup("/a").unwrap().0.allow, "GET,HEAD,HEAD");
    }

    #[test]
    fn parameters_are_percent_decoded_and_must_be_utf8() {
        let table = Table::new(vec![
            route(0, Method::GET, "/items/{item_id}"),
            route(1, Method::GET, "/files/{*rest}"),
        ])
        .unwrap();
        let (_, params) = table.lookup("/items/a%20b").unwrap();
        assert_eq!(params.unwrap()["item_id"], "a b");
        let (_, params) = table.lookup("/files/x/y%2Fz").unwrap();
        assert_eq!(params.unwrap()["rest"], "x/y/z");
        let (_, params) = table.lookup("/items/%FF").unwrap();
        assert!(params.is_err());
        assert!(table.lookup("/items/").is_none());
    }

    fn converted(id: usize, method: Method, path: &str, converters: &[(&str, &str)]) -> Route {
        let mut route = route(id, method, path);
        route.converters = converters
            .iter()
            .map(|(name, converter)| ((*name).to_owned(), Converter::parse(converter).unwrap()))
            .collect();
        route
    }

    #[test]
    fn converters_gate_the_match() {
        let table = Table::new(vec![
            converted(0, Method::GET, "/i/{id}", &[("id", "int")]),
            converted(1, Method::GET, "/f/{x}", &[("x", "float")]),
            converted(2, Method::GET, "/u/{u}", &[("u", "uuid")]),
            route(3, Method::GET, "/s/{s}"),
        ])
        .unwrap();
        assert_eq!(selected_id(&table, Method::GET, "/i/42"), Some(0));
        assert!(table.lookup("/i/-1").is_none());
        assert!(table.lookup("/i/4x").is_none());
        assert_eq!(selected_id(&table, Method::GET, "/f/1.5"), Some(1));
        assert_eq!(selected_id(&table, Method::GET, "/f/3"), Some(1));
        assert!(table.lookup("/f/1.").is_none());
        assert!(table.lookup("/f/.5").is_none());
        assert!(table.lookup("/f/1e3").is_none());
        let uuid = "/u/0b5e3c9a-2a1f-4b7e-9c3d-1e2f3a4b5c6d";
        assert_eq!(selected_id(&table, Method::GET, uuid), Some(2));
        assert!(
            table
                .lookup(&uuid.to_uppercase().replace("/U/", "/u/"))
                .is_none()
        );
        assert!(table.lookup("/u/not-a-uuid").is_none());
        assert_eq!(selected_id(&table, Method::GET, "/s/anything"), Some(3));
    }

    #[test]
    fn a_path_converter_matches_slashes_and_an_empty_remainder() {
        let table = Table::new(vec![converted(
            0,
            Method::GET,
            "/files/{*rest}",
            &[("rest", "path")],
        )])
        .unwrap();
        let (_, params) = table.lookup("/files/a/b%20c").unwrap();
        assert_eq!(params.unwrap()["rest"], "a/b c");
        let (_, params) = table.lookup("/files/").unwrap();
        assert_eq!(params.unwrap()["rest"], "");
        assert!(table.lookup("/files").is_none());
        // An explicit route at the bare path keeps it.
        let table = Table::new(vec![
            converted(0, Method::GET, "/files/{*rest}", &[("rest", "path")]),
            route(1, Method::GET, "/files/"),
        ])
        .unwrap();
        assert_eq!(selected_id(&table, Method::GET, "/files/"), Some(1));
        assert_eq!(selected_id(&table, Method::GET, "/files/x"), Some(0));
    }

    #[test]
    fn routes_sharing_a_path_must_share_converters() {
        assert!(
            Table::new(vec![
                converted(0, Method::GET, "/i/{id}", &[("id", "int")]),
                route(1, Method::POST, "/i/{id}"),
            ])
            .is_err()
        );
        assert!(
            Table::new(vec![
                converted(0, Method::GET, "/i/{id}", &[("id", "int")]),
                converted(1, Method::POST, "/i/{id}", &[("id", "int")]),
            ])
            .is_ok()
        );
    }

    #[test]
    fn duplicate_methods_and_invalid_paths_are_rejected() {
        assert!(
            Table::new(vec![
                route(0, Method::GET, "/a"),
                route(1, Method::GET, "/a"),
            ])
            .is_err()
        );
        assert!(
            Table::new(vec![
                route(0, Method::GET, "/{a}"),
                route(1, Method::GET, "/{b}"),
            ])
            .is_err()
        );
        assert!(Table::new(vec![route(0, Method::GET, "a")]).is_err());
        assert!(Table::new(vec![route(0, Method::GET, "/:a")]).is_err());
    }

    #[test]
    fn content_length_is_set_before_a_head_body_is_removed() {
        let mut response = Response::new(Body::from("hello"));
        finish(&Method::HEAD, &mut response);
        assert_eq!(response.headers()[header::CONTENT_LENGTH], "5");
        assert_eq!(response.body().size_hint().exact(), Some(0));
        let mut response = Response::new(Body::empty());
        finish(&Method::HEAD, &mut response);
        assert_eq!(response.headers()[header::CONTENT_LENGTH], "0");
        // Other methods leave the length to the server, which writes it from
        // the body's exact size.
        let mut response = Response::new(Body::from("hello"));
        finish(&Method::GET, &mut response);
        assert!(!response.headers().contains_key(header::CONTENT_LENGTH));
        assert_eq!(response.body().size_hint().exact(), Some(5));
    }
}
