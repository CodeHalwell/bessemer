//! OpenTelemetry tracing: W3C Trace Context propagation and OTLP/HTTP JSON
//! export of one server span per request.
//!
//! Spans are recorded natively when a request finishes and handed to a
//! bounded queue. A dedicated thread batches them and posts them to the
//! collector, so request handling never waits on the network; a full queue or
//! a failed export drops spans and counts them instead.

use crate::jwks::Target;
use http_body_util::{BodyExt, Full, Limited};
use hyper::{
    Request,
    body::Bytes,
    header::{self, HeaderMap, HeaderName, HeaderValue},
};
use hyper_util::rt::TokioIo;
use ring::rand::SecureRandom;
use serde_json::{Map, Value, json};
use std::{
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
        mpsc::{RecvTimeoutError, SyncSender, TrySendError, sync_channel},
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::{
    io::{AsyncRead, AsyncWrite},
    net::TcpStream,
};

/// Spans waiting for export; more are dropped and counted.
const QUEUE: usize = 2048;
/// The most spans in one export request.
const BATCH: usize = 512;
/// A partial batch is exported after at most this long.
const INTERVAL: Duration = Duration::from_secs(1);
/// Connect, handshake and the export request must finish within this.
const EXPORT_TIMEOUT: Duration = Duration::from_secs(10);
/// Incoming `tracestate` values longer than this are not propagated.
const MAX_TRACESTATE: usize = 512;
/// The largest collector response read (and discarded).
const MAX_RESPONSE: usize = 65_536;

/// A W3C Trace Context span identity.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct SpanContext {
    pub(crate) trace_id: [u8; 16],
    pub(crate) span_id: [u8; 8],
    pub(crate) sampled: bool,
}

impl SpanContext {
    /// The `traceparent` header value naming this span.
    pub(crate) fn traceparent(&self) -> String {
        format!(
            "00-{}-{}-{:02x}",
            hex(&self.trace_id),
            hex(&self.span_id),
            u8::from(self.sampled)
        )
    }
}

fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write;
    let mut text = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        write!(text, "{byte:02x}").expect("writing to a String cannot fail");
    }
    text
}

fn unhex<const N: usize>(text: &str) -> Option<[u8; N]> {
    if text.len() != N * 2
        || !text
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return None;
    }
    let mut bytes = [0u8; N];
    for (index, byte) in bytes.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&text[index * 2..index * 2 + 2], 16).ok()?;
    }
    Some(bytes)
}

/// Parse a `traceparent` header (W3C Trace Context level 1).
///
/// Version `00` must be exactly 55 characters. A later version is read by its
/// first four fields, as the specification asks; `ff` and all-zero ids are
/// invalid.
pub(crate) fn parse_traceparent(value: &str) -> Option<SpanContext> {
    let version = value.get(..2)?;
    let version = unhex::<1>(version)?[0];
    if version == 0xff
        || (version == 0 && value.len() != 55)
        || value.len() < 55
        || (value.len() > 55 && value.as_bytes()[55] != b'-')
    {
        return None;
    }
    let fields = &value[..55];
    let mut parts = fields.split('-');
    let (_, trace, span, flags) = (parts.next()?, parts.next()?, parts.next()?, parts.next()?);
    let trace_id = unhex::<16>(trace)?;
    let span_id = unhex::<8>(span)?;
    let flags = unhex::<1>(flags)?[0];
    if trace_id == [0; 16] || span_id == [0; 8] {
        return None;
    }
    Some(SpanContext {
        trace_id,
        span_id,
        sampled: flags & 1 == 1,
    })
}

/// The trace a request takes part in.
pub(crate) struct ServerSpan {
    /// This server's span when recording, otherwise the incoming parent.
    pub(crate) context: SpanContext,
    pub(crate) tracestate: Option<String>,
    parent: Option<[u8; 8]>,
    start: SystemTime,
    recorded: bool,
}

/// What the finished request reports about itself.
pub(crate) struct Outcome<'a> {
    pub(crate) method: &'a str,
    pub(crate) route: Option<&'a str>,
    pub(crate) path: &'a str,
    /// `None` when the request was cancelled before a response.
    pub(crate) status: Option<u16>,
    pub(crate) client: Option<&'a str>,
}

/// What the exporter thread receives.
enum Message {
    Span(Value),
    /// Export everything queued before this now, then acknowledge.
    Flush(SyncSender<()>),
}

pub(crate) struct Tracer {
    exporter: Option<SyncSender<Message>>,
    /// Root spans are sampled when the trace id's low 64 bits are below this;
    /// `None` samples every root.
    threshold: Option<u64>,
    random: ring::rand::SystemRandom,
    pub(crate) dropped: Arc<AtomicU64>,
}

impl Tracer {
    pub(crate) fn parse(value: &Value) -> Result<Self, String> {
        let config = value.as_object().ok_or("otel must be an object")?;
        for key in config.keys() {
            if ![
                "endpoint",
                "service_name",
                "sample_ratio",
                "headers_env",
                "ca_file",
            ]
            .contains(&key.as_str())
            {
                return Err(format!("otel has an unsupported setting {key}"));
            }
        }
        let text = |name: &str| -> Result<Option<&str>, String> {
            match config.get(name) {
                None | Some(Value::Null) => Ok(None),
                Some(Value::String(value)) => Ok(Some(value.as_str())),
                Some(_) => Err(format!("otel {name} must be a string")),
            }
        };
        let ratio = match config.get("sample_ratio") {
            None | Some(Value::Null) => 1.0,
            Some(value) => value
                .as_f64()
                .filter(|ratio| (0.0..=1.0).contains(ratio))
                .ok_or("otel sample_ratio must be a number from 0 to 1")?,
        };
        let threshold = (ratio < 1.0).then_some((ratio * 18_446_744_073_709_551_616.0) as u64);
        let dropped = Arc::new(AtomicU64::new(0));
        let endpoint = text("endpoint")?;
        if endpoint.is_none() && (text("headers_env")?.is_some() || text("ca_file")?.is_some()) {
            return Err("otel headers_env and ca_file need an endpoint".into());
        }
        let exporter = endpoint
            .map(|endpoint| {
                let target = Target::parse_url(
                    endpoint,
                    "otel endpoint must be an http or https URL",
                    true,
                )?;
                let tls = target
                    .tls
                    .then(|| {
                        crate::jwks::tls_client_config(
                            text("ca_file")?,
                            "otel ca_file",
                            "otel endpoint",
                        )
                        .map(Arc::new)
                    })
                    .transpose()?;
                let headers = match text("headers_env")? {
                    Some(name) => export_headers(name)?,
                    None => Vec::new(),
                };
                let service = text("service_name")?.unwrap_or("bessemer");
                if service.is_empty() || service.len() > 256 {
                    return Err("otel service_name must have 1 to 256 characters".to_owned());
                }
                let resource = json!({"attributes": [
                    attribute("service.name", json!({"stringValue": service})),
                    attribute("telemetry.sdk.name", json!({"stringValue": "bessemer"})),
                    attribute("telemetry.sdk.language", json!({"stringValue": "rust"})),
                    attribute("telemetry.sdk.version", json!({"stringValue": env!("CARGO_PKG_VERSION")})),
                ]});
                spawn(target, tls, headers, resource, dropped.clone())
            })
            .transpose()?;
        Ok(Self {
            exporter,
            threshold,
            random: ring::rand::SystemRandom::new(),
            dropped,
        })
    }

    /// Whether spans are exported (rather than only propagated).
    pub(crate) fn exports(&self) -> bool {
        self.exporter.is_some()
    }

    /// Join the incoming trace, if any, and start this request's span.
    pub(crate) fn start(&self, headers: &HeaderMap) -> Option<ServerSpan> {
        let mut values = headers.get_all("traceparent").iter();
        let incoming = match (values.next(), values.next()) {
            (Some(value), None) => value.to_str().ok().and_then(parse_traceparent),
            _ => None,
        };
        let tracestate = incoming.as_ref().and_then(|_| {
            let mut values = headers.get_all("tracestate").iter();
            match (values.next(), values.next()) {
                (Some(value), None) => value
                    .to_str()
                    .ok()
                    .filter(|text| !text.is_empty() && text.len() <= MAX_TRACESTATE)
                    .map(str::to_owned),
                _ => None,
            }
        });
        if self.exporter.is_none() {
            // Propagation only: hand the caller's span on unchanged.
            return incoming.map(|context| ServerSpan {
                context,
                tracestate,
                parent: None,
                start: SystemTime::now(),
                recorded: false,
            });
        }
        let (trace_id, sampled, parent) = match &incoming {
            Some(parent) => (parent.trace_id, parent.sampled, Some(parent.span_id)),
            None => {
                let trace_id: [u8; 16] = self.id();
                let low = u64::from_be_bytes(trace_id[8..].try_into().expect("8 bytes"));
                let sampled = self.threshold.is_none_or(|threshold| low < threshold);
                (trace_id, sampled, None)
            }
        };
        Some(ServerSpan {
            context: SpanContext {
                trace_id,
                span_id: self.id(),
                sampled,
            },
            tracestate,
            parent,
            start: SystemTime::now(),
            recorded: sampled,
        })
    }

    fn id<const N: usize>(&self) -> [u8; N] {
        let mut id = [0u8; N];
        while id == [0u8; N] {
            if self.random.fill(&mut id).is_err() {
                // No system randomness: fall back to the clock, never zero.
                let nanos = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map_or(1, |elapsed| elapsed.as_nanos())
                    .max(1);
                for (index, byte) in id.iter_mut().enumerate() {
                    *byte = (nanos >> ((index % 16) * 8)) as u8;
                }
            }
        }
        id
    }

    /// Record `span` for export, if it is sampled.
    pub(crate) fn finish(&self, span: &ServerSpan, outcome: &Outcome<'_>) {
        let Some(sender) = &self.exporter else {
            return;
        };
        if !span.recorded {
            return;
        }
        if let Err(TrySendError::Full(_) | TrySendError::Disconnected(_)) =
            sender.try_send(Message::Span(span_json(span, outcome, SystemTime::now())))
        {
            self.dropped.fetch_add(1, Ordering::Relaxed);
        }
    }

    /// Export the spans queued so far, waiting at most `budget`. Called once
    /// the server has shut down gracefully, so a worker that exits normally
    /// does not lose its final batch.
    pub(crate) fn flush(&self, budget: Duration) {
        let Some(sender) = &self.exporter else {
            return;
        };
        let deadline = Instant::now() + budget;
        let (ack, done) = sync_channel(1);
        let mut message = Message::Flush(ack);
        loop {
            match sender.try_send(message) {
                Ok(()) => break,
                Err(TrySendError::Full(returned)) if Instant::now() < deadline => {
                    message = returned;
                    std::thread::sleep(Duration::from_millis(5));
                }
                Err(_) => return,
            }
        }
        let _ = done.recv_timeout(deadline.saturating_duration_since(Instant::now()));
    }
}

fn nanos(time: SystemTime) -> String {
    time.duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_nanos())
        .to_string()
}

fn attribute(key: &str, value: Value) -> Value {
    json!({"key": key, "value": value})
}

fn span_json(span: &ServerSpan, outcome: &Outcome<'_>, end: SystemTime) -> Value {
    let method = match outcome.method {
        "OTHER" => "_OTHER",
        method => method,
    };
    let mut attributes = vec![
        attribute("http.request.method", json!({"stringValue": method})),
        attribute("url.path", json!({"stringValue": outcome.path})),
    ];
    let name = match outcome.route {
        Some(route) => {
            attributes.push(attribute("http.route", json!({"stringValue": route})));
            format!("{method} {route}")
        }
        None => method.to_owned(),
    };
    if let Some(status) = outcome.status {
        attributes.push(attribute(
            "http.response.status_code",
            json!({"intValue": status.to_string()}),
        ));
    }
    if let Some(client) = outcome.client {
        attributes.push(attribute("client.address", json!({"stringValue": client})));
    }
    let mut record = Map::new();
    record.insert("traceId".into(), hex(&span.context.trace_id).into());
    record.insert("spanId".into(), hex(&span.context.span_id).into());
    if let Some(parent) = &span.parent {
        record.insert("parentSpanId".into(), hex(parent).into());
    }
    if let Some(state) = &span.tracestate {
        record.insert("traceState".into(), state.clone().into());
    }
    record.insert("name".into(), name.into());
    // SPAN_KIND_SERVER.
    record.insert("kind".into(), 2.into());
    record.insert("startTimeUnixNano".into(), nanos(span.start).into());
    record.insert("endTimeUnixNano".into(), nanos(end).into());
    record.insert("attributes".into(), attributes.into());
    match outcome.status {
        // STATUS_CODE_ERROR: server errors and requests that never finished.
        None => {
            record.insert(
                "status".into(),
                json!({"code": 2, "message": "request cancelled"}),
            );
        }
        Some(status) if status >= 500 => {
            record.insert("status".into(), json!({"code": 2}));
        }
        Some(_) => {}
    }
    Value::Object(record)
}

/// Read OTLP headers (`key=value,key=value`, values percent-encoded) from
/// the environment variable `name`.
fn export_headers(name: &str) -> Result<Vec<(HeaderName, HeaderValue)>, String> {
    let mut characters = name.chars();
    if name.len() > 128
        || !characters
            .next()
            .is_some_and(|value| value == '_' || value.is_ascii_alphabetic())
        || !characters.all(|value| value == '_' || value.is_ascii_alphanumeric())
    {
        return Err("otel headers_env must be a valid environment variable name".into());
    }
    let text = std::env::var(name)
        .map_err(|_| format!("otel headers environment variable {name} is unavailable"))?;
    if text.len() > 8192 {
        return Err(format!("otel headers in {name} exceed 8192 bytes"));
    }
    let invalid = || format!("otel headers in {name} must be key=value pairs separated by commas");
    let mut headers = Vec::new();
    for pair in text
        .split(',')
        .map(str::trim)
        .filter(|pair| !pair.is_empty())
    {
        let (key, value) = pair.split_once('=').ok_or_else(invalid)?;
        let value = percent_encoding::percent_decode_str(value.trim())
            .decode_utf8()
            .map_err(|_| invalid())?;
        let key = HeaderName::from_bytes(key.trim().as_bytes()).map_err(|_| invalid())?;
        if [
            header::HOST,
            header::CONTENT_LENGTH,
            header::CONTENT_TYPE,
            header::TRANSFER_ENCODING,
            header::CONNECTION,
        ]
        .contains(&key)
        {
            return Err(format!("otel headers in {name} cannot set {key}"));
        }
        let mut value = HeaderValue::from_str(&value).map_err(|_| invalid())?;
        value.set_sensitive(true);
        headers.push((key, value));
    }
    if headers.len() > 32 {
        return Err(format!("otel headers in {name} cannot exceed 32 entries"));
    }
    Ok(headers)
}

fn spawn(
    target: Target,
    tls: Option<Arc<rustls::ClientConfig>>,
    headers: Vec<(HeaderName, HeaderValue)>,
    resource: Value,
    dropped: Arc<AtomicU64>,
) -> Result<SyncSender<Message>, String> {
    let (sender, receiver) = sync_channel::<Message>(QUEUE);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| "could not start the OpenTelemetry exporter".to_owned())?;
    std::thread::Builder::new()
        .name("bessemer-otel".to_owned())
        .spawn(move || {
            let mut failing = false;
            while let Ok(first) = receiver.recv() {
                let mut batch = Vec::new();
                let mut acknowledgements = Vec::new();
                match first {
                    Message::Span(span) => batch.push(span),
                    Message::Flush(ack) => acknowledgements.push(ack),
                }
                let deadline = Instant::now() + INTERVAL;
                while acknowledgements.is_empty() && batch.len() < BATCH {
                    match receiver.recv_timeout(deadline.saturating_duration_since(Instant::now()))
                    {
                        Ok(Message::Span(span)) => batch.push(span),
                        Ok(Message::Flush(ack)) => acknowledgements.push(ack),
                        Err(RecvTimeoutError::Timeout | RecvTimeoutError::Disconnected) => break,
                    }
                }
                if batch.is_empty() {
                    for ack in acknowledgements {
                        let _ = ack.send(());
                    }
                    continue;
                }
                let count = batch.len() as u64;
                let body = json!({"resourceSpans": [{
                    "resource": resource,
                    "scopeSpans": [{
                        "scope": {"name": "bessemer", "version": env!("CARGO_PKG_VERSION")},
                        "spans": batch,
                    }],
                }]});
                let body = Bytes::from(body.to_string());
                match runtime.block_on(export(&target, tls.as_ref(), &headers, body)) {
                    Ok(()) => failing = false,
                    Err(error) => {
                        dropped.fetch_add(count, Ordering::Relaxed);
                        if !failing {
                            diagnostic!(
                                "Bessemer OpenTelemetry: span export failed ({error}); dropping spans until it recovers"
                            );
                        }
                        failing = true;
                    }
                }
                for ack in acknowledgements {
                    let _ = ack.send(());
                }
            }
        })
        .map_err(|_| "could not start the OpenTelemetry exporter".to_owned())?;
    Ok(sender)
}

async fn export(
    target: &Target,
    tls: Option<&Arc<rustls::ClientConfig>>,
    headers: &[(HeaderName, HeaderValue)],
    body: Bytes,
) -> Result<(), String> {
    tokio::time::timeout(EXPORT_TIMEOUT, async {
        let stream = TcpStream::connect((target.host.as_str(), target.port))
            .await
            .map_err(|error| format!("cannot connect: {error}"))?;
        let _ = stream.set_nodelay(true);
        match tls {
            Some(config) => {
                let name = rustls::pki_types::ServerName::try_from(target.host.clone())
                    .map_err(|_| "invalid TLS server name".to_owned())?;
                let stream = tokio_rustls::TlsConnector::from(config.clone())
                    .connect(name, stream)
                    .await
                    .map_err(|error| format!("TLS handshake failed: {error}"))?;
                post(stream, target, headers, body).await
            }
            None => post(stream, target, headers, body).await,
        }
    })
    .await
    .map_err(|_| "timed out".to_owned())?
}

async fn post<S>(
    stream: S,
    target: &Target,
    headers: &[(HeaderName, HeaderValue)],
    body: Bytes,
) -> Result<(), String>
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let (mut sender, connection) = hyper::client::conn::http1::handshake(TokioIo::new(stream))
        .await
        .map_err(|error| format!("HTTP handshake failed: {error}"))?;
    let driver = tokio::spawn(connection);
    let mut request = Request::post(target.path.as_str())
        .header(header::HOST, target.authority())
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::USER_AGENT, "bessemer-otel")
        .header(header::CONNECTION, "close")
        .body(Full::new(body))
        .map_err(|_| "invalid export request".to_owned())?;
    for (name, value) in headers {
        request.headers_mut().append(name.clone(), value.clone());
    }
    let result = async {
        let response = sender
            .send_request(request)
            .await
            .map_err(|error| format!("request failed: {error}"))?;
        let status = response.status();
        // Drain a bounded response so the connection closes cleanly.
        let _ = Limited::new(response.into_body(), MAX_RESPONSE)
            .collect()
            .await;
        if !status.is_success() {
            return Err(format!("the collector answered {status}"));
        }
        Ok(())
    }
    .await;
    driver.abort();
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[test]
    fn traceparent_follows_w3c_trace_context() {
        let value = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01";
        let context = parse_traceparent(value).unwrap();
        assert!(context.sampled);
        assert_eq!(context.traceparent(), value);
        assert!(
            !parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00")
                .unwrap()
                .sampled
        );
        // A later version may append fields after a dash.
        assert!(
            parse_traceparent("cc-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-extra")
                .is_some()
        );
        for bad in [
            "",
            "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "00-00000000000000000000000000000000-00f067aa0ba902b7-01",
            "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01",
            "00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01",
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-extra",
            "cc-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01extra",
            "00-4bf92f3577b34da6a3ce929d0e0e473-600f067aa0ba902b7-01",
            "00_4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        ] {
            assert!(parse_traceparent(bad).is_none(), "{bad}");
        }
    }

    fn headers(pairs: &[(&'static str, &'static str)]) -> HeaderMap {
        let mut map = HeaderMap::new();
        for (name, value) in pairs {
            map.append(*name, HeaderValue::from_static(value));
        }
        map
    }

    #[test]
    fn spans_join_the_incoming_trace_and_sample_by_ratio() {
        let parent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01";
        let propagate = Tracer::parse(&json!({})).unwrap();
        // Without an exporter the caller's span is passed on unchanged.
        let span = propagate
            .start(&headers(&[("traceparent", parent), ("tracestate", "a=1")]))
            .unwrap();
        assert_eq!(span.context.traceparent(), parent);
        assert_eq!(span.tracestate.as_deref(), Some("a=1"));
        assert!(propagate.start(&HeaderMap::new()).is_none());
        // Two traceparent headers are ignored.
        assert!(
            propagate
                .start(&headers(&[
                    ("traceparent", parent),
                    ("traceparent", parent)
                ]))
                .is_none()
        );
        let (sender, receiver) = sync_channel(4);
        let recording = Tracer {
            exporter: Some(sender),
            threshold: None,
            random: ring::rand::SystemRandom::new(),
            dropped: Arc::new(AtomicU64::new(0)),
        };
        let span = recording
            .start(&headers(&[("traceparent", parent)]))
            .unwrap();
        assert_eq!(
            hex(&span.context.trace_id),
            "4bf92f3577b34da6a3ce929d0e0e4736"
        );
        assert_ne!(hex(&span.context.span_id), "00f067aa0ba902b7");
        recording.finish(
            &span,
            &Outcome {
                method: "GET",
                route: Some("/items/{id}"),
                path: "/items/7",
                status: Some(503),
                client: Some("10.0.0.1"),
            },
        );
        let Message::Span(exported) = receiver.try_recv().unwrap() else {
            panic!("expected a span");
        };
        assert_eq!(exported["name"], "GET /items/{id}");
        assert_eq!(exported["parentSpanId"], "00f067aa0ba902b7");
        assert_eq!(exported["kind"], 2);
        assert_eq!(exported["status"]["code"], 2);
        assert!(
            exported["attributes"]
                .as_array()
                .unwrap()
                .contains(&attribute(
                    "http.response.status_code",
                    json!({"intValue": "503"})
                ))
        );
        // An unsampled parent is not recorded.
        let unsampled = recording
            .start(&headers(&[(
                "traceparent",
                "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00",
            )]))
            .unwrap();
        recording.finish(
            &unsampled,
            &Outcome {
                method: "GET",
                route: None,
                path: "/",
                status: Some(200),
                client: None,
            },
        );
        assert!(receiver.try_recv().is_err());
        // A root span with sample_ratio 0 is never recorded.
        let never = Tracer {
            threshold: Some(0),
            ..recording
        };
        assert!(!never.start(&HeaderMap::new()).unwrap().context.sampled);
    }

    #[test]
    fn settings_are_validated() {
        for (config, message) in [
            (json!({"endpoint": "ftp://x"}), "http or https"),
            (json!({"sample_ratio": 2}), "sample_ratio"),
            (json!({"ca_file": "/x"}), "need an endpoint"),
            (json!({"bogus": 1}), "unsupported"),
            (
                json!({"endpoint": "http://127.0.0.1:1", "headers_env": "BESSEMER_TEST_NO_SUCH_HEADERS"}),
                "unavailable",
            ),
        ] {
            let error = Tracer::parse(&config).err().unwrap();
            assert!(error.contains(message), "{error}");
        }
    }

    #[test]
    fn spans_are_batched_and_posted_as_otlp_json() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let listener = runtime
            .block_on(tokio::net::TcpListener::bind("127.0.0.1:0"))
            .unwrap();
        let port = listener.local_addr().unwrap().port();
        let received = runtime.spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = Vec::new();
            let mut buffer = [0u8; 65_536];
            loop {
                let read = socket.read(&mut buffer).await.unwrap();
                request.extend_from_slice(&buffer[..read]);
                let text = String::from_utf8_lossy(&request);
                if let Some((head, body)) = text.split_once("\r\n\r\n") {
                    let length: usize = head
                        .lines()
                        .find_map(|line| {
                            line.to_ascii_lowercase()
                                .strip_prefix("content-length: ")
                                .map(|v| v.trim().parse().unwrap())
                        })
                        .unwrap();
                    if body.len() >= length {
                        socket
                            .write_all(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\n{}")
                            .await
                            .unwrap();
                        return text.into_owned();
                    }
                }
            }
        });
        let tracer = Tracer::parse(&json!({
            "endpoint": format!("http://127.0.0.1:{port}/v1/traces"),
            "service_name": "checkout",
        }))
        .unwrap();
        let span = tracer.start(&HeaderMap::new()).unwrap();
        tracer.finish(
            &span,
            &Outcome {
                method: "POST",
                route: Some("/orders"),
                path: "/orders",
                status: Some(201),
                client: None,
            },
        );
        // A flush exports at once rather than after the batch interval.
        let flushing = Instant::now();
        tracer.flush(Duration::from_secs(5));
        assert!(flushing.elapsed() < Duration::from_millis(900));
        let request = runtime.block_on(received).unwrap();
        let (head, body) = request.split_once("\r\n\r\n").unwrap();
        assert!(head.starts_with("POST /v1/traces HTTP/1.1"));
        assert!(
            head.to_ascii_lowercase()
                .contains("content-type: application/json")
        );
        let document: Value = serde_json::from_str(body).unwrap();
        let resource = &document["resourceSpans"][0];
        assert!(
            resource["resource"]["attributes"]
                .as_array()
                .unwrap()
                .contains(&attribute(
                    "service.name",
                    json!({"stringValue": "checkout"})
                ))
        );
        let exported = &resource["scopeSpans"][0]["spans"][0];
        assert_eq!(exported["name"], "POST /orders");
        assert_eq!(exported["traceId"], hex(&span.context.trace_id));
        assert!(exported.get("parentSpanId").is_none());
        assert!(exported.get("status").is_none());
    }
}
