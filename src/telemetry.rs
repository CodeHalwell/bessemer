//! Bounded, process-local HTTP metrics and a non-blocking access-log producer.

use serde_json::json;
use std::{
    collections::{BTreeMap, HashSet},
    io::Write,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc::{SyncSender, TrySendError, sync_channel},
    },
    time::{Instant, SystemTime, UNIX_EPOCH},
};

const BUCKETS: [f64; 12] = [
    0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
];
const MAX_SERIES: usize = 10_000;
pub const MAX_SCRAPE_BYTES: usize = 4 * 1024 * 1024;
const SCRAPE_TRAILER_RESERVE: usize = 2048;
const MAX_ACCESS_LOG_BYTES: usize = 4096;
type SeriesLabels = (Arc<str>, &'static str, u16);

#[derive(Clone, Default)]
struct Series {
    count: u64,
    sum: f64,
    buckets: [u64; 13],
}

pub struct Metrics {
    routes: Mutex<HashSet<String>>,
    series: Mutex<BTreeMap<SeriesLabels, Series>>,
    in_flight: AtomicU64,
    cancelled: AtomicU64,
    omitted_series: AtomicU64,
    pub log_dropped: Arc<AtomicU64>,
    /// Spans dropped by the OpenTelemetry exporter, when tracing exports.
    pub otel_dropped: Option<Arc<AtomicU64>>,
    /// Rate limit decisions the shared store could not make.
    pub rate_store_errors: Option<Arc<AtomicU64>>,
    enabled: bool,
}

impl Default for Metrics {
    fn default() -> Self {
        Self {
            routes: Mutex::new(HashSet::new()),
            series: Mutex::new(BTreeMap::new()),
            in_flight: AtomicU64::new(0),
            cancelled: AtomicU64::new(0),
            omitted_series: AtomicU64::new(0),
            log_dropped: Arc::new(AtomicU64::new(0)),
            otel_dropped: None,
            rate_store_errors: None,
            enabled: true,
        }
    }
}

impl Metrics {
    pub fn with_enabled(enabled: bool) -> Self {
        Self {
            enabled,
            ..Self::default()
        }
    }

    pub fn register_routes(&self, routes: &[String]) -> Result<(), String> {
        if routes.len() > 1024
            || routes
                .iter()
                .any(|route| route.len() > 1024 || route.chars().any(char::is_control))
        {
            return Err(
                "at most 1024 route templates of at most 1024 bytes are supported by telemetry"
                    .into(),
            );
        }
        *self.routes.lock().unwrap() = routes.iter().cloned().collect();
        Ok(())
    }

    pub fn route_label(&self, route: &str) -> String {
        if self.routes.lock().unwrap().contains(route) {
            route.to_owned()
        } else {
            "<unmatched>".to_owned()
        }
    }

    fn observe(&self, route: String, method: &'static str, status: u16, elapsed: f64) {
        let mut series = self.series.lock().unwrap();
        let mut label = (Arc::<str>::from(route), method, status / 100);
        if !series.contains_key(&label) && series.len() >= MAX_SERIES {
            label = (Arc::from("<overflow>"), "OTHER", 0);
        }
        let value = series.entry(label).or_default();
        value.count += 1;
        value.sum += elapsed;
        let index = BUCKETS
            .iter()
            .position(|bound| elapsed <= *bound)
            .unwrap_or(BUCKETS.len());
        value.buckets[index] += 1;
    }

    pub fn render(&self) -> String {
        use std::fmt::Write as _;
        // Keys share their bounded route strings. Copy fixed-size values while
        // locked, then perform all escaping/formatting without holding the lock.
        let snapshot: Vec<_> = self
            .series
            .lock()
            .unwrap()
            .iter()
            .map(|(labels, series)| (labels.clone(), series.clone()))
            .collect();
        let mut text = String::with_capacity(MAX_SCRAPE_BYTES);
        text.push_str("# HELP bessemer_http_requests_total Completed HTTP requests by declared route.\n# TYPE bessemer_http_requests_total counter\n# HELP bessemer_http_request_duration_seconds HTTP response duration.\n# TYPE bessemer_http_request_duration_seconds histogram\n");
        let mut omitted = 0;
        for (index, ((route, method, class), series)) in snapshot.iter().enumerate() {
            let mut block = String::new();
            let route = route
                .replace('\\', "\\\\")
                .replace('"', "\\\"")
                .replace('\n', "\\n");
            let labels =
                format!("route=\"{route}\",method=\"{method}\",status_class=\"{class}xx\"");
            writeln!(
                block,
                "bessemer_http_requests_total{{{labels}}} {}",
                series.count
            )
            .unwrap();
            let mut cumulative = 0;
            for (index, count) in series.buckets.iter().enumerate() {
                cumulative += count;
                let bound = if index < BUCKETS.len() {
                    BUCKETS[index].to_string()
                } else {
                    "+Inf".to_owned()
                };
                writeln!(block, "bessemer_http_request_duration_seconds_bucket{{{labels},le=\"{bound}\"}} {cumulative}").unwrap();
            }
            writeln!(
                block,
                "bessemer_http_request_duration_seconds_sum{{{labels}}} {}",
                series.sum
            )
            .unwrap();
            writeln!(
                block,
                "bessemer_http_request_duration_seconds_count{{{labels}}} {}",
                series.count
            )
            .unwrap();
            if text.len() + block.len() + SCRAPE_TRAILER_RESERVE > MAX_SCRAPE_BYTES {
                omitted = snapshot.len() - index;
                break;
            }
            text.push_str(&block);
        }
        let omitted_total = self
            .omitted_series
            .fetch_add(omitted as u64, Ordering::Relaxed)
            .saturating_add(omitted as u64);
        writeln!(text, "# HELP bessemer_http_in_flight HTTP requests awaiting a response.\n# TYPE bessemer_http_in_flight gauge\nbessemer_http_in_flight {}", self.in_flight.load(Ordering::Relaxed)).unwrap();
        writeln!(text, "# HELP bessemer_http_cancelled_total HTTP requests dropped before a response.\n# TYPE bessemer_http_cancelled_total counter\nbessemer_http_cancelled_total {}", self.cancelled.load(Ordering::Relaxed)).unwrap();
        writeln!(text, "# HELP bessemer_access_log_dropped_total Access log events dropped due to backpressure.\n# TYPE bessemer_access_log_dropped_total counter\nbessemer_access_log_dropped_total {}", self.log_dropped.load(Ordering::Relaxed)).unwrap();
        if let Some(dropped) = &self.otel_dropped {
            writeln!(text, "# HELP bessemer_otel_spans_dropped_total OpenTelemetry spans dropped by a full queue or a failed export.\n# TYPE bessemer_otel_spans_dropped_total counter\nbessemer_otel_spans_dropped_total {}", dropped.load(Ordering::Relaxed)).unwrap();
        }
        if let Some(errors) = &self.rate_store_errors {
            writeln!(text, "# HELP bessemer_rate_limit_store_errors_total Rate limit decisions the shared store could not make.\n# TYPE bessemer_rate_limit_store_errors_total counter\nbessemer_rate_limit_store_errors_total {}", errors.load(Ordering::Relaxed)).unwrap();
        }
        writeln!(text, "# HELP bessemer_metrics_scrape_omitted_series Series omitted from this scrape to satisfy its byte limit.\n# TYPE bessemer_metrics_scrape_omitted_series gauge\nbessemer_metrics_scrape_omitted_series {omitted}").unwrap();
        writeln!(text, "# HELP bessemer_metrics_omitted_series_total Series omitted across all scrapes due to the byte limit.\n# TYPE bessemer_metrics_omitted_series_total counter\nbessemer_metrics_omitted_series_total {omitted_total}").unwrap();
        debug_assert!(text.len() <= MAX_SCRAPE_BYTES);
        text
    }
}

pub struct AccessLogger {
    sender: SyncSender<String>,
    dropped: Arc<AtomicU64>,
}

impl AccessLogger {
    pub fn new(dropped: Arc<AtomicU64>) -> Result<Arc<Self>, String> {
        let (sender, receiver) = sync_channel::<String>(1024);
        let writer_dropped = dropped.clone();
        std::thread::Builder::new()
            .name("bessemer-access-log".to_owned())
            .spawn(move || {
                let stderr = std::io::stderr();
                while let Ok(line) = receiver.recv() {
                    let mut writer = stderr.lock();
                    if write_access_record(&mut writer, &line).is_err() {
                        writer_dropped.fetch_add(1, Ordering::Relaxed);
                        writer_dropped.fetch_add(
                            receiver.try_iter().take(1024).count() as u64,
                            Ordering::Relaxed,
                        );
                        break;
                    }
                }
            })
            .map_err(|_| "could not start the access-log writer".to_owned())?;
        Ok(Arc::new(Self { sender, dropped }))
    }

    fn write(&self, context: &RequestTelemetry, route: &str, status: u16) {
        // Log declared route templates rather than raw paths. Headers, cookies,
        // query strings and request/response bodies are deliberately omitted.
        let mut line = json!({
            "event": "http_request", "timestamp": SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64(),
            "method": context.method, "route": route, "status": status,
            "duration_ms": context.started.elapsed().as_secs_f64() * 1000.0,
            "request_id": context.request_id, "client_ip": context.client_ip,
        }).to_string();
        line.push('\n');
        if line.len() > MAX_ACCESS_LOG_BYTES {
            self.dropped.fetch_add(1, Ordering::Relaxed);
            return;
        }
        if let Err(TrySendError::Full(_) | TrySendError::Disconnected(_)) =
            self.sender.try_send(line)
        {
            self.dropped.fetch_add(1, Ordering::Relaxed);
        }
    }
}

/// Write `text` and its newline to stderr as one record; see `diagnostic!`.
pub(crate) fn write_diagnostic(text: &str) {
    let _ = write_line(&mut std::io::stderr().lock(), text);
}

fn write_line(writer: &mut impl Write, text: &str) -> std::io::Result<()> {
    let mut line = String::with_capacity(text.len() + 1);
    line.push_str(text);
    line.push('\n');
    writer.write_all(line.as_bytes())
}

fn write_access_record(writer: &mut impl Write, record: &str) -> std::io::Result<()> {
    // StderrLock forwards this single Write call to one OS write on Linux.
    // Including the newline within PIPE_BUF prevents other processes from
    // inserting their log output between this JSON record and its delimiter.
    if writer.write(record.as_bytes())? != record.len() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::WriteZero,
            "incomplete access-log record",
        ));
    }
    Ok(())
}

pub struct RequestTelemetry {
    pub request_id: Option<String>,
    pub client_ip: Option<String>,
    started: Instant,
    method: &'static str,
    finished: AtomicBool,
    /// None for a request that nothing observes (see `inert`).
    metrics: Option<Arc<Metrics>>,
    logger: Option<Arc<AccessLogger>>,
    /// Boxed: tracing is opt-in, and inline the span would add over a
    /// hundred bytes to every request context, which is moved several times.
    trace: Option<Box<Trace>>,
}

/// A request's span and what its export needs.
pub struct Trace {
    pub(crate) tracer: Arc<crate::otel::Tracer>,
    pub(crate) span: crate::otel::ServerSpan,
    pub(crate) path: String,
}

impl RequestTelemetry {
    /// The record of a request when metrics, the access log and tracing are
    /// all off, which is the default: it holds no reference to the metrics,
    /// takes no clock reading and `finish` and `drop` do nothing.
    pub fn inert(started: Instant) -> Self {
        Self {
            request_id: None,
            client_ip: None,
            started,
            method: "OTHER",
            finished: AtomicBool::new(true),
            metrics: None,
            logger: None,
            trace: None,
        }
    }

    #[cfg(test)]
    pub fn new(
        metrics: Arc<Metrics>,
        logger: Option<Arc<AccessLogger>>,
        method: &str,
        request_id: Option<String>,
        client_ip: Option<String>,
        trace: Option<Box<Trace>>,
    ) -> Self {
        Self::observed(
            Instant::now(),
            metrics,
            logger,
            method,
            request_id,
            client_ip,
            trace,
        )
    }

    /// As `new`, with durations measured from `started`, the instant the
    /// request's head was received.
    pub fn observed(
        started: Instant,
        metrics: Arc<Metrics>,
        logger: Option<Arc<AccessLogger>>,
        method: &str,
        request_id: Option<String>,
        client_ip: Option<String>,
        trace: Option<Box<Trace>>,
    ) -> Self {
        if metrics.enabled {
            metrics.in_flight.fetch_add(1, Ordering::Relaxed);
        }
        let method = match method {
            "GET" => "GET",
            "HEAD" => "HEAD",
            "POST" => "POST",
            "PUT" => "PUT",
            "DELETE" => "DELETE",
            "PATCH" => "PATCH",
            "OPTIONS" => "OPTIONS",
            "CONNECT" => "CONNECT",
            "TRACE" => "TRACE",
            _ => "OTHER",
        };
        Self {
            request_id,
            client_ip,
            started,
            method,
            finished: AtomicBool::new(false),
            metrics: Some(metrics),
            logger,
            trace,
        }
    }

    /// The request's span, when tracing is configured and the request has one.
    pub(crate) fn span(&self) -> Option<&crate::otel::ServerSpan> {
        self.trace.as_ref().map(|trace| &trace.span)
    }

    fn export(&self, route: Option<&str>, status: Option<u16>) {
        if let Some(trace) = &self.trace {
            trace.tracer.finish(
                &trace.span,
                &crate::otel::Outcome {
                    method: self.method,
                    route,
                    path: &trace.path,
                    status,
                    client: self.client_ip.as_deref(),
                },
            );
        }
    }

    pub fn finish(&self, route: &str, status: u16) {
        let Some(metrics) = &self.metrics else {
            return;
        };
        if self.finished.swap(true, Ordering::Relaxed) {
            return;
        }
        if !metrics.enabled && self.logger.is_none() && self.trace.is_none() {
            return;
        }
        let route = metrics.route_label(route);
        self.export(
            (route != "<unmatched>").then_some(route.as_str()),
            Some(status),
        );
        if metrics.enabled {
            metrics.observe(
                route.clone(),
                self.method,
                status,
                self.started.elapsed().as_secs_f64(),
            );
            metrics.in_flight.fetch_sub(1, Ordering::Relaxed);
        }
        if let Some(logger) = &self.logger {
            logger.write(self, &route, status);
        }
    }
}

impl Drop for RequestTelemetry {
    fn drop(&mut self) {
        let Some(metrics) = &self.metrics else {
            return;
        };
        if !self.finished.swap(true, Ordering::Relaxed) {
            if metrics.enabled {
                metrics.cancelled.fetch_add(1, Ordering::Relaxed);
                metrics.in_flight.fetch_sub(1, Ordering::Relaxed);
                metrics.observe(
                    "<cancelled>".to_owned(),
                    self.method,
                    499,
                    self.started.elapsed().as_secs_f64(),
                );
            }
            if let Some(logger) = &self.logger {
                logger.write(self, "<cancelled>", 499);
            }
            self.export(None, None);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn worst_case_scrape_is_byte_bounded_and_omits_only_complete_series() {
        let metrics = Metrics::default();
        let routes: Vec<_> = (0..1024)
            .map(|index| {
                let prefix = format!("/{index:04}-");
                format!("{prefix}{}", "\"\\".repeat(509))
            })
            .collect();
        metrics.register_routes(&routes).unwrap();
        for route in routes {
            for method in [
                "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT",
                "OTHER",
            ] {
                metrics.observe(route.clone(), method, 200, 0.1);
            }
        }
        assert_eq!(metrics.series.lock().unwrap().len(), MAX_SERIES + 1);
        let text = metrics.render();
        assert!(text.len() <= MAX_SCRAPE_BYTES);
        assert!(text.ends_with('\n'));
        let count = |prefix: &str| text.lines().filter(|line| line.starts_with(prefix)).count();
        let series = count("bessemer_http_requests_total{");
        assert!(series > 0 && series < MAX_SERIES);
        assert_eq!(
            count("bessemer_http_request_duration_seconds_bucket{"),
            series * 13
        );
        assert_eq!(count("bessemer_http_request_duration_seconds_sum{"), series);
        assert_eq!(
            count("bessemer_http_request_duration_seconds_count{"),
            series
        );
        let omitted = (MAX_SERIES + 1 - series) as u64;
        assert!(text.contains(&format!(
            "bessemer_metrics_scrape_omitted_series {omitted}\n"
        )));
        assert!(text.contains(&format!(
            "bessemer_metrics_omitted_series_total {omitted}\n"
        )));
        let text = metrics.render();
        assert!(text.contains(&format!(
            "bessemer_metrics_omitted_series_total {}\n",
            omitted * 2
        )));
    }

    #[test]
    fn metrics_use_declared_templates_and_close_capacity_on_drop() {
        let metrics = Arc::new(Metrics::default());
        metrics
            .register_routes(&["/items/{item_id}".into()])
            .unwrap();
        let request = RequestTelemetry::new(metrics.clone(), None, "GET", None, None, None);
        request.finish("/items/{item_id}", 200);
        request.finish("/items/{item_id}", 200);
        drop(request);
        let request = RequestTelemetry::new(metrics.clone(), None, "MADE-UP", None, None, None);
        request.finish("/users/private-id?secret=hidden", 404);
        drop(request);
        drop(RequestTelemetry::new(
            metrics.clone(),
            None,
            "GET",
            None,
            None,
            None,
        ));
        let rendered = metrics.render();
        assert!(
            rendered.contains("route=\"/items/{item_id}\",method=\"GET\",status_class=\"2xx\"} 1")
        );
        assert!(!rendered.contains("private-id"));
        assert!(!rendered.contains("MADE-UP"));
        assert!(rendered.contains("bessemer_http_in_flight 0"));
        assert!(rendered.contains("bessemer_http_cancelled_total 1"));
        assert!(rendered.contains("le=\"+Inf\"} 1"));
    }

    #[test]
    fn durations_run_from_the_instant_given_at_the_start() {
        let metrics = Arc::new(Metrics::with_enabled(true));
        metrics.register_routes(&["/slow".to_owned()]).unwrap();
        let started = Instant::now() - std::time::Duration::from_millis(40);
        let request =
            RequestTelemetry::observed(started, metrics.clone(), None, "GET", None, None, None);
        request.finish("/slow", 200);
        let rendered = metrics.render();
        let sum: f64 = rendered
            .lines()
            .find(|line| line.starts_with("bessemer_http_request_duration_seconds_sum{"))
            .and_then(|line| line.rsplit(' ').next())
            .and_then(|value| value.parse().ok())
            .expect("a duration sum");
        assert!((0.04..5.0).contains(&sum), "{sum}");
    }

    #[test]
    fn an_inert_request_records_nothing_and_does_not_count_as_cancelled() {
        let metrics = Arc::new(Metrics::with_enabled(true));
        let before = Arc::strong_count(&metrics);
        let request = RequestTelemetry::inert(Instant::now());
        assert_eq!(Arc::strong_count(&metrics), before);
        request.finish("/x", 200);
        drop(request);
        assert_eq!(metrics.cancelled.load(Ordering::Relaxed), 0);
        assert_eq!(metrics.in_flight.load(Ordering::Relaxed), 0);
        assert!(metrics.series.lock().unwrap().is_empty());
    }

    #[test]
    fn access_logs_have_only_safe_structured_fields_and_bounded_backpressure() {
        let metrics = Arc::new(Metrics::default());
        let (sender, receiver) = sync_channel(1);
        let logger = AccessLogger {
            sender,
            dropped: metrics.log_dropped.clone(),
        };
        let request = RequestTelemetry::new(
            metrics.clone(),
            None,
            "GET",
            Some("correlation".into()),
            Some("192.0.2.1".into()),
            None,
        );
        logger.write(&request, "/items/{item_id}", 200);
        logger.write(&request, "/items/{item_id}", 200);
        assert_eq!(metrics.log_dropped.load(Ordering::Relaxed), 1);
        let record = receiver.recv().unwrap();
        assert!(record.ends_with('\n'));
        assert!(record.len() <= MAX_ACCESS_LOG_BYTES);
        let event: serde_json::Value = serde_json::from_str(&record).unwrap();
        let fields: HashSet<_> = event
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(
            fields,
            [
                "event",
                "timestamp",
                "method",
                "route",
                "status",
                "duration_ms",
                "request_id",
                "client_ip"
            ]
            .into_iter()
            .collect()
        );
        assert_eq!(event["route"], "/items/{item_id}");
        logger.write(&request, &"x".repeat(MAX_ACCESS_LOG_BYTES), 200);
        assert_eq!(metrics.log_dropped.load(Ordering::Relaxed), 2);
        assert!(receiver.try_recv().is_err());
        request.finish("<unmatched>", 200);
    }

    #[test]
    fn access_log_record_and_newline_are_one_write() {
        #[derive(Default)]
        struct Writer {
            calls: usize,
            bytes: Vec<u8>,
        }
        impl Write for Writer {
            fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
                self.calls += 1;
                self.bytes.extend_from_slice(bytes);
                Ok(bytes.len())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let mut writer = Writer::default();
        write_access_record(&mut writer, "{\"event\":\"http_request\"}\n").unwrap();
        assert_eq!(writer.calls, 1);
        assert_eq!(writer.bytes, b"{\"event\":\"http_request\"}\n");
        // Diagnostics are written the same way, so neither can split the other.
        let mut writer = Writer::default();
        write_line(
            &mut writer,
            &format!("Bessemer listening on {}", "127.0.0.1:8000"),
        )
        .unwrap();
        assert_eq!(writer.calls, 1);
        assert_eq!(writer.bytes, b"Bessemer listening on 127.0.0.1:8000\n");
    }
}
