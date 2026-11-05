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
//! - `Content-Length` is set from an exact body size, and `HEAD` bodies are
//!   removed last.

use crate::{RequestInfo, Route, ServerState, controls, error};
use axum::{
    body::{Body, HttpBody},
    extract::{ConnectInfo, Request},
    http::{HeaderValue, Method, StatusCode, header},
    response::Response,
};
use std::{collections::HashMap, net::SocketAddr, sync::Arc};

/// Path parameters, percent-decoded, or `Err` when a value is not UTF-8.
pub(crate) type PathParams = Result<HashMap<String, String>, ()>;

struct PathEntry {
    template: Box<str>,
    /// Routes in registration order. Each path has at most one per method.
    routes: Vec<Route>,
    /// The `Allow` value axum built for this path, used on 405 responses.
    allow: HeaderValue,
}

/// Compiled routes: one `matchit` entry per path, with its methods.
struct Table {
    paths: matchit::Router<usize>,
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
                .insert(route.path.clone(), index)
                .map_err(|_| format!("invalid or conflicting route: {}", route.path))?;
            by_template.insert(route.path.clone(), index);
            entries.push(PathEntry {
                template: route.path.clone().into_boxed_str(),
                allow: HeaderValue::from_str(&own).map_err(|e| e.to_string())?,
                routes: vec![route],
            });
        }
        Ok(Self { paths, entries })
    }

    /// The path entry and its decoded parameters, when the path matches.
    fn lookup(&self, path: &str) -> Option<(&PathEntry, PathParams)> {
        let matched = self.paths.at(path).ok()?;
        let params = if matched.params.is_empty() {
            Ok(HashMap::new())
        } else {
            decode_params(&matched.params)
        };
        Some((&self.entries[*matched.value], params))
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

    pub(crate) async fn call(&self, mut request: Request) -> Response {
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
        let peer = request
            .extensions()
            .get::<ConnectInfo<SocketAddr>>()
            .map(|value| value.0);
        let controls::Begin { context, response } = self
            .controls
            .begin(request.method(), request.uri(), request.headers(), peer)
            .await;
        if let Some(request_id) = context.request_id_header() {
            request
                .headers_mut()
                .insert(controls::X_REQUEST_ID, request_id.clone());
        }
        let secure = request.extensions().get::<crate::transport::Secure>();
        let info = RequestInfo {
            request_id: context.request_id.clone(),
            client: context.client_ip,
            claims: context.claims.clone(),
            secure: secure.is_some(),
            client_certificate: secure.and_then(|secure| secure.client_certificate.clone()),
            trace: context.trace_parent(),
        };
        let mut response = match response {
            Some(response) => response,
            None => match selected {
                Selected::Route(route) => {
                    crate::handle(&self.state, route, params, info, request).await
                }
                Selected::MethodNotAllowed => {
                    error(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed")
                }
                Selected::NotFound => error(StatusCode::NOT_FOUND, "Not Found"),
            },
        };
        response = self.controls.resolve_response(response).await;
        if let Some(compression) = self.controls.compression() {
            response = compression.apply(coding, response).await;
        }
        self.controls.finish(&context, &mut response, template);
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

/// Set `Content-Length` from an exact body size, then drop the body of a
/// `HEAD` response, so the length still describes the `GET` representation.
fn finish(method: &Method, response: &mut Response) {
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
    if *method == Method::HEAD {
        *response.body_mut() = Body::empty();
    }
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
        finish(&Method::GET, &mut response);
        assert_eq!(response.headers()[header::CONTENT_LENGTH], "0");
    }
}
