//! Native request controls. Configuration is frozen before any listener starts.

use crate::telemetry::{AccessLogger, Metrics, RequestTelemetry};
use axum::{
    Json,
    body::{Body, Bytes},
    http::{HeaderMap, HeaderName, HeaderValue, Method, StatusCode, Uri, header},
    response::{IntoResponse, Response},
};
use ipnet::IpNet;
use serde_json::{Map, Value, json};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    net::{IpAddr, SocketAddr},
    sync::{
        Arc, Mutex, OnceLock,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use subtle::ConstantTimeEq;
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

type Config = Map<String, Value>;
static REQUEST_SEQUENCE: AtomicU64 = AtomicU64::new(0);
pub(crate) const X_REQUEST_ID: HeaderName = HeaderName::from_static("x-request-id");
const X_CONTENT_TYPE_OPTIONS: HeaderName = HeaderName::from_static("x-content-type-options");

struct ScrapePermit {
    _permit: OwnedSemaphorePermit,
}

#[derive(Clone)]
struct DeferredScrape(Arc<ScrapePermit>);

struct ScrapePayload {
    text: String,
    _permit: Arc<ScrapePermit>,
}

impl AsRef<[u8]> for ScrapePayload {
    fn as_ref(&self) -> &[u8] {
        self.text.as_bytes()
    }
}

struct Cors {
    origins: HashSet<String>,
    wildcard: bool,
    methods: HashSet<String>,
    headers: HashSet<String>,
    wildcard_headers: bool,
    expose: String,
    credentials: bool,
    max_age: u64,
}

struct ApiKey {
    header: HeaderName,
    secret: Vec<u8>,
    exclude_paths: HashSet<String>,
}

enum AllowedHost {
    Exact(String),
    Subdomains(String),
}

impl AllowedHost {
    fn parse(value: &str) -> Result<Self, String> {
        let wildcard = value.starts_with("*.");
        let host = value.strip_prefix("*.").unwrap_or(value);
        let literal = host
            .strip_prefix('[')
            .and_then(|host| host.strip_suffix(']'))
            .unwrap_or(host);
        if let Ok(address) = literal.parse::<IpAddr>() {
            if host.starts_with('[') && !address.is_ipv6() {
                return Err("allowed_hosts brackets require an IPv6 address".into());
            }
            return if wildcard {
                Err("allowed_hosts wildcards require a DNS suffix".into())
            } else {
                Ok(Self::Exact(address.to_string()))
            };
        }
        let host = host.strip_suffix('.').unwrap_or(host);
        if host.is_empty()
            || host.len() > 253
            || host.split('.').any(|label| {
                label.is_empty()
                    || label.len() > 63
                    || label.starts_with('-')
                    || label.ends_with('-')
                    || !label
                        .bytes()
                        .all(|value| value.is_ascii_alphanumeric() || value == b'-')
            })
        {
            return Err(
                "allowed_hosts must contain DNS names, IPs or *.DNS suffixes without ports".into(),
            );
        }
        Ok(if wildcard {
            Self::Subdomains(format!(".{}", host.to_ascii_lowercase()))
        } else {
            Self::Exact(host.to_ascii_lowercase())
        })
    }

    fn matches(&self, host: &str) -> bool {
        match self {
            Self::Exact(value) => host == value,
            Self::Subdomains(suffix) => host.len() > suffix.len() && host.ends_with(suffix),
        }
    }
}

struct RateEntry {
    requests: VecDeque<Instant>,
}
struct RateState {
    entries: HashMap<IpAddr, RateEntry>,
    last_prune: Instant,
}
struct RateLimit {
    requests: usize,
    window: Duration,
    max_keys: usize,
    exclude_paths: HashSet<String>,
    state: Mutex<RateState>,
    /// Decisions shared through Redis instead of the local table.
    shared: Option<crate::redis::SharedLimiter>,
}

pub struct Controls {
    cors: Option<Cors>,
    security_headers: bool,
    request_id: bool,
    trusted_proxies: Vec<IpNet>,
    allowed_hosts: Option<Vec<AllowedHost>>,
    api_key: Option<ApiKey>,
    jwt: Option<crate::jwt::JwtAuth>,
    rate_limit: Option<RateLimit>,
    metrics_path: Option<String>,
    metrics_secret: Option<Vec<u8>>,
    metrics: Arc<Metrics>,
    scrape_permit: Arc<Semaphore>,
    logger: Option<Arc<AccessLogger>>,
    compression: Option<crate::compression::Compression>,
    tracer: Option<Arc<crate::otel::Tracer>>,
}

/// Headers `Controls::finish` sets with `insert` that the HTTP/1 writer
/// writes straight into its output, so they never enter the response's
/// `HeaderMap`. The writer skips any value the application gave for these
/// names, as `insert` would have replaced it. HTTP/2, and every caller that
/// does not ask for this, gets them in the map.
#[derive(Default)]
pub(crate) struct DirectHeaders {
    pub(crate) request_id: Option<HeaderValue>,
    pub(crate) security: bool,
    /// The HTTP/1 parser already checked the request's Host, Transfer-Encoding
    /// and Content-Type headers (see `transport::protocol_rejection`).
    pub(crate) head_checked: bool,
}

impl DirectHeaders {
    /// Whether a response header of this name is replaced by one of ours.
    pub(crate) fn replaces(&self, name: &HeaderName) -> bool {
        (self.security
            && (*name == X_CONTENT_TYPE_OPTIONS
                || *name == header::X_FRAME_OPTIONS
                || *name == header::REFERRER_POLICY))
            || (self.request_id.is_some() && *name == X_REQUEST_ID)
    }

    /// Append the header lines to an HTTP/1 head.
    pub(crate) fn write(&self, out: &mut Vec<u8>) {
        if let Some(request_id) = &self.request_id {
            out.extend_from_slice(b"x-request-id: ");
            out.extend_from_slice(request_id.as_bytes());
            out.extend_from_slice(b"\r\n");
        }
        if self.security {
            out.extend_from_slice(
                b"x-content-type-options: nosniff\r\nx-frame-options: DENY\r\nreferrer-policy: no-referrer\r\n",
            );
        }
    }
}

pub struct RequestContext {
    /// Verified JWT claims when bearer authentication is configured.
    pub claims: Option<Arc<Value>>,
    request_id_header: Option<HeaderValue>,
    pub client_ip: Option<IpAddr>,
    allowed_origin: Option<HeaderValue>,
    preflight: bool,
    metrics_request: bool,
    telemetry: RequestTelemetry,
}

impl RequestContext {
    pub fn request_id_header(&self) -> Option<&HeaderValue> {
        self.request_id_header.as_ref()
    }

    /// The `traceparent` (and `tracestate`) handlers see as their parent.
    pub fn trace_parent(&self) -> Option<(String, Option<String>)> {
        self.telemetry
            .span()
            .map(|span| (span.context.traceparent(), span.tracestate.clone()))
    }
}

pub struct Begin {
    pub context: RequestContext,
    /// Boxed: a rejection is rare, and inline it would double the size of
    /// every `Begin`, which is moved several times per request.
    pub response: Option<Box<Response>>,
}

fn object<'a>(value: &'a Value, name: &str) -> Result<&'a Config, String> {
    value
        .as_object()
        .ok_or_else(|| format!("{name} must be an object"))
}

fn keys(config: &Config, allowed: &[&str], name: &str) -> Result<(), String> {
    if config.keys().any(|key| !allowed.contains(&key.as_str())) {
        return Err(format!("{name} contains unsupported settings"));
    }
    Ok(())
}

fn boolean(config: &Config, name: &str, default: bool) -> Result<bool, String> {
    match config.get(name) {
        None => Ok(default),
        Some(Value::Bool(value)) => Ok(*value),
        _ => Err(format!("{name} must be a bool")),
    }
}

fn string(config: &Config, name: &str, default: Option<&str>) -> Result<String, String> {
    match config.get(name) {
        None => default
            .map(str::to_owned)
            .ok_or_else(|| format!("{name} is required")),
        Some(Value::String(value)) => Ok(value.clone()),
        _ => Err(format!("{name} must be a string")),
    }
}

fn strings(config: &Config, name: &str, default: &[&str]) -> Result<Vec<String>, String> {
    match config.get(name) {
        None => Ok(default.iter().map(|value| (*value).to_owned()).collect()),
        Some(Value::Array(values)) => values
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .map(str::to_owned)
                    .ok_or_else(|| format!("{name} must contain strings"))
            })
            .collect(),
        _ => Err(format!("{name} must be a list of strings")),
    }
}

fn positive(
    config: &Config,
    name: &str,
    default: Option<u64>,
    maximum: u64,
) -> Result<u64, String> {
    let value = match config.get(name) {
        None => default,
        Some(value) => value.as_u64(),
    };
    value
        .filter(|value| *value > 0 && *value <= maximum)
        .ok_or_else(|| format!("{name} must be an integer between 1 and {maximum}"))
}

pub(crate) fn exact_path(value: &str) -> bool {
    value.is_ascii()
        && value.starts_with('/')
        && !value.starts_with("//")
        && !value.contains(['?', '#', '{', '}', '*', '\\'])
        && !value
            .chars()
            .any(|value| value.is_control() || value == ' ')
}

fn exclusions(config: &Config) -> Result<HashSet<String>, String> {
    let paths = strings(config, "exclude_paths", &[])?;
    if paths.len() > 1024 || paths.iter().any(|path| !exact_path(path)) {
        return Err("exclude_paths must contain at most 1024 exact absolute paths".into());
    }
    Ok(paths.into_iter().collect())
}

fn env_secret(name: &str) -> Result<Vec<u8>, String> {
    let mut characters = name.chars();
    if name.len() > 128
        || !characters
            .next()
            .is_some_and(|value| value == '_' || value.is_ascii_alphabetic())
        || !characters.all(|value| value == '_' || value.is_ascii_alphanumeric())
    {
        return Err("secret environment variable names must be valid identifiers".into());
    }
    let secret = std::env::var(name)
        .map_err(|_| format!("required secret environment variable {name} is unavailable"))?
        .into_bytes();
    if !(16..=4096).contains(&secret.len()) || !secret.iter().all(u8::is_ascii_graphic) {
        return Err(format!(
            "secret environment variable {name} must contain 16 to 4096 visible ASCII characters without spaces"
        ));
    }
    Ok(secret)
}

fn serialized_origin(origin: &str) -> bool {
    if origin == "null" {
        return true;
    }
    let Ok(uri) = origin.parse::<Uri>() else {
        return false;
    };
    matches!(uri.scheme_str(), Some("http" | "https"))
        && uri
            .authority()
            .is_some_and(|authority| valid_authority(authority.as_str()))
        && uri.path_and_query().is_none_or(|path| path.as_str() == "/")
        && !origin.ends_with('/')
        && !origin.chars().any(char::is_control)
}

fn valid_authority(authority: &str) -> bool {
    if authority.is_empty() || authority.contains('@') {
        return false;
    }
    let port = if let Some(ipv6) = authority.strip_prefix('[') {
        let Some((address, suffix)) = ipv6.split_once(']') else {
            return false;
        };
        if address.parse::<std::net::Ipv6Addr>().is_err() {
            return false;
        }
        if suffix.is_empty() {
            return true;
        }
        let Some(port) = suffix.strip_prefix(':') else {
            return false;
        };
        port
    } else {
        let Some((host, port)) = authority.split_once(':') else {
            return true;
        };
        if host.is_empty() {
            return false;
        }
        port
    };
    !port.is_empty()
        && port.bytes().all(|value| value.is_ascii_digit())
        && port.parse::<u16>().is_ok()
}

impl Cors {
    fn parse(value: &Value) -> Result<Self, String> {
        let config = object(value, "cors")?;
        keys(
            config,
            &[
                "origins",
                "allow_methods",
                "allow_headers",
                "expose_headers",
                "allow_credentials",
                "max_age",
            ],
            "cors",
        )?;
        let origins = strings(config, "origins", &[])?;
        if origins.len() > 1024
            || origins
                .iter()
                .any(|origin| origin.len() > 2048 || (origin != "*" && !serialized_origin(origin)))
        {
            return Err("cors origins must be serialized HTTP(S) origins, null or *".into());
        }
        let wildcard = origins.iter().any(|origin| origin == "*");
        let credentials = boolean(config, "allow_credentials", false)?;
        if wildcard && (credentials || origins.len() != 1) {
            return Err("wildcard cors origins must be used alone and without credentials".into());
        }
        let methods = strings(config, "allow_methods", &["GET"])?;
        let valid = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"];
        let methods: HashSet<String> = if methods == ["*"] {
            valid.iter().map(|value| (*value).to_owned()).collect()
        } else {
            if methods.is_empty()
                || methods
                    .iter()
                    .any(|method| !valid.contains(&method.as_str()))
            {
                return Err("cors allow_methods contains an unsupported method".into());
            }
            methods.into_iter().collect()
        };
        let headers = strings(config, "allow_headers", &[])?;
        let wildcard_headers = headers == ["*"];
        if headers.len() > 128
            || (!wildcard_headers
                && headers.iter().any(|name| {
                    name.len() > 128
                        || name == "*"
                        || HeaderName::from_bytes(name.as_bytes()).is_err()
                }))
        {
            return Err(
                "cors allow_headers contains an invalid header name or mixed wildcard".into(),
            );
        }
        let mut headers: HashSet<String> = headers
            .into_iter()
            .map(|header| header.to_ascii_lowercase())
            .collect();
        headers.extend(
            [
                "accept",
                "accept-language",
                "content-language",
                "content-type",
            ]
            .map(str::to_owned),
        );
        let exposed = strings(config, "expose_headers", &[])?;
        if exposed.len() > 32
            || exposed
                .iter()
                .any(|name| name.len() > 128 || HeaderName::from_bytes(name.as_bytes()).is_err())
            || (credentials && exposed.iter().any(|name| name == "*"))
        {
            return Err(
                "cors expose_headers contains an invalid or credentialed wildcard header".into(),
            );
        }
        let max_age = match config.get("max_age") {
            None => 600,
            Some(value) => value
                .as_u64()
                .filter(|value| *value <= 86_400)
                .ok_or("cors max_age must be an integer between 0 and 86400")?,
        };
        Ok(Self {
            origins: origins.into_iter().collect(),
            wildcard,
            methods,
            headers,
            wildcard_headers,
            expose: exposed.join(", "),
            credentials,
            max_age,
        })
    }

    fn origin(&self, origin: &str) -> Option<HeaderValue> {
        if !serialized_origin(origin) || (!self.wildcard && !self.origins.contains(origin)) {
            return None;
        }
        HeaderValue::from_str(if self.wildcard { "*" } else { origin }).ok()
    }

    fn preflight(&self, method: &str, headers: &HeaderMap) -> Response {
        if !self.methods.contains(method) {
            return reject(StatusCode::FORBIDDEN, "CORS method is not allowed");
        }
        let mut requested = Vec::new();
        let mut length = 0;
        for value in headers.get_all(header::ACCESS_CONTROL_REQUEST_HEADERS) {
            let Ok(value) = value.to_str() else {
                return reject(StatusCode::BAD_REQUEST, "Invalid CORS request headers");
            };
            length += value.len();
            if length > 8192 {
                return reject(
                    StatusCode::BAD_REQUEST,
                    "CORS request headers are too large",
                );
            }
            for name in value.split(',').map(str::trim) {
                if name.is_empty() || HeaderName::from_bytes(name.as_bytes()).is_err() {
                    return reject(StatusCode::BAD_REQUEST, "Invalid CORS request headers");
                }
                let name = name.to_ascii_lowercase();
                if !self.wildcard_headers && !self.headers.contains(&name) {
                    return reject(StatusCode::FORBIDDEN, "CORS request header is not allowed");
                }
                requested.push(name);
            }
        }
        requested.sort();
        requested.dedup();
        let mut methods: Vec<_> = self.methods.iter().map(String::as_str).collect();
        methods.sort();
        let mut response = StatusCode::NO_CONTENT.into_response();
        response.headers_mut().insert(
            header::ACCESS_CONTROL_ALLOW_METHODS,
            HeaderValue::from_str(&methods.join(", ")).unwrap(),
        );
        response.headers_mut().insert(
            header::ACCESS_CONTROL_MAX_AGE,
            HeaderValue::from_str(&self.max_age.to_string()).unwrap(),
        );
        if !requested.is_empty() {
            response.headers_mut().insert(
                header::ACCESS_CONTROL_ALLOW_HEADERS,
                HeaderValue::from_str(&requested.join(", ")).unwrap(),
            );
        }
        response
    }
}

fn reject(status: StatusCode, detail: &str) -> Response {
    (status, Json(json!({"detail": detail}))).into_response()
}

fn singleton<'a>(headers: &'a HeaderMap, name: &HeaderName) -> Result<Option<&'a HeaderValue>, ()> {
    let mut values = headers.get_all(name).iter();
    let first = values.next();
    if values.next().is_some() {
        return Err(());
    }
    Ok(first)
}

/// The request's id: the client's `x-request-id` when it is a safe token,
/// otherwise a new one. The client's value is shared, not copied.
fn request_id(headers: &HeaderMap) -> HeaderValue {
    if let Ok(Some(value)) = singleton(headers, &X_REQUEST_ID)
        && !value.is_empty()
        && value.len() <= 128
        && value
            .as_bytes()
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._:-".contains(byte))
    {
        return value.clone();
    }
    // Workers are separate executables, so the start time and process id are
    // fixed per process. Computing them once avoids a clock read, a getpid
    // system call and a formatter pass on every request.
    static PREFIX: OnceLock<String> = OnceLock::new();
    let prefix = PREFIX.get_or_init(|| {
        format!(
            "{:x}-{:x}-",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos(),
            std::process::id()
        )
    });
    let sequence = REQUEST_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let mut id = String::with_capacity(prefix.len() + 16);
    id.push_str(prefix);
    let digits = (64 - (sequence | 1).leading_zeros()).div_ceil(4);
    for shift in (0..digits).rev() {
        id.push(char::from(
            b"0123456789abcdef"[(sequence >> (shift * 4)) as usize & 15],
        ));
    }
    // The bytes move into the header value without a copy.
    HeaderValue::from_maybe_shared(bytes::Bytes::from(id)).expect("request ids are header-safe")
}

fn trusted(ip: IpAddr, proxies: &[IpNet]) -> bool {
    proxies
        .iter()
        .any(|network| network.contains(&ip.to_canonical()))
}

fn client_ip(
    peer: Option<SocketAddr>,
    headers: &HeaderMap,
    proxies: &[IpNet],
) -> Result<Option<IpAddr>, ()> {
    let Some(peer) = peer else {
        return Ok(None);
    };
    let mut current = peer.ip().to_canonical();
    if !trusted(current, proxies) {
        return Ok(Some(current));
    }
    let mut chain = Vec::new();
    let mut length = 0;
    for value in headers.get_all("x-forwarded-for") {
        let value = value.to_str().map_err(|_| ())?;
        length += value.len();
        if length > 1024 {
            return Err(());
        }
        for value in value.split(',') {
            if chain.len() >= 32 {
                return Err(());
            }
            chain.push(
                value
                    .trim()
                    .parse::<IpAddr>()
                    .map_err(|_| ())?
                    .to_canonical(),
            );
        }
    }
    for candidate in chain.into_iter().rev() {
        if !trusted(current, proxies) {
            break;
        }
        current = candidate;
    }
    Ok(Some(current))
}

impl RateLimit {
    fn parse(value: &Value) -> Result<Self, String> {
        let config = object(value, "rate_limit")?;
        keys(
            config,
            &[
                "requests",
                "window_seconds",
                "max_keys",
                "exclude_paths",
                "redis",
            ],
            "rate_limit",
        )?;
        let requests = positive(config, "requests", None, 10_000)? as usize;
        let shared = match config.get("redis") {
            None | Some(Value::Null) => None,
            Some(redis) => {
                if config.contains_key("max_keys") {
                    return Err(
                        "rate_limit max_keys bounds the local table and does not apply with redis"
                            .into(),
                    );
                }
                Some(crate::redis::SharedLimiter::parse(
                    object(redis, "rate_limit redis")?,
                    |name| std::env::var(name).ok(),
                )?)
            }
        };
        let max_keys = positive(config, "max_keys", Some(10_000), 100_000)? as usize;
        // The product bounds the local table's memory; Redis holds its own.
        if shared.is_none() && requests.saturating_mul(max_keys) > 5_000_000 {
            return Err("rate_limit requests multiplied by max_keys cannot exceed 5000000".into());
        }
        let seconds = config
            .get("window_seconds")
            .and_then(Value::as_f64)
            .ok_or("rate_limit window_seconds is required")?;
        if !seconds.is_finite() || !(0.001..=86_400.0).contains(&seconds) {
            return Err("rate_limit window_seconds must be between 0.001 and 86400".into());
        }
        Ok(Self {
            requests,
            window: Duration::from_secs_f64(seconds),
            max_keys,
            exclude_paths: exclusions(config)?,
            state: Mutex::new(RateState {
                entries: HashMap::new(),
                last_prune: Instant::now(),
            }),
            shared,
        })
    }

    async fn admit(&self, ip: IpAddr) -> Result<(), (StatusCode, u64)> {
        match &self.shared {
            None => self.admit_at(ip, Instant::now()),
            Some(shared) => {
                let decision = shared.admit(ip, self.requests, self.window).await;
                crate::redis::rejection(decision).map_or(Ok(()), Err)
            }
        }
    }

    fn admit_at(&self, ip: IpAddr, now: Instant) -> Result<(), (StatusCode, u64)> {
        let mut state = self.state.lock().unwrap();
        if !state.entries.contains_key(&ip) && state.entries.len() >= self.max_keys {
            if now.saturating_duration_since(state.last_prune)
                >= Duration::from_secs(1).min(self.window)
            {
                state.entries.retain(|_, entry| {
                    entry
                        .requests
                        .back()
                        .is_some_and(|last| now.saturating_duration_since(*last) < self.window)
                });
                state.last_prune = now;
            }
            if state.entries.len() >= self.max_keys {
                return Err((
                    StatusCode::SERVICE_UNAVAILABLE,
                    self.window.as_secs_f64().ceil() as u64,
                ));
            }
        }
        let entry = state.entries.entry(ip).or_insert_with(|| RateEntry {
            requests: VecDeque::new(),
        });
        while entry
            .requests
            .front()
            .is_some_and(|first| now.saturating_duration_since(*first) >= self.window)
        {
            entry.requests.pop_front();
        }
        if entry.requests.len() >= self.requests {
            let retry = self
                .window
                .saturating_sub(now.saturating_duration_since(*entry.requests.front().unwrap()))
                .as_secs_f64()
                .ceil() as u64;
            return Err((StatusCode::TOO_MANY_REQUESTS, retry.max(1)));
        }
        entry.requests.push_back(now);
        Ok(())
    }
}

impl Controls {
    pub(crate) fn websocket_origin_allowed(&self, headers: &HeaderMap) -> bool {
        if !headers.contains_key(header::ORIGIN) {
            return true;
        }
        let Some(cors) = &self.cors else {
            return false;
        };
        !cors.wildcard
            && singleton(headers, &header::ORIGIN)
                .ok()
                .flatten()
                .and_then(|origin| origin.to_str().ok())
                .is_some_and(|origin| cors.origin(origin).is_some())
    }

    pub fn from_json(value: &Value) -> Result<Self, String> {
        let empty = Config::new();
        let config = if value.is_null() {
            &empty
        } else {
            object(value, "controls")?
        };
        keys(
            config,
            &[
                "cors",
                "security_headers",
                "request_id",
                "access_log",
                "trusted_proxies",
                "allowed_hosts",
                "api_key",
                "jwt",
                "rate_limit",
                "metrics_path",
                "metrics_token_env",
                "compression",
                "otel",
            ],
            "controls",
        )?;
        let compression = config
            .get("compression")
            .filter(|value| !value.is_null())
            .map(crate::compression::Compression::parse)
            .transpose()?;
        let cors = config
            .get("cors")
            .filter(|value| !value.is_null())
            .map(Cors::parse)
            .transpose()?;
        let proxies = strings(config, "trusted_proxies", &[])?;
        if proxies.len() > 1024 {
            return Err("trusted_proxies cannot contain more than 1024 CIDRs".into());
        }
        let trusted_proxies = proxies
            .iter()
            .map(|value| {
                value
                    .parse::<IpNet>()
                    .map_err(|_| "trusted_proxies must contain valid CIDRs".to_owned())
            })
            .collect::<Result<Vec<_>, _>>()?;
        let allowed_hosts = config
            .get("allowed_hosts")
            .filter(|value| !value.is_null())
            .map(|_| {
                let hosts = strings(config, "allowed_hosts", &[])?;
                if hosts.len() > 1024 {
                    return Err("allowed_hosts cannot contain more than 1024 entries".to_owned());
                }
                hosts
                    .iter()
                    .map(|host| AllowedHost::parse(host))
                    .collect::<Result<Vec<_>, _>>()
            })
            .transpose()?;
        let api_key = config
            .get("api_key")
            .filter(|value| !value.is_null())
            .map(|value| {
                let config = object(value, "api_key")?;
                keys(config, &["env", "header", "exclude_paths"], "api_key")?;
                let header = string(config, "header", Some("X-API-Key"))?;
                if header.len() > 128 {
                    return Err("api_key header cannot exceed 128 characters".to_owned());
                }
                let header = HeaderName::from_bytes(header.as_bytes())
                    .map_err(|_| "api_key header must be a valid header name")?;
                if [
                    header::HOST,
                    header::CONTENT_LENGTH,
                    header::TRANSFER_ENCODING,
                    header::CONNECTION,
                    header::ORIGIN,
                    HeaderName::from_static("x-request-id"),
                    HeaderName::from_static("forwarded"),
                ]
                .contains(&header)
                    || header.as_str().starts_with("access-control-")
                    || header.as_str().starts_with("x-forwarded-")
                {
                    return Err(
                        "api_key header cannot be an HTTP framing or metadata header".to_owned(),
                    );
                }
                Ok(ApiKey {
                    header,
                    secret: env_secret(&string(config, "env", None)?)?,
                    exclude_paths: exclusions(config)?,
                })
            })
            .transpose()?;
        let jwt = config
            .get("jwt")
            .filter(|value| !value.is_null())
            .map(|value| crate::jwt::JwtAuth::parse(value, env_secret))
            .transpose()?;
        let rate_limit = config
            .get("rate_limit")
            .filter(|value| !value.is_null())
            .map(RateLimit::parse)
            .transpose()?;
        let metrics_path = config
            .get("metrics_path")
            .filter(|value| !value.is_null())
            .map(|value| {
                let value = value
                    .as_str()
                    .filter(|path| exact_path(path))
                    .ok_or("metrics_path must be an exact absolute path")?;
                Ok::<_, String>(value.to_owned())
            })
            .transpose()?;
        let metrics_secret = config
            .get("metrics_token_env")
            .filter(|value| !value.is_null())
            .map(|value| env_secret(value.as_str().ok_or("metrics_token_env must be a string")?))
            .transpose()?;
        if metrics_path.is_some() && metrics_secret.is_none() && api_key.is_none() {
            return Err("metrics_path requires api_key or metrics_token_env authentication".into());
        }
        if metrics_path.is_none() && metrics_secret.is_some() {
            return Err("metrics_token_env requires metrics_path".into());
        }
        let tracer = config
            .get("otel")
            .filter(|value| !value.is_null())
            .map(|value| crate::otel::Tracer::parse(value).map(Arc::new))
            .transpose()?;
        let mut metrics = Metrics::with_enabled(metrics_path.is_some());
        metrics.otel_dropped = tracer
            .as_ref()
            .filter(|tracer| tracer.exports())
            .map(|tracer| tracer.dropped.clone());
        metrics.rate_store_errors = rate_limit
            .as_ref()
            .and_then(|rate| rate.shared.as_ref())
            .map(|shared| shared.errors.clone());
        let metrics = Arc::new(metrics);
        let logger = if boolean(config, "access_log", false)? {
            Some(AccessLogger::new(metrics.log_dropped.clone())?)
        } else {
            None
        };
        Ok(Self {
            cors,
            security_headers: boolean(config, "security_headers", true)?,
            request_id: boolean(config, "request_id", true)?,
            trusted_proxies,
            allowed_hosts,
            api_key,
            jwt,
            rate_limit,
            metrics_path,
            metrics_secret,
            metrics,
            scrape_permit: Arc::new(Semaphore::new(1)),
            logger,
            compression,
            tracer,
        })
    }

    /// The tracer, so the server can flush it after a graceful shutdown.
    pub(crate) fn tracer(&self) -> Option<Arc<crate::otel::Tracer>> {
        self.tracer.clone()
    }

    pub fn register_routes(&self, routes: &[String]) -> Result<(), String> {
        let mut routes = routes.to_vec();
        if let Some(path) = &self.metrics_path {
            routes.push(path.clone());
        }
        self.metrics.register_routes(&routes)
    }

    pub async fn begin(
        &self,
        method: &Method,
        uri: &Uri,
        headers: &HeaderMap,
        peer: Option<SocketAddr>,
    ) -> Begin {
        let request_id_header = self.request_id.then(|| request_id(headers));
        let client = client_ip(peer, headers, &self.trusted_proxies);
        let trace = self.tracer.as_ref().and_then(|tracer| {
            tracer.start(headers).map(|span| {
                Box::new(crate::telemetry::Trace {
                    tracer: tracer.clone(),
                    span,
                    path: uri.path().to_owned(),
                })
            })
        });
        // Only the access log and spans read the textual copies.
        let textual = self.logger.is_some() || trace.is_some();
        let telemetry = RequestTelemetry::new(
            self.metrics.clone(),
            self.logger.clone(),
            method.as_str(),
            self.logger.as_ref().and_then(|_| {
                request_id_header
                    .as_ref()
                    .and_then(|value| value.to_str().ok())
                    .map(str::to_owned)
            }),
            textual
                .then(|| {
                    client
                        .as_ref()
                        .ok()
                        .and_then(|value| value.map(|ip| ip.to_string()))
                })
                .flatten(),
            trace,
        );
        let mut context = RequestContext {
            claims: None,
            request_id_header,
            client_ip: client.as_ref().ok().copied().flatten(),
            allowed_origin: None,
            preflight: false,
            metrics_request: self.metrics_path.as_deref() == Some(uri.path()),
            telemetry,
        };
        if let Some(hosts) = &self.allowed_hosts {
            let host = singleton(headers, &header::HOST)
                .ok()
                .flatten()
                .and_then(|value| value.to_str().ok())
                .and_then(crate::transport::normalize_host);
            if !host.is_some_and(|host| hosts.iter().any(|allowed| allowed.matches(&host))) {
                return Begin {
                    context,
                    response: Some(Box::new(reject(
                        StatusCode::BAD_REQUEST,
                        "Host is not allowed",
                    ))),
                };
            }
        }
        if client.is_err() {
            return Begin {
                context,
                response: Some(Box::new(reject(
                    StatusCode::BAD_REQUEST,
                    "Invalid forwarded client address",
                ))),
            };
        }
        let mut preflight_response = None;
        if let Some(cors) = &self.cors {
            let origin = match singleton(headers, &header::ORIGIN) {
                Ok(value) => value,
                Err(_) => {
                    return Begin {
                        context,
                        response: Some(Box::new(reject(
                            StatusCode::BAD_REQUEST,
                            "Multiple Origin headers are not allowed",
                        ))),
                    };
                }
            };
            if let Some(origin) = origin {
                let Some(allowed) = origin.to_str().ok().and_then(|value| cors.origin(value))
                else {
                    return Begin {
                        context,
                        response: Some(Box::new(reject(
                            StatusCode::FORBIDDEN,
                            "CORS origin is not allowed",
                        ))),
                    };
                };
                context.allowed_origin = Some(allowed);
            }
            let requested_method = match singleton(headers, &header::ACCESS_CONTROL_REQUEST_METHOD)
            {
                Ok(value) => value,
                Err(_) => {
                    return Begin {
                        context,
                        response: Some(Box::new(reject(
                            StatusCode::BAD_REQUEST,
                            "Multiple CORS request methods are not allowed",
                        ))),
                    };
                }
            };
            if method == Method::OPTIONS
                && let Some(requested_method) = requested_method
            {
                context.preflight = true;
                let response = if origin.is_none() {
                    reject(StatusCode::BAD_REQUEST, "A CORS preflight requires Origin")
                } else {
                    match requested_method.to_str() {
                        Ok(value) => cors.preflight(value, headers),
                        Err(_) => reject(StatusCode::BAD_REQUEST, "Invalid CORS request method"),
                    }
                };
                preflight_response = Some(response);
            }
        }
        if let Some(rate) = &self.rate_limit
            && !rate.exclude_paths.contains(uri.path())
        {
            let Some(ip) = context.client_ip else {
                return Begin {
                    context,
                    response: Some(Box::new(reject(
                        StatusCode::SERVICE_UNAVAILABLE,
                        "Client address is unavailable for rate limiting",
                    ))),
                };
            };
            // Boxed: a shared (Redis) limiter's future is kilobytes, and inline
            // it would enlarge, and be copied with, every request's future.
            if let Err((status, retry)) = Box::pin(rate.admit(ip)).await {
                let mut response = reject(
                    status,
                    if status == StatusCode::TOO_MANY_REQUESTS {
                        "Rate limit exceeded"
                    } else if rate.shared.is_some() {
                        "Rate limiter is unavailable"
                    } else {
                        "Rate limiter is at capacity"
                    },
                );
                response.headers_mut().insert(
                    header::RETRY_AFTER,
                    HeaderValue::from_str(&retry.max(1).to_string()).unwrap(),
                );
                return Begin {
                    context,
                    response: Some(Box::new(response)),
                };
            }
        }
        if let Some(response) = preflight_response {
            return Begin {
                context,
                response: Some(Box::new(response)),
            };
        }
        if self.metrics_path.as_deref() == Some(uri.path()) {
            let authorized = if let Some(secret) = &self.metrics_secret {
                match singleton(headers, &header::AUTHORIZATION) {
                    Ok(Some(value)) => value
                        .to_str()
                        .ok()
                        .and_then(|value| value.split_once(' '))
                        .filter(|(scheme, _)| scheme.eq_ignore_ascii_case("Bearer"))
                        .is_some_and(|(_, value)| {
                            value
                                .trim_start_matches(' ')
                                .as_bytes()
                                .ct_eq(secret.as_slice())
                                .into()
                        }),
                    Ok(None) => false,
                    Err(_) => {
                        return Begin {
                            context,
                            response: Some(Box::new(reject(
                                StatusCode::BAD_REQUEST,
                                "Multiple Authorization headers are not allowed",
                            ))),
                        };
                    }
                }
            } else {
                match self.authenticated(headers) {
                    Ok(value) => value,
                    Err(_) => {
                        return Begin {
                            context,
                            response: Some(Box::new(reject(
                                StatusCode::BAD_REQUEST,
                                "Multiple API key headers are not allowed",
                            ))),
                        };
                    }
                }
            };
            let mut response = if !authorized {
                unauthorized(self.metrics_secret.is_some())
            } else if method != Method::GET && method != Method::HEAD {
                let mut response = reject(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed");
                response
                    .headers_mut()
                    .insert(header::ALLOW, HeaderValue::from_static("GET, HEAD"));
                response
            } else {
                let mut response = Body::empty().into_response();
                if method == Method::GET {
                    match self.scrape_permit.clone().try_acquire_owned() {
                        Ok(permit) => {
                            response
                                .extensions_mut()
                                .insert(DeferredScrape(Arc::new(ScrapePermit { _permit: permit })));
                        }
                        Err(_) => {
                            response = reject(
                                StatusCode::SERVICE_UNAVAILABLE,
                                "A metrics scrape is already in progress",
                            );
                            response
                                .headers_mut()
                                .insert(header::RETRY_AFTER, HeaderValue::from_static("1"));
                        }
                    }
                }
                if response.status() == StatusCode::OK {
                    response.headers_mut().insert(
                        header::CONTENT_TYPE,
                        HeaderValue::from_static("text/plain; version=0.0.4; charset=utf-8"),
                    );
                }
                response
            };
            response
                .headers_mut()
                .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
            return Begin {
                context,
                response: Some(Box::new(response)),
            };
        }
        if self
            .api_key
            .as_ref()
            .is_some_and(|key| !key.exclude_paths.contains(uri.path()))
        {
            match self.authenticated(headers) {
                Ok(true) => {}
                Ok(false) => {
                    return Begin {
                        context,
                        response: Some(Box::new(unauthorized(false))),
                    };
                }
                Err(_) => {
                    return Begin {
                        context,
                        response: Some(Box::new(reject(
                            StatusCode::BAD_REQUEST,
                            "Multiple API key headers are not allowed",
                        ))),
                    };
                }
            }
        }
        if let Some(jwt) = &self.jwt {
            let authorization = match singleton(headers, &header::AUTHORIZATION) {
                Ok(value) => value.map(HeaderValue::as_bytes),
                Err(_) => {
                    return Begin {
                        context,
                        response: Some(Box::new(reject(
                            StatusCode::BAD_REQUEST,
                            "Multiple Authorization headers are not allowed",
                        ))),
                    };
                }
            };
            match jwt.authenticate(authorization) {
                Ok(claims) => context.claims = Some(claims),
                // Excluded paths are public; a valid token still yields a principal.
                Err(_) if jwt.exclude_paths.contains(uri.path()) => {}
                Err(rejection) => {
                    return Begin {
                        context,
                        response: Some(Box::new(bearer_unauthorized(
                            rejection == crate::jwt::Rejection::Invalid,
                        ))),
                    };
                }
            }
        }
        Begin {
            context,
            response: None,
        }
    }

    fn authenticated(&self, headers: &HeaderMap) -> Result<bool, ()> {
        let Some(key) = &self.api_key else {
            return Ok(false);
        };
        Ok(singleton(headers, &key.header)?
            .is_some_and(|value| value.as_bytes().ct_eq(key.secret.as_slice()).into()))
    }

    pub(crate) fn compression(&self) -> Option<&crate::compression::Compression> {
        self.compression.as_ref()
    }

    /// Whether `resolve_response` has work to do for this response.
    pub fn needs_resolution(response: &Response) -> bool {
        response.extensions().get::<DeferredScrape>().is_some()
    }

    pub async fn resolve_response(&self, mut response: Response) -> Response {
        let Some(scrape) = response.extensions_mut().remove::<DeferredScrape>() else {
            return response;
        };
        let metrics = self.metrics.clone();
        match tokio::task::spawn_blocking(move || (metrics.render(), scrape)).await {
            Ok((text, scrape)) => {
                // The Bytes owner retains admission through formatting and while
                // Hyper owns any response bytes, including a slow client write.
                *response.body_mut() = Body::from(Bytes::from_owner(ScrapePayload {
                    text,
                    _permit: scrape.0,
                }));
                response
            }
            Err(_) => {
                let mut response = reject(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "Metrics are temporarily unavailable",
                );
                response
                    .headers_mut()
                    .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
                response
                    .headers_mut()
                    .insert(header::RETRY_AFTER, HeaderValue::from_static("1"));
                response
            }
        }
    }

    /// Add the response headers the controls own. With `direct`, the request
    /// id and security headers are left there for the HTTP/1 writer instead
    /// of being inserted into the response's map.
    pub fn finish(
        &self,
        context: &RequestContext,
        response: &mut Response,
        route_template: &str,
        direct: Option<&mut DirectHeaders>,
    ) {
        if let Some(direct) = direct {
            direct.request_id = context.request_id_header.clone();
            direct.security = self.security_headers;
        } else {
            if let Some(request_id) = &context.request_id_header {
                response
                    .headers_mut()
                    .insert(X_REQUEST_ID, request_id.clone());
            }
            if self.security_headers {
                let headers = response.headers_mut();
                headers.insert(X_CONTENT_TYPE_OPTIONS, HeaderValue::from_static("nosniff"));
                headers.insert(header::X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));
                headers.insert(
                    header::REFERRER_POLICY,
                    HeaderValue::from_static("no-referrer"),
                );
            }
        }
        if let Some(cors) = &self.cors {
            append_vary(response.headers_mut(), "Origin");
            if let Some(origin) = &context.allowed_origin {
                response
                    .headers_mut()
                    .insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, origin.clone());
                if cors.credentials {
                    response.headers_mut().insert(
                        header::ACCESS_CONTROL_ALLOW_CREDENTIALS,
                        HeaderValue::from_static("true"),
                    );
                }
                if !context.preflight && !cors.expose.is_empty() {
                    response.headers_mut().insert(
                        header::ACCESS_CONTROL_EXPOSE_HEADERS,
                        HeaderValue::from_str(&cors.expose).unwrap(),
                    );
                }
            }
            if context.preflight {
                append_vary(response.headers_mut(), "Access-Control-Request-Method");
                append_vary(response.headers_mut(), "Access-Control-Request-Headers");
            }
        }
        let route = if context.metrics_request {
            self.metrics_path.as_deref().unwrap()
        } else {
            route_template
        };
        context.telemetry.finish(route, response.status().as_u16());
    }
}

fn unauthorized(bearer: bool) -> Response {
    let mut response = reject(StatusCode::UNAUTHORIZED, "Unauthorized");
    response.headers_mut().insert(
        header::WWW_AUTHENTICATE,
        HeaderValue::from_static(if bearer { "Bearer" } else { "ApiKey" }),
    );
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response
}

fn bearer_unauthorized(invalid: bool) -> Response {
    let mut response = unauthorized(true);
    if invalid {
        response.headers_mut().insert(
            header::WWW_AUTHENTICATE,
            HeaderValue::from_static("Bearer error=\"invalid_token\""),
        );
    }
    response
}

fn append_vary(headers: &mut HeaderMap, name: &str) {
    let existing: Vec<_> = headers
        .get_all(header::VARY)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(',').map(str::trim))
        .map(str::to_owned)
        .collect();
    if existing
        .iter()
        .any(|value| value == "*" || value.eq_ignore_ascii_case(name))
    {
        return;
    }
    headers.append(header::VARY, HeaderValue::from_str(name).unwrap());
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allowed_hosts_are_exact_or_subdomains_and_never_ports_or_urls() {
        let controls = Controls::from_json(
            &json!({"allowed_hosts":["Good.Test", "*.example.test", "127.0.0.1", "[::1]"]}),
        )
        .unwrap();
        for host in [
            "GOOD.TEST:8000",
            "good.test.",
            "sub.example.test",
            "many.sub.example.test:443",
            "127.0.0.1:99",
            "[::1]:8000",
        ] {
            let mut headers = HeaderMap::new();
            headers.insert(header::HOST, HeaderValue::from_str(host).unwrap());
            assert!(
                request(&controls, Method::GET, "/", headers, None)
                    .1
                    .is_none(),
                "{host}"
            );
        }
        for host in [
            "evil.test",
            "example.test",
            "badexample.test",
            "good.test.evil",
            "example.test@evil.test",
            "good.test:bad",
            "",
        ] {
            let mut headers = HeaderMap::new();
            headers.insert(header::HOST, HeaderValue::from_str(host).unwrap());
            assert_eq!(
                request(&controls, Method::GET, "/", headers, None)
                    .1
                    .unwrap()
                    .status(),
                StatusCode::BAD_REQUEST,
                "{host}"
            );
        }
        for host in [
            "*",
            "foo..test",
            "-foo.test",
            "foo.test:8000",
            "https://good.test",
            "*.127.0.0.1",
            "under_score.test",
        ] {
            assert!(
                Controls::from_json(&json!({"allowed_hosts":[host]})).is_err(),
                "{host}"
            );
        }
        let controls = Controls::from_json(&json!({"allowed_hosts":[]})).unwrap();
        assert_eq!(
            request(&controls, Method::GET, "/", HeaderMap::new(), None)
                .1
                .unwrap()
                .status(),
            StatusCode::BAD_REQUEST
        );
    }

    #[tokio::test]
    async fn only_one_metrics_payload_can_exist_until_all_its_bytes_are_dropped() {
        let mut controls = Controls::from_json(&Value::Null).unwrap();
        controls.api_key = Some(ApiKey {
            header: HeaderName::from_static("x-api-key"),
            secret: b"private-api-key-1234".to_vec(),
            exclude_paths: HashSet::new(),
        });
        controls.metrics_path = Some("/metrics".to_owned());
        let mut headers = HeaderMap::new();
        headers.insert(
            "x-api-key",
            HeaderValue::from_static("private-api-key-1234"),
        );
        let (_, first) = request(&controls, Method::GET, "/metrics", headers.clone(), None);
        assert_eq!(controls.scrape_permit.available_permits(), 0);
        assert_eq!(
            request(&controls, Method::GET, "/metrics", headers.clone(), None)
                .1
                .unwrap()
                .status(),
            StatusCode::SERVICE_UNAVAILABLE
        );
        let response = controls.resolve_response(first.unwrap()).await;
        assert_eq!(response.status(), StatusCode::OK);
        let bytes = axum::body::to_bytes(response.into_body(), crate::telemetry::MAX_SCRAPE_BYTES)
            .await
            .unwrap();
        assert!(bytes.starts_with(b"# HELP"));
        assert_eq!(controls.scrape_permit.available_permits(), 0);
        drop(bytes);
        assert_eq!(controls.scrape_permit.available_permits(), 1);
        let (_, response) = request(&controls, Method::HEAD, "/metrics", headers, None);
        assert_eq!(response.unwrap().status(), StatusCode::OK);
        assert_eq!(controls.scrape_permit.available_permits(), 1);
    }

    fn request(
        controls: &Controls,
        method: Method,
        path: &str,
        headers: HeaderMap,
        peer: Option<&str>,
    ) -> (RequestContext, Option<Response>) {
        // Without a shared rate store, `begin` completes without waiting.
        let uri = path.parse().unwrap();
        let begin = std::pin::pin!(controls.begin(
            &method,
            &uri,
            &headers,
            peer.map(|value| value.parse().unwrap()),
        ));
        let std::task::Poll::Ready(Begin { context, response }) = std::future::Future::poll(
            begin,
            &mut std::task::Context::from_waker(std::task::Waker::noop()),
        ) else {
            panic!("begin waited without a shared rate store");
        };
        let mut response = response.map(|response| *response);
        if let Some(response) = &mut response {
            controls.finish(&context, response, path, None);
        }
        (context, response)
    }

    #[test]
    fn cors_rejects_unsafe_configuration_and_origins() {
        for value in [
            json!({"origins":["*"],"allow_credentials":true}),
            json!({"origins":["https://good.test/path"]}),
            json!({"origins":["https://user@good.test"]}),
        ] {
            assert!(Controls::from_json(&json!({"cors":value})).is_err());
        }
        let controls = Controls::from_json(&json!({"cors":{"origins":["https://good.test"],"allow_methods":["GET","POST"],"allow_headers":["X-API-Key"],"allow_credentials":true}})).unwrap();
        let mut headers = HeaderMap::new();
        headers.insert(
            header::ORIGIN,
            HeaderValue::from_static("https://good.test"),
        );
        headers.insert(
            header::ACCESS_CONTROL_REQUEST_METHOD,
            HeaderValue::from_static("POST"),
        );
        headers.insert(
            header::ACCESS_CONTROL_REQUEST_HEADERS,
            HeaderValue::from_static("x-api-key, content-type"),
        );
        let (_, response) = request(
            &controls,
            Method::OPTIONS,
            "/items",
            headers.clone(),
            Some("127.0.0.1:1"),
        );
        let response = response.unwrap();
        assert_eq!(response.status(), StatusCode::NO_CONTENT);
        assert_eq!(
            response.headers()[header::ACCESS_CONTROL_ALLOW_ORIGIN],
            "https://good.test"
        );
        assert_eq!(
            response.headers()[header::ACCESS_CONTROL_ALLOW_CREDENTIALS],
            "true"
        );
        headers.insert(
            header::ORIGIN,
            HeaderValue::from_static("https://evil.test"),
        );
        assert_eq!(
            request(&controls, Method::GET, "/items", headers.clone(), None)
                .1
                .unwrap()
                .status(),
            StatusCode::FORBIDDEN
        );
        headers.append(
            header::ORIGIN,
            HeaderValue::from_static("https://good.test"),
        );
        assert_eq!(
            request(&controls, Method::GET, "/items", headers, None)
                .1
                .unwrap()
                .status(),
            StatusCode::BAD_REQUEST
        );
    }

    #[test]
    fn forwarded_addresses_follow_only_trusted_hops_from_the_right() {
        let proxies = vec!["10.0.0.0/8".parse().unwrap()];
        let mut headers = HeaderMap::new();
        headers.insert(
            "x-forwarded-for",
            HeaderValue::from_static("192.0.2.66, 203.0.113.1, 10.1.2.3"),
        );
        assert_eq!(
            client_ip(Some("10.0.0.1:80".parse().unwrap()), &headers, &proxies).unwrap(),
            Some("203.0.113.1".parse().unwrap())
        );
        assert_eq!(
            client_ip(Some("198.51.100.5:80".parse().unwrap()), &headers, &proxies).unwrap(),
            Some("198.51.100.5".parse().unwrap())
        );
        headers.insert("x-forwarded-for", HeaderValue::from_static("invalid"));
        assert!(client_ip(Some("10.0.0.1:80".parse().unwrap()), &headers, &proxies).is_err());
        assert!(client_ip(Some("198.51.100.5:80".parse().unwrap()), &headers, &proxies).is_ok());
    }

    #[test]
    fn sliding_window_and_key_table_are_bounded() {
        let rate =
            RateLimit::parse(&json!({"requests":2,"window_seconds":10,"max_keys":1})).unwrap();
        let ip = "192.0.2.1".parse().unwrap();
        let now = Instant::now();
        assert!(rate.admit_at(ip, now).is_ok());
        assert!(rate.admit_at(ip, now + Duration::from_secs(4)).is_ok());
        assert_eq!(
            rate.admit_at(ip, now + Duration::from_secs(9)),
            Err((StatusCode::TOO_MANY_REQUESTS, 1))
        );
        assert_eq!(
            rate.admit_at("192.0.2.2".parse().unwrap(), now + Duration::from_secs(9)),
            Err((StatusCode::SERVICE_UNAVAILABLE, 10))
        );
        assert!(rate.admit_at(ip, now + Duration::from_secs(10)).is_ok());
        assert!(
            rate.admit_at("192.0.2.2".parse().unwrap(), now + Duration::from_secs(21))
                .is_ok()
        );
    }

    #[test]
    fn request_ids_and_security_headers_are_safe_defaults() {
        let controls = Controls::from_json(&Value::Null).unwrap();
        let mut headers = HeaderMap::new();
        headers.insert("x-request-id", HeaderValue::from_static("valid-ID_123"));
        let (context, response) =
            request(&controls, Method::GET, "/healthz", headers.clone(), None);
        assert!(response.is_none());
        let mut response = StatusCode::OK.into_response();
        controls.finish(&context, &mut response, "/healthz", None);
        assert_eq!(response.headers()["x-request-id"], "valid-ID_123");
        assert_eq!(response.headers()["x-content-type-options"], "nosniff");
        // For the HTTP/1 writer they stay out of the map, which keeps the
        // application's own values for the writer to skip.
        let mut response = StatusCode::OK.into_response();
        response
            .headers_mut()
            .insert("x-frame-options", HeaderValue::from_static("SAMEORIGIN"));
        let mut direct = DirectHeaders::default();
        controls.finish(&context, &mut response, "/healthz", Some(&mut direct));
        assert_eq!(response.headers().len(), 1);
        assert!(direct.replaces(&header::X_FRAME_OPTIONS));
        assert!(direct.replaces(&X_REQUEST_ID));
        assert!(!direct.replaces(&header::CONTENT_TYPE));
        let mut out = Vec::new();
        direct.write(&mut out);
        assert_eq!(
            out,
            b"x-request-id: valid-ID_123\r\nx-content-type-options: nosniff\r\nx-frame-options: DENY\r\nreferrer-policy: no-referrer\r\n"
        );
        headers.append("x-request-id", HeaderValue::from_static("duplicate"));
        assert_ne!(request_id(&headers), "valid-ID_123");
        headers.insert(
            "x-request-id",
            HeaderValue::from_static("bad id with spaces"),
        );
        assert!(!request_id(&headers).to_str().unwrap().contains(' '));
    }

    #[test]
    fn metrics_require_explicit_authentication() {
        assert!(Controls::from_json(&json!({"metrics_path":"/metrics"})).is_err());
        assert!(Controls::from_json(&json!({"metrics_path":"/metrics?token=secret"})).is_err());
        assert!(Controls::from_json(&json!({"api_key":{"env":"INVALID ENV"}})).is_err());
    }

    #[test]
    fn authentication_is_fail_closed_and_metrics_ignore_public_exclusions() {
        let mut controls = Controls::from_json(&Value::Null).unwrap();
        controls.api_key = Some(ApiKey {
            header: HeaderName::from_static("x-api-key"),
            secret: b"private-api-key-1234".to_vec(),
            exclude_paths: ["/healthz".to_owned(), "/metrics".to_owned()]
                .into_iter()
                .collect(),
        });
        controls.metrics_path = Some("/metrics".to_owned());
        controls
            .register_routes(&["/healthz".to_owned(), "/private".to_owned()])
            .unwrap();
        assert_eq!(
            request(&controls, Method::GET, "/private", HeaderMap::new(), None)
                .1
                .unwrap()
                .status(),
            StatusCode::UNAUTHORIZED
        );
        assert!(
            request(&controls, Method::GET, "/healthz", HeaderMap::new(), None)
                .1
                .is_none()
        );
        assert_eq!(
            request(&controls, Method::GET, "/metrics", HeaderMap::new(), None)
                .1
                .unwrap()
                .status(),
            StatusCode::UNAUTHORIZED
        );
        let mut headers = HeaderMap::new();
        headers.insert(
            "x-api-key",
            HeaderValue::from_static("private-api-key-1234"),
        );
        assert!(
            request(&controls, Method::GET, "/private", headers.clone(), None)
                .1
                .is_none()
        );
        assert_eq!(
            request(&controls, Method::GET, "/metrics", headers.clone(), None)
                .1
                .unwrap()
                .status(),
            StatusCode::OK
        );
        headers.append(
            "x-api-key",
            HeaderValue::from_static("private-api-key-1234"),
        );
        assert_eq!(
            request(&controls, Method::GET, "/private", headers, None)
                .1
                .unwrap()
                .status(),
            StatusCode::BAD_REQUEST
        );
        controls.metrics_secret = Some(b"separate-metrics-key".to_vec());
        let mut headers = HeaderMap::new();
        headers.insert(
            header::AUTHORIZATION,
            HeaderValue::from_static("bEaReR separate-metrics-key"),
        );
        assert_eq!(
            request(&controls, Method::GET, "/metrics", headers.clone(), None)
                .1
                .unwrap()
                .status(),
            StatusCode::OK
        );
        headers.append(
            header::AUTHORIZATION,
            HeaderValue::from_static("Bearer separate-metrics-key"),
        );
        assert_eq!(
            request(&controls, Method::GET, "/metrics", headers, None)
                .1
                .unwrap()
                .status(),
            StatusCode::BAD_REQUEST
        );
    }
}
