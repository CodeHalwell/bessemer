//! Native response compression negotiated from `Accept-Encoding`.
//!
//! Buffered bodies are compressed whole, and kept only when that saves bytes.
//! Streaming bodies are compressed frame by frame with a sync flush after each
//! frame, so server-sent events and other incremental responses still arrive
//! promptly. Compressed output owns the original bytes it replaced, which
//! keeps the response byte budget charged until Hyper releases the data.
//!
//! A file's bytes are not in memory, and so not in that budget, until they
//! are read. A file small enough to compress whole is therefore reserved in
//! the budget before it is read; when the budget cannot take it, it is
//! compressed as a stream instead, a chunk at a time, like a larger file.
//!
//! Compressing is CPU work on the thread that also serves every connection
//! (and, in the default layout, runs the event loop). Small bodies are
//! compressed there, since a thread hop costs more than a few kilobytes of
//! compression; a body or frame of [`OFFLOAD_MIN`] or more, and any but a
//! small frame of a stream that has already compressed [`STREAM_INLINE`]
//! inline, is compressed on the blocking pool, with no more running at once
//! than there are CPUs.
//!
//! A compressed variant is its own representation, so its `ETag` is made
//! weak. A response sent without its body (a `HEAD`, or a file's `304` or
//! `206`) carries the `ETag` and `Vary` that its `200` would for the same
//! request; a file's `304` or `206` says what that `200` is with a
//! [`Representation`].

use crate::{
    budget::{ByteBudget, BytePermit, Unreserved},
    static_files::FileProbe,
};
use axum::{
    body::{Body, Bytes},
    http::{HeaderMap, HeaderValue, StatusCode, header},
    response::Response,
};
use http_body::{Body as _, Frame, SizeHint};
use http_body_util::BodyExt;
use serde_json::Value;
use std::{
    io::Write,
    num::NonZeroUsize,
    pin::Pin,
    sync::{Arc, Mutex, OnceLock, PoisonError},
    task::{Context, Poll},
};
use tokio::{sync::Semaphore, task::JoinError, time::Instant};

/// Bodies and stream frames of this size or more are compressed on the
/// blocking pool. File chunks (64 KiB) are well above it; a typical JSON
/// response is well below.
const OFFLOAD_MIN: usize = 16 * 1024;
/// Once a stream has compressed this much inline, its frames of
/// [`SMALL_FRAME`] or more are compressed on the blocking pool too, so a
/// stream of medium frames cannot occupy the I/O thread indefinitely.
const STREAM_INLINE: usize = 64 * 1024;
const SMALL_FRAME: usize = 1024;
/// A stream that compresses this much inline without having waited for
/// anything yields to the runtime before taking its next frame.
const YIELD_AFTER: usize = 16 * 1024;
/// Larger exact-size bodies, such as files, stream through the encoder rather
/// than being read into memory whole.
const BUFFER_LIMIT: u64 = 1024 * 1024;
const GZIP_LEVEL: u32 = 6;
/// A dynamic-content quality: far faster than 11, still smaller than gzip.
const BROTLI_QUALITY: u32 = 4;
const BROTLI_WINDOW: u32 = 22;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Coding {
    Gzip,
    Brotli,
}

impl Coding {
    fn name(self) -> &'static str {
        match self {
            Self::Gzip => "gzip",
            Self::Brotli => "br",
        }
    }
}

#[derive(Debug)]
pub(crate) struct Compression {
    gzip: bool,
    br: bool,
    min_size: u64,
}

#[derive(Clone, Copy)]
pub(crate) struct FilePreview {
    min_size: u64,
}

impl FilePreview {
    pub(crate) fn needs_probe(self, response: &Response, length: u64, head: bool) -> bool {
        if !(self.min_size..=BUFFER_LIMIT).contains(&length) {
            return false;
        }
        if response.status() == StatusCode::NOT_MODIFIED {
            return response
                .extensions()
                .get::<Representation>()
                .is_some_and(|representation| representation.compressible(response.headers()));
        }
        head && compressible(response)
    }
}

impl Compression {
    pub(crate) fn parse(value: &Value) -> Result<Self, String> {
        let Value::Object(config) = value else {
            return Err("compression must be an object".into());
        };
        if let Some(key) = config
            .keys()
            .find(|key| !matches!(key.as_str(), "gzip" | "br" | "min_size"))
        {
            return Err(format!("compression contains unsupported setting {key}"));
        }
        let flag = |name: &str| match config.get(name) {
            None => Ok(true),
            Some(Value::Bool(value)) => Ok(*value),
            Some(_) => Err(format!("compression {name} must be a bool")),
        };
        let gzip = flag("gzip")?;
        let br = flag("br")?;
        if !gzip && !br {
            return Err("compression must enable gzip or br".into());
        }
        let min_size = match config.get("min_size") {
            None => 1024,
            Some(value) => value
                .as_u64()
                .filter(|size| *size <= 1 << 30)
                .ok_or("compression min_size must be an integer from 0 to 1073741824")?,
        };
        Ok(Self { gzip, br, min_size })
    }

    /// The coding to use for a request, if the client accepts one we offer.
    pub(crate) fn negotiate(&self, accept: &HeaderMap) -> Option<Coding> {
        let mut gzip = None;
        let mut br = None;
        let mut any = None;
        for value in accept.get_all(header::ACCEPT_ENCODING) {
            let Ok(value) = value.to_str() else { continue };
            for item in value.split(',') {
                let mut parts = item.split(';');
                let name = parts.next().unwrap_or("").trim();
                let mut quality = 1.0_f32;
                for parameter in parts {
                    let parameter = parameter.trim();
                    if let Some(raw) = parameter
                        .strip_prefix("q=")
                        .or_else(|| parameter.strip_prefix("Q="))
                    {
                        quality = raw.trim().parse().unwrap_or(0.0);
                    }
                }
                if name.eq_ignore_ascii_case("gzip") || name.eq_ignore_ascii_case("x-gzip") {
                    gzip = Some(quality);
                } else if name.eq_ignore_ascii_case("br") {
                    br = Some(quality);
                } else if name == "*" {
                    any = Some(quality);
                }
            }
        }
        let accepted = |explicit: Option<f32>| explicit.or(any).is_some_and(|q| q > 0.0);
        let br_quality = br.or(any).unwrap_or(0.0);
        let gzip_quality = gzip.or(any).unwrap_or(0.0);
        if self.br && accepted(br) && (!self.gzip || br_quality >= gzip_quality) {
            Some(Coding::Brotli)
        } else if self.gzip && accepted(gzip) {
            Some(Coding::Gzip)
        } else if self.br && accepted(br) {
            Some(Coding::Brotli)
        } else {
            None
        }
    }

    pub(crate) fn file_preview(&self, coding: Option<Coding>) -> Option<FilePreview> {
        coding.map(|_| FilePreview {
            min_size: self.min_size.max(1),
        })
    }

    async fn file_validator_is_weak(
        &self,
        coding: Option<Coding>,
        representation: &Representation,
        response: &mut Response,
        budget: &ByteBudget,
    ) -> Result<bool, FilePreviewError> {
        let Some(coding) = coding else {
            return Ok(false);
        };
        if !representation.compressible(response.headers())
            || representation.length < self.min_size.max(1)
        {
            return Ok(false);
        }
        probe_file(coding, response, budget).await
    }

    /// Give a file's `304` or `206` the `Vary` and `ETag` its `200` would
    /// carry for this request (RFC 9110 section 15.4.5), and let a `304`
    /// chosen by `If-None-Match` stand only when the client listed `*` or
    /// the tag it would be sent: the compressed variant's weak tag when the
    /// `200` would be compressed, the file's own tag otherwise. A client that
    /// listed only the other holds another representation, and is sent this
    /// one. `if_none_match` is the request's.
    ///
    /// Only a response with a [`Representation`], which the file layer adds,
    /// is changed: nothing is known of the `200` behind an application's own
    /// `304` or `206`.
    pub(crate) async fn complete(
        &self,
        coding: Option<Coding>,
        if_none_match: Option<&HeaderValue>,
        mut response: Response,
        budget: &ByteBudget,
    ) -> Response {
        let Some(mut representation) = response.extensions_mut().remove::<Representation>() else {
            return response;
        };
        let mut weak = false;
        if response.status() == StatusCode::NOT_MODIFIED {
            weak = match self
                .file_validator_is_weak(coding, &representation, &mut response, budget)
                .await
            {
                Ok(weak) => weak,
                Err(error) => return error.response(),
            };
        }
        if let Some(reselect) = representation.reselect.take()
            && response.status() == StatusCode::NOT_MODIFIED
            && !lists_sent_tag(if_none_match, response.headers().get(header::ETAG), weak)
        {
            let Some(again) = reselect.run(weak).await else {
                return crate::error(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error");
            };
            // Preserve extensions owned by the original request.
            let (parts, body) = again.into_parts();
            *response.status_mut() = parts.status;
            *response.headers_mut() = parts.headers;
            *response.body_mut() = body;
            response.extensions_mut().extend(parts.extensions);
            match response.extensions_mut().remove::<Representation>() {
                Some(selected) => representation = selected,
                None => return response,
            }
            if response.status() == StatusCode::NOT_MODIFIED {
                weak = match self
                    .file_validator_is_weak(coding, &representation, &mut response, budget)
                    .await
                {
                    Ok(weak) => weak,
                    Err(error) => return error.response(),
                };
            }
        }
        let status = response.status();
        if status != StatusCode::NOT_MODIFIED && status != StatusCode::PARTIAL_CONTENT {
            return response;
        }
        let headers = response.headers_mut();
        if !representation.compressible(headers) {
            return response;
        }
        append_vary(headers);
        // A range is of the identity representation and keeps its strong tag.
        if status == StatusCode::NOT_MODIFIED && weak {
            weaken_etag(headers);
        }
        response
    }

    pub(crate) async fn prepare_file_head(
        &self,
        coding: Coding,
        mut response: Response,
        budget: &ByteBudget,
    ) -> (Response, bool) {
        match probe_file(coding, &mut response, budget).await {
            Ok(encoded) => {
                let omit = self.prepare_head(encoded.then_some(coding), &mut response);
                (response, omit)
            }
            Err(error) => (error.response(), false),
        }
    }

    /// Give a `HEAD` the headers of the `GET` it stands for: `Vary` when that
    /// is compressible and, when it would be compressed, its
    /// `Content-Encoding` and weak `ETag`, without the `Content-Length`,
    /// which is known only once the body has been compressed. Returns
    /// whether the length is to be omitted.
    pub(crate) fn prepare_head(&self, coding: Option<Coding>, response: &mut Response) -> bool {
        if !compressible(response) {
            return false;
        }
        append_vary(response.headers_mut());
        let Some(coding) = coding else {
            return false;
        };
        // HEAD bodies may already be empty; only their header retains the GET length.
        let length = response
            .headers()
            .get(header::CONTENT_LENGTH)
            .and_then(|value| value.to_str().ok()?.parse::<u64>().ok());
        if length.is_some_and(|length| length < self.min_size.max(1)) {
            return false;
        }
        mark_encoded(response.headers_mut(), coding);
        true
    }

    /// Compress a response for the negotiated coding when that is safe.
    /// `budget` is the response byte budget.
    pub(crate) async fn apply(
        &self,
        coding: Option<Coding>,
        response: Response,
        budget: &ByteBudget,
    ) -> Response {
        let mut response = response;
        if !compressible(&response) {
            return response;
        }
        append_vary(response.headers_mut());
        let Some(coding) = coding else {
            return response;
        };
        let hint = response.body().size_hint();
        match hint.exact() {
            Some(size) if size < self.min_size.max(1) => response,
            Some(size) if size <= BUFFER_LIMIT => {
                let reserved = if response.extensions().get::<Unreserved>().is_some() {
                    // `size` is at most `BUFFER_LIMIT`, so it fits a `usize`.
                    match budget.acquire(size as usize) {
                        Ok(reserved) => Some(reserved),
                        Err(_) => return streamed(coding, response),
                    }
                } else {
                    None
                };
                buffered(coding, response, reserved).await
            }
            _ => streamed(coding, response),
        }
    }
}

fn compressible(response: &Response) -> bool {
    let status = response.status();
    if status.is_informational()
        || status == StatusCode::NO_CONTENT
        || status == StatusCode::NOT_MODIFIED
        || status == StatusCode::PARTIAL_CONTENT
    {
        return false;
    }
    let headers = response.headers();
    if headers.contains_key(header::CONTENT_ENCODING) || headers.contains_key(header::CONTENT_RANGE)
    {
        return false;
    }
    if no_transform(headers) {
        return false;
    }
    headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .is_some_and(compressible_type)
}

fn no_transform(headers: &HeaderMap) -> bool {
    headers
        .get_all(header::CACHE_CONTROL)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .any(|value| {
            value
                .split(',')
                .any(|directive| directive.trim().eq_ignore_ascii_case("no-transform"))
        })
}

/// The `200` that a file's response without its full body (a `304` or a
/// `206`) stands for, which that response no longer shows: a `304` has no
/// `Content-Type` or `Content-Encoding`, and neither has the full length.
/// The file layer adds it; [`Compression::complete`] reads it.
#[derive(Clone)]
pub(crate) struct Representation {
    pub(crate) content_type: Option<HeaderValue>,
    pub(crate) length: u64,
    /// The `200` carries a `Content-Encoding` of its own, such as a
    /// precompressed sibling's.
    pub(crate) encoded: bool,
    /// For a `304` chosen by `If-None-Match`, which the file layer compares
    /// weakly: selects the response again, comparing exactly with the tag
    /// this client would be sent.
    pub(crate) reselect: Option<Reselect>,
}

impl Representation {
    /// Whether compression applies to the `200`. `headers` are the bodyless
    /// response's, which keep its `Cache-Control`.
    fn compressible(&self, headers: &HeaderMap) -> bool {
        !self.encoded
            && !no_transform(headers)
            && self
                .content_type
                .as_ref()
                .and_then(|value| value.to_str().ok())
                .is_some_and(compressible_type)
    }
}

type ReselectWork = Box<dyn FnOnce(bool) -> Response + Send>;

/// Blocking work that selects a file's response again, given whether the
/// tag this client is sent is the weak one. It runs at most once.
#[derive(Clone)]
pub(crate) struct Reselect(Arc<Mutex<Option<ReselectWork>>>);

impl Reselect {
    pub(crate) fn new(work: impl FnOnce(bool) -> Response + Send + 'static) -> Self {
        Self(Arc::new(Mutex::new(Some(Box::new(work)))))
    }

    async fn run(self, weak: bool) -> Option<Response> {
        let work = self
            .0
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take()?;
        tokio::task::spawn_blocking(move || work(weak)).await.ok()
    }
}

/// Whether `If-None-Match` lists `*` or exactly the tag this client is sent:
/// `etag`, or its weak form when `weak`.
fn lists_sent_tag(
    if_none_match: Option<&HeaderValue>,
    etag: Option<&HeaderValue>,
    weak: bool,
) -> bool {
    let (Some(if_none_match), Some(etag)) = (if_none_match, etag) else {
        return true;
    };
    let etag = etag.as_bytes();
    let opaque = etag.strip_prefix(b"W/").unwrap_or(etag);
    let weak = weak || opaque.len() < etag.len();
    if_none_match
        .as_bytes()
        .split(|byte| *byte == b',')
        .map(<[u8]>::trim_ascii)
        .any(|candidate| {
            candidate == b"*"
                || match candidate.strip_prefix(b"W/") {
                    Some(tag) => weak && tag == opaque,
                    None => !weak && candidate == opaque,
                }
        })
}

/// Text-like media types; already-compressed formats are left alone.
fn compressible_type(content_type: &str) -> bool {
    let essence = content_type
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    let Some((kind, subtype)) = essence.split_once('/') else {
        return false;
    };
    kind == "text"
        || subtype.ends_with("+json")
        || subtype.ends_with("+xml")
        || matches!(
            (kind, subtype),
            ("application", "json")
                | ("application", "javascript")
                | ("application", "x-javascript")
                | ("application", "ecmascript")
                | ("application", "xml")
                | ("application", "x-ndjson")
                | ("application", "graphql-response+json")
                | ("application", "wasm")
                | ("image", "svg+xml")
                | ("image", "x-icon")
                | ("image", "vnd.microsoft.icon")
                | ("font", "ttf")
                | ("font", "otf")
        )
}

fn append_vary(headers: &mut HeaderMap) {
    let present = headers
        .get_all(header::VARY)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .any(|name| {
            let name = name.trim();
            name == "*" || name.eq_ignore_ascii_case("accept-encoding")
        });
    if !present {
        headers.append(header::VARY, HeaderValue::from_static("Accept-Encoding"));
    }
}

fn mark_encoded(headers: &mut HeaderMap, coding: Coding) {
    headers.insert(
        header::CONTENT_ENCODING,
        HeaderValue::from_static(coding.name()),
    );
    headers.remove(header::CONTENT_LENGTH);
    headers.remove(header::ACCEPT_RANGES);
    // The representation changed, so a strong validator no longer matches it.
    weaken_etag(headers);
}

fn weaken_etag(headers: &mut HeaderMap) {
    if let Some(etag) = headers.get(header::ETAG)
        && !etag.as_bytes().starts_with(b"W/")
    {
        let mut weak = b"W/".to_vec();
        weak.extend_from_slice(etag.as_bytes());
        if let Ok(weak) = HeaderValue::from_bytes(&weak) {
            headers.insert(header::ETAG, weak);
        }
    }
}

/// Compressed data that keeps the bytes it replaced, and their budget, alive.
struct Encoded {
    data: Vec<u8>,
    _original: Bytes,
}

impl AsRef<[u8]> for Encoded {
    fn as_ref(&self) -> &[u8] {
        &self.data
    }
}

fn compress_all(coding: Coding, data: &[u8]) -> Vec<u8> {
    let mut encoder = Encoder::new(coding, data.len() / 2);
    encoder.write(data);
    encoder.finish()
}

/// Compressions that may run on the blocking pool at once: no more than there
/// are CPUs, since more cannot finish sooner. Past that a body or frame waits
/// here for its turn, holding no thread, so one client asking for many large
/// compressed responses cannot fill the pool that file reads also use.
/// Nothing is refused; a buffered body's wait counts against its deadline.
fn offload_slots() -> &'static Arc<Semaphore> {
    static SLOTS: OnceLock<Arc<Semaphore>> = OnceLock::new();
    SLOTS.get_or_init(|| Arc::new(Semaphore::new(offload_slot_count())))
}

fn offload_slot_count() -> usize {
    std::thread::available_parallelism()
        .map_or(1, NonZeroUsize::get)
        .clamp(1, Semaphore::MAX_PERMITS)
}

/// Run `work` on the blocking pool once a compression slot is free. The slot
/// is held by the blocking task itself, so a response dropped while its
/// compression runs does not release it early.
async fn off_runtime<T: Send + 'static>(
    work: impl FnOnce() -> T + Send + 'static,
) -> Result<T, JoinError> {
    let slot = offload_slots()
        .clone()
        .acquire_owned()
        .await
        .expect("the compression semaphore is never closed");
    tokio::task::spawn_blocking(move || {
        let _slot = slot;
        work()
    })
    .await
}

fn timed_out() -> Response {
    crate::error(StatusCode::GATEWAY_TIMEOUT, "Request timed out")
}

enum FilePreviewError {
    Read,
    Timeout,
}

impl FilePreviewError {
    fn response(self) -> Response {
        match self {
            Self::Read => crate::error(StatusCode::INTERNAL_SERVER_ERROR, "Response body failed"),
            Self::Timeout => timed_out(),
        }
    }
}

async fn probe_file(
    coding: Coding,
    response: &mut Response,
    budget: &ByteBudget,
) -> Result<bool, FilePreviewError> {
    let Some(probe) = response.extensions_mut().remove::<FileProbe>() else {
        return Ok(true);
    };
    // A file that cannot be buffered is compressed as a stream.
    let Ok(permit) = budget.acquire(probe.length()) else {
        return Ok(true);
    };
    let task = off_runtime(move || {
        let _permit = permit;
        let input = probe.read()?;
        let encoded = compress_all(coding, &input);
        Ok::<_, std::io::Error>(encoded.len() < input.len())
    });
    let deadline = response
        .extensions()
        .get::<crate::ProcessingDeadline>()
        .map(|deadline| deadline.0);
    let joined = match deadline {
        Some(deadline) => tokio::time::timeout_at(deadline, task)
            .await
            .map_err(|_| FilePreviewError::Timeout)?,
        None => task.await,
    };
    if deadline.is_some_and(|deadline| Instant::now() >= deadline) {
        return Err(FilePreviewError::Timeout);
    }
    joined
        .map_err(|_| FilePreviewError::Read)?
        .map_err(|_| FilePreviewError::Read)
}

/// Compress a body of known size whole. `reserved` is the budget taken for a
/// body that had none of its own; it stays with the bytes read, and so with
/// whichever of them or their compressed form is sent.
async fn buffered(coding: Coding, response: Response, reserved: Option<BytePermit>) -> Response {
    let deadline = response
        .extensions()
        .get::<crate::ProcessingDeadline>()
        .map(|deadline| deadline.0);
    let expired = || deadline.is_some_and(|deadline| Instant::now() >= deadline);
    let (mut parts, body) = response.into_parts();
    let whole = reserved
        .as_ref()
        .and_then(|_| parts.extensions.remove::<crate::budget::WholeFile>());
    let (reserved, shared) = match (reserved, whole) {
        (Some(permit), Some(file)) => (None, Some(file.reserve(permit))),
        (reserved, _) => (reserved, None),
    };
    let original = match body.collect().await {
        Ok(collected) => collected.to_bytes(),
        // A failed body (a file read error, say) must not become a complete,
        // cacheable response; nothing has been sent yet, so report it.
        Err(_) => {
            return crate::error(StatusCode::INTERNAL_SERVER_ERROR, "Response body failed");
        }
    };
    let original = match (reserved, shared) {
        (Some(reserved), _) => ByteBudget::own_reserved(original, reserved),
        (_, Some(shared)) => ByteBudget::own_reserved(original, shared),
        _ => original,
    };
    let compressed = if original.len() >= OFFLOAD_MIN {
        let input = original.clone();
        let task = off_runtime(move || compress_all(coding, &input));
        let joined = match deadline {
            Some(deadline) => match tokio::time::timeout_at(deadline, task).await {
                Ok(joined) => joined,
                // The blocking task finishes on its own; its output is dropped.
                Err(_) => return timed_out(),
            },
            None => task.await,
        };
        match joined {
            Ok(compressed) => compressed,
            Err(_) => return Response::from_parts(parts, Body::from(original)),
        }
    } else {
        compress_all(coding, &original)
    };
    // The handler's deadline covers native post-processing too: a response
    // that only became ready after it is a timeout, as in the dispatcher.
    if expired() {
        return timed_out();
    }
    if compressed.len() >= original.len() {
        return Response::from_parts(parts, Body::from(original));
    }
    mark_encoded(&mut parts.headers, coding);
    let body = Bytes::from_owner(Encoded {
        data: compressed,
        _original: original,
    });
    Response::from_parts(parts, Body::from(body))
}

fn streamed(coding: Coding, response: Response) -> Response {
    let (mut parts, body) = response.into_parts();
    mark_encoded(&mut parts.headers, coding);
    let body = Body::new(Streamed {
        inner: body,
        encoder: Some(Encoder::new(coding, 0)),
        working: None,
        trailers: None,
        inline: 0,
        since_yield: 0,
    });
    Response::from_parts(parts, body)
}

enum Encoder {
    Gzip(Box<flate2::write::GzEncoder<Vec<u8>>>),
    Brotli(Box<brotli::CompressorWriter<Vec<u8>>>),
}

impl Encoder {
    fn new(coding: Coding, capacity: usize) -> Self {
        let output = Vec::with_capacity(capacity);
        match coding {
            Coding::Gzip => Self::Gzip(Box::new(flate2::write::GzEncoder::new(
                output,
                flate2::Compression::new(GZIP_LEVEL),
            ))),
            Coding::Brotli => Self::Brotli(Box::new(brotli::CompressorWriter::new(
                output,
                4096,
                BROTLI_QUALITY,
                BROTLI_WINDOW,
            ))),
        }
    }

    // Writes into a Vec cannot fail.
    fn write(&mut self, data: &[u8]) {
        match self {
            Self::Gzip(encoder) => encoder.write_all(data),
            Self::Brotli(encoder) => encoder.write_all(data),
        }
        .expect("in-memory compression cannot fail");
    }

    /// Emit everything written so far as a decodable prefix.
    fn flush(&mut self) -> Vec<u8> {
        match self {
            Self::Gzip(encoder) => {
                encoder.flush().expect("in-memory compression cannot fail");
                std::mem::take(encoder.get_mut())
            }
            Self::Brotli(encoder) => {
                encoder.flush().expect("in-memory compression cannot fail");
                std::mem::take(encoder.get_mut())
            }
        }
    }

    fn finish(self) -> Vec<u8> {
        match self {
            Self::Gzip(encoder) => encoder.finish().expect("in-memory compression cannot fail"),
            Self::Brotli(encoder) => encoder.into_inner(),
        }
    }
}

/// Encoder output for one frame, compressed on the blocking pool when the
/// frame is large. The encoder travels with the task and comes back with it.
type Work = Pin<Box<dyn Future<Output = Result<(Encoder, Vec<u8>), JoinError>> + Send>>;

struct Streamed {
    inner: Body,
    encoder: Option<Encoder>,
    /// A large frame being compressed off the runtime; no further input is
    /// read until it returns, which keeps frame order and backpressure.
    working: Option<(Work, Bytes)>,
    trailers: Option<HeaderMap>,
    /// Bytes this stream has compressed on the runtime thread.
    inline: usize,
    /// Of those, the bytes compressed since the stream last waited.
    since_yield: usize,
}

impl Streamed {
    /// Whether a frame of `len` bytes is compressed on the blocking pool.
    fn offloads(&self, len: usize) -> bool {
        len >= OFFLOAD_MIN || (self.inline >= STREAM_INLINE && len >= SMALL_FRAME)
    }
}

fn compress_frame(mut encoder: Encoder, input: Bytes) -> Work {
    Box::pin(off_runtime(move || {
        encoder.write(&input);
        let output = encoder.flush();
        (encoder, output)
    }))
}

fn encoded_frame(output: Vec<u8>, original: Bytes) -> Frame<Bytes> {
    Frame::data(Bytes::from_owner(Encoded {
        data: output,
        _original: original,
    }))
}

impl http_body::Body for Streamed {
    type Data = Bytes;
    type Error = axum::Error;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
        loop {
            if let Some((work, _)) = self.working.as_mut() {
                let Poll::Ready(joined) = work.as_mut().poll(cx) else {
                    self.since_yield = 0;
                    return Poll::Pending;
                };
                let (_, original) = self.working.take().expect("checked above");
                let (encoder, output) = joined.map_err(axum::Error::new)?;
                self.encoder = Some(encoder);
                if output.is_empty() {
                    continue;
                }
                return Poll::Ready(Some(Ok(encoded_frame(output, original))));
            }
            if self.encoder.is_none() {
                return Poll::Ready(self.trailers.take().map(|t| Ok(Frame::trailers(t))));
            }
            if self.since_yield >= YIELD_AFTER {
                // A fast producer of small frames would otherwise keep this
                // thread compressing: let other connections have a turn.
                self.since_yield = 0;
                cx.waker().wake_by_ref();
                return Poll::Pending;
            }
            let Poll::Ready(next) = Pin::new(&mut self.inner).poll_frame(cx) else {
                self.since_yield = 0;
                return Poll::Pending;
            };
            match next {
                Some(Ok(frame)) => match frame.into_data() {
                    Ok(data) => {
                        if data.is_empty() {
                            continue;
                        }
                        if self.offloads(data.len()) {
                            let encoder = self.encoder.take().expect("checked above");
                            self.working = Some((compress_frame(encoder, data.clone()), data));
                            continue;
                        }
                        self.inline = self.inline.saturating_add(data.len());
                        self.since_yield += data.len();
                        let encoder = self.encoder.as_mut().expect("checked above");
                        encoder.write(&data);
                        let output = encoder.flush();
                        if output.is_empty() {
                            continue;
                        }
                        return Poll::Ready(Some(Ok(encoded_frame(output, data))));
                    }
                    Err(frame) => {
                        if let Ok(trailers) = frame.into_trailers() {
                            self.trailers = Some(trailers);
                        }
                    }
                },
                Some(Err(error)) => return Poll::Ready(Some(Err(error))),
                None => {
                    let tail = self.encoder.take().expect("checked above").finish();
                    if !tail.is_empty() {
                        return Poll::Ready(Some(Ok(Frame::data(Bytes::from(tail)))));
                    }
                }
            }
        }
    }

    fn is_end_stream(&self) -> bool {
        self.working.is_none() && self.encoder.is_none() && self.trailers.is_none()
    }

    fn size_hint(&self) -> SizeHint {
        SizeHint::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;

    fn config(value: Value) -> Compression {
        Compression::parse(&value).unwrap()
    }

    fn accept(value: &str) -> HeaderMap {
        let mut headers = HeaderMap::new();
        headers.insert(
            header::ACCEPT_ENCODING,
            HeaderValue::from_str(value).unwrap(),
        );
        headers
    }

    fn gunzip(data: &[u8]) -> Vec<u8> {
        let mut output = Vec::new();
        flate2::read::GzDecoder::new(data)
            .read_to_end(&mut output)
            .unwrap();
        output
    }

    fn unbrotli(data: &[u8]) -> Vec<u8> {
        let mut output = Vec::new();
        brotli::Decompressor::new(data, 4096)
            .read_to_end(&mut output)
            .unwrap();
        output
    }

    #[test]
    fn configuration_is_validated() {
        let all = config(serde_json::json!({}));
        assert!(all.gzip && all.br && all.min_size == 1024);
        for bad in [
            serde_json::json!({"gzip": false, "br": false}),
            serde_json::json!({"zstd": true}),
            serde_json::json!({"min_size": -1}),
            serde_json::json!({"gzip": "yes"}),
            serde_json::json!([]),
        ] {
            assert!(Compression::parse(&bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn negotiation_follows_quality_values() {
        let both = config(serde_json::json!({}));
        assert_eq!(both.negotiate(&accept("gzip, br")), Some(Coding::Brotli));
        assert_eq!(
            both.negotiate(&accept("gzip;q=1, br;q=0.5")),
            Some(Coding::Gzip)
        );
        assert_eq!(both.negotiate(&accept("br;q=0, gzip")), Some(Coding::Gzip));
        assert_eq!(both.negotiate(&accept("*")), Some(Coding::Brotli));
        assert_eq!(both.negotiate(&accept("*;q=0")), None);
        assert_eq!(both.negotiate(&accept("identity")), None);
        assert_eq!(both.negotiate(&HeaderMap::new()), None);
        let gzip_only = config(serde_json::json!({"br": false}));
        assert_eq!(
            gzip_only.negotiate(&accept("br, gzip;q=0.1")),
            Some(Coding::Gzip)
        );
        assert_eq!(gzip_only.negotiate(&accept("br")), None);
    }

    #[test]
    fn media_types_are_allow_listed() {
        for kind in [
            "text/html; charset=utf-8",
            "application/json",
            "application/problem+json",
            "image/svg+xml",
            "text/event-stream",
        ] {
            assert!(compressible_type(kind), "{kind}");
        }
        for kind in [
            "image/png",
            "application/zip",
            "application/octet-stream",
            "video/mp4",
            "nonsense",
        ] {
            assert!(!compressible_type(kind), "{kind}");
        }
    }

    /// A response byte budget that no test here fills.
    fn roomy() -> ByteBudget {
        ByteBudget::new(1 << 30)
    }

    fn response(body: Body, content_type: &str) -> Response {
        let mut response = Response::new(body);
        response.headers_mut().insert(
            header::CONTENT_TYPE,
            HeaderValue::from_str(content_type).unwrap(),
        );
        response
    }

    #[tokio::test]
    async fn buffered_bodies_round_trip_and_small_ones_are_left_alone() {
        let compression = config(serde_json::json!({"min_size": 16}));
        let text = "hello compression ".repeat(200);
        for coding in [Coding::Gzip, Coding::Brotli] {
            let mut original = response(Body::from(text.clone()), "text/plain");
            original
                .headers_mut()
                .insert(header::ETAG, HeaderValue::from_static("\"v1\""));
            let encoded = compression.apply(Some(coding), original, &roomy()).await;
            assert_eq!(encoded.headers()[header::CONTENT_ENCODING], coding.name());
            assert_eq!(encoded.headers()[header::VARY], "Accept-Encoding");
            assert_eq!(encoded.headers()[header::ETAG], "W/\"v1\"");
            let body = encoded.into_body().collect().await.unwrap().to_bytes();
            assert!(body.len() < text.len());
            let decoded = match coding {
                Coding::Gzip => gunzip(&body),
                Coding::Brotli => unbrotli(&body),
            };
            assert_eq!(decoded, text.as_bytes());
        }
        let small = compression
            .apply(
                Some(Coding::Gzip),
                response(Body::from("tiny"), "text/plain"),
                &roomy(),
            )
            .await;
        assert!(!small.headers().contains_key(header::CONTENT_ENCODING));
        assert_eq!(small.headers()[header::VARY], "Accept-Encoding");
        let image = compression
            .apply(
                Some(Coding::Gzip),
                response(Body::from(text.clone()), "image/png"),
                &roomy(),
            )
            .await;
        assert!(!image.headers().contains_key(header::CONTENT_ENCODING));
        assert!(!image.headers().contains_key(header::VARY));
        let mut opted_out = response(Body::from(text.clone()), "text/plain");
        opted_out.headers_mut().insert(
            header::CACHE_CONTROL,
            HeaderValue::from_static("public, no-transform"),
        );
        let opted_out = compression
            .apply(Some(Coding::Gzip), opted_out, &roomy())
            .await;
        assert!(!opted_out.headers().contains_key(header::CONTENT_ENCODING));
    }

    #[tokio::test]
    async fn streams_flush_every_frame() {
        let compression = config(serde_json::json!({}));
        let chunks = ["data: one\n\n", "data: two\n\n", "data: three\n\n"];
        let stream = futures_stream(
            chunks
                .iter()
                .map(|chunk| Bytes::from_static(chunk.as_bytes())),
        );
        for coding in [Coding::Gzip, Coding::Brotli] {
            let encoded = compression
                .apply(
                    Some(coding),
                    response(Body::new(stream.clone()), "text/event-stream"),
                    &roomy(),
                )
                .await;
            assert_eq!(encoded.headers()[header::CONTENT_ENCODING], coding.name());
            let mut body = encoded.into_body();
            let mut received = Vec::new();
            let mut frames = 0;
            while let Some(frame) = body.frame().await {
                let data = frame.unwrap().into_data().unwrap();
                received.extend_from_slice(&data);
                frames += 1;
                if frames <= chunks.len() {
                    // Each flushed prefix already decodes to everything sent so far.
                    let expected = chunks[..frames].concat();
                    let prefix = match coding {
                        Coding::Gzip => {
                            let mut output = Vec::new();
                            let _ = flate2::read::GzDecoder::new(&received[..])
                                .read_to_end(&mut output);
                            output
                        }
                        Coding::Brotli => {
                            let mut output = Vec::new();
                            let _ = brotli::Decompressor::new(&received[..], 4096)
                                .read_to_end(&mut output);
                            output
                        }
                    };
                    assert_eq!(prefix, expected.as_bytes());
                }
            }
            let decoded = match coding {
                Coding::Gzip => gunzip(&received),
                Coding::Brotli => unbrotli(&received),
            };
            assert_eq!(decoded, chunks.concat().as_bytes());
        }
    }

    #[tokio::test]
    async fn large_stream_frames_are_compressed_off_the_runtime_in_order() {
        let compression = config(serde_json::json!({}));
        let large: Vec<u8> = (0..300_000u32)
            .map(|i| b"abcdefgh"[(i % 7) as usize])
            .collect();
        let chunks = vec![
            Bytes::from_static(b"head "),
            Bytes::from(large.clone()),
            Bytes::from_static(b" middle "),
            Bytes::from(large.clone()),
            Bytes::from_static(b" tail"),
        ];
        let expected = chunks.concat();
        for coding in [Coding::Gzip, Coding::Brotli] {
            let encoded = compression
                .apply(
                    Some(coding),
                    response(
                        Body::new(futures_stream(chunks.clone().into_iter())),
                        "text/plain",
                    ),
                    &roomy(),
                )
                .await;
            let received = encoded.into_body().collect().await.unwrap().to_bytes();
            assert!(received.len() < expected.len() / 10);
            let decoded = match coding {
                Coding::Gzip => gunzip(&received),
                Coding::Brotli => unbrotli(&received),
            };
            assert_eq!(decoded, expected);
        }
    }

    #[tokio::test]
    async fn buffered_compression_respects_the_processing_deadline() {
        let compression = config(serde_json::json!({}));
        let text = "x".repeat(200_000);
        for (deadline, status) in [
            (
                Instant::now() - std::time::Duration::from_millis(1),
                StatusCode::GATEWAY_TIMEOUT,
            ),
            (
                Instant::now() + std::time::Duration::from_secs(60),
                StatusCode::OK,
            ),
        ] {
            let mut original = response(Body::from(text.clone()), "text/plain");
            original
                .extensions_mut()
                .insert(crate::ProcessingDeadline(deadline));
            let result = compression
                .apply(Some(Coding::Gzip), original, &roomy())
                .await;
            assert_eq!(result.status(), status);
            assert_eq!(
                result.headers().contains_key(header::CONTENT_ENCODING),
                status == StatusCode::OK
            );
        }
    }

    /// A body with an exact length that arrives in several frames and holds
    /// no budget of its own, as a file's does.
    fn file_like(text: &str) -> Response {
        struct Sized(std::collections::VecDeque<Bytes>, u64);

        impl http_body::Body for Sized {
            type Data = Bytes;
            type Error = std::convert::Infallible;

            fn poll_frame(
                mut self: Pin<&mut Self>,
                _: &mut Context<'_>,
            ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
                Poll::Ready(self.0.pop_front().map(|chunk| {
                    self.1 -= chunk.len() as u64;
                    Ok(Frame::data(chunk))
                }))
            }

            fn size_hint(&self) -> SizeHint {
                SizeHint::with_exact(self.1)
            }
        }

        let chunks = text
            .as_bytes()
            .chunks(4096)
            .map(Bytes::copy_from_slice)
            .collect();
        let mut response = response(Body::new(Sized(chunks, text.len() as u64)), "text/css");
        response.extensions_mut().insert(Unreserved);
        response
    }

    #[tokio::test]
    async fn a_file_compressed_whole_is_charged_to_the_response_budget_until_it_is_sent() {
        let compression = config(serde_json::json!({}));
        // Hard to compress, so the compressed form is a good part of it.
        let text: String = (0..40_000u32)
            .map(|index| char::from(b'a' + (index.wrapping_mul(2_654_435_761) >> 27) as u8 % 26))
            .collect();
        let budget = ByteBudget::new(100_000);
        let encoded = compression
            .apply(Some(Coding::Gzip), file_like(&text), &budget)
            .await;
        assert_eq!(encoded.headers()[header::CONTENT_ENCODING], "gzip");
        // Compressed whole: one frame of known length.
        assert!(encoded.body().size_hint().exact().is_some());
        assert_eq!(budget.available(), 100_000 - text.len());
        let body = encoded.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(gunzip(&body), text.as_bytes());
        assert_eq!(budget.available(), 100_000 - text.len());
        drop(body);
        assert_eq!(budget.available(), 100_000);

        // A response that already holds its own budget is not charged twice.
        let owned = budget.own(text.clone().into_bytes()).unwrap();
        let encoded = compression
            .apply(
                Some(Coding::Gzip),
                response(Body::from(owned), "text/css"),
                &budget,
            )
            .await;
        assert_eq!(encoded.headers()[header::CONTENT_ENCODING], "gzip");
        assert_eq!(budget.available(), 100_000 - text.len());
        drop(encoded);
        assert_eq!(budget.available(), 100_000);
    }

    #[tokio::test]
    async fn a_file_the_budget_cannot_take_is_compressed_as_a_stream() {
        let compression = config(serde_json::json!({}));
        let text = "body { margin: 0 } ".repeat(2_000);
        let budget = ByteBudget::new(text.len() - 1);
        let encoded = compression
            .apply(Some(Coding::Gzip), file_like(&text), &budget)
            .await;
        assert_eq!(encoded.headers()[header::CONTENT_ENCODING], "gzip");
        // A stream: its length is not known ahead, and nothing is reserved.
        assert!(encoded.body().size_hint().exact().is_none());
        assert_eq!(budget.available(), text.len() - 1);
        let body = encoded.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(gunzip(&body), text.as_bytes());
        assert_eq!(budget.available(), text.len() - 1);
    }

    struct Failing;

    impl http_body::Body for Failing {
        type Data = Bytes;
        type Error = std::io::Error;

        fn poll_frame(
            self: Pin<&mut Self>,
            _: &mut Context<'_>,
        ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
            Poll::Ready(Some(Err(std::io::Error::other("disk read failed"))))
        }

        fn size_hint(&self) -> SizeHint {
            SizeHint::with_exact(4096)
        }
    }

    #[tokio::test]
    async fn failed_buffered_bodies_are_errors_not_empty_successes() {
        let compression = config(serde_json::json!({}));
        let mut original = response(Body::new(Failing), "text/plain");
        original
            .headers_mut()
            .insert(header::ETAG, HeaderValue::from_static("\"tag\""));
        let result = compression
            .apply(Some(Coding::Gzip), original, &roomy())
            .await;
        assert_eq!(result.status(), StatusCode::INTERNAL_SERVER_ERROR);
        assert!(!result.headers().contains_key(header::ETAG));
    }

    fn text(len: usize) -> Bytes {
        let line = b"the quick brown fox jumps over the lazy dog 0123456789\n";
        Bytes::from(line.iter().copied().cycle().take(len).collect::<Vec<u8>>())
    }

    /// Holds every compression slot of the blocking pool: work sent there
    /// waits for one, and work done inline does not.
    async fn hold_slots() -> tokio::sync::OwnedSemaphorePermit {
        let count = u32::try_from(offload_slot_count()).unwrap();
        offload_slots()
            .clone()
            .acquire_many_owned(count)
            .await
            .unwrap()
    }

    fn poll_once<F: Future>(future: Pin<&mut F>) -> Poll<F::Output> {
        future.poll(&mut Context::from_waker(std::task::Waker::noop()))
    }

    fn poll_frame_once(body: &mut Body) -> Poll<Option<Result<Frame<Bytes>, axum::Error>>> {
        Pin::new(body).poll_frame(&mut Context::from_waker(std::task::Waker::noop()))
    }

    fn frame_data(polled: Poll<Option<Result<Frame<Bytes>, axum::Error>>>) -> Bytes {
        match polled {
            Poll::Ready(Some(Ok(frame))) => frame.into_data().unwrap(),
            _ => panic!("expected a data frame"),
        }
    }

    #[tokio::test]
    async fn bodies_and_frames_from_16_kib_are_compressed_on_the_blocking_pool() {
        let compression = config(serde_json::json!({}));
        let budget = roomy();
        let held = hold_slots().await;
        // Just under the threshold, a whole body is compressed inline...
        let small = std::pin::pin!(compression.apply(
            Some(Coding::Gzip),
            response(Body::from(text(OFFLOAD_MIN - 1)), "text/plain"),
            &budget,
        ));
        let Poll::Ready(small) = poll_once(small) else {
            panic!("a small body waited for the blocking pool");
        };
        let small = small.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(gunzip(&small), text(OFFLOAD_MIN - 1));
        // ...and at it, the body waits for a slot on the blocking pool.
        let mut large = std::pin::pin!(compression.apply(
            Some(Coding::Gzip),
            response(Body::from(text(OFFLOAD_MIN)), "text/plain"),
            &budget,
        ));
        assert!(poll_once(large.as_mut()).is_pending());

        // A stream: a 4 KiB frame inline, a file's 64 KiB chunk offloaded.
        let chunks = [text(4096), text(64 * 1024)];
        let mut stream = compression
            .apply(
                Some(Coding::Gzip),
                response(
                    Body::new(futures_stream(chunks.clone().into_iter())),
                    "text/plain",
                ),
                &roomy(),
            )
            .await
            .into_body();
        let mut received = frame_data(poll_frame_once(&mut stream)).to_vec();
        assert!(poll_frame_once(&mut stream).is_pending());

        drop(held);
        let large = large.await;
        assert_eq!(large.headers()[header::CONTENT_ENCODING], "gzip");
        let large = large.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(gunzip(&large), text(OFFLOAD_MIN));
        received.extend_from_slice(&stream.collect().await.unwrap().to_bytes());
        assert_eq!(gunzip(&received), chunks.concat());
    }

    #[tokio::test]
    async fn a_stream_of_medium_frames_is_offloaded_after_its_inline_share() {
        let compression = config(serde_json::json!({}));
        let frame = 8 * 1024;
        let chunks: Vec<Bytes> = (0..12).map(|_| text(frame)).collect();
        let held = hold_slots().await;
        let mut stream = compression
            .apply(
                Some(Coding::Gzip),
                response(
                    Body::new(futures_stream(chunks.clone().into_iter())),
                    "text/plain",
                ),
                &roomy(),
            )
            .await
            .into_body();
        let mut received = Vec::new();
        let mut inline = 0;
        let mut pending = 0;
        // A cooperative yield goes on at the next poll; a wait for a slot does not.
        while pending < 2 {
            match poll_frame_once(&mut stream) {
                Poll::Ready(Some(Ok(frame))) => {
                    received.extend_from_slice(&frame.into_data().unwrap());
                    inline += 1;
                    pending = 0;
                }
                Poll::Pending => pending += 1,
                Poll::Ready(other) => panic!("unexpected end: {:?}", other.is_some()),
            }
        }
        // Exactly the inline share was compressed while every slot was held.
        assert_eq!(inline * frame, STREAM_INLINE);
        drop(held);
        received.extend_from_slice(&stream.collect().await.unwrap().to_bytes());
        assert_eq!(gunzip(&received), chunks.concat());
    }

    #[tokio::test]
    async fn a_fast_stream_of_small_frames_yields_between_turns() {
        let compression = config(serde_json::json!({}));
        let chunks: Vec<Bytes> = (0..200).map(|_| text(512)).collect();
        // Small frames are never offloaded, even with every slot taken.
        let _held = hold_slots().await;
        let mut stream = compression
            .apply(
                Some(Coding::Brotli),
                response(
                    Body::new(futures_stream(chunks.clone().into_iter())),
                    "text/plain",
                ),
                &roomy(),
            )
            .await
            .into_body();
        let mut received = Vec::new();
        let mut yields = 0;
        loop {
            match poll_frame_once(&mut stream) {
                Poll::Ready(Some(Ok(frame))) => {
                    received.extend_from_slice(&frame.into_data().unwrap());
                }
                Poll::Ready(None) => break,
                Poll::Pending => yields += 1,
                Poll::Ready(Some(Err(error))) => panic!("{error}"),
            }
        }
        // 100 KiB of input, a yield after each 16 KiB compressed inline.
        assert_eq!(yields, 6);
        assert_eq!(unbrotli(&received), chunks.concat());
    }

    fn revalidation(
        status: StatusCode,
        etag: &'static str,
        headers: &[(&'static str, &'static str)],
    ) -> Response {
        let mut response = Response::new(Body::empty());
        *response.status_mut() = status;
        response
            .headers_mut()
            .insert(header::ETAG, HeaderValue::from_static(etag));
        for (name, value) in headers {
            response.headers_mut().insert(
                header::HeaderName::from_static(name),
                HeaderValue::from_static(value),
            );
        }
        response
    }

    /// A file's bodyless response standing for a `200` of `content_type`
    /// and `length`.
    fn of_file(
        status: StatusCode,
        content_type: &'static str,
        length: u64,
        encoded: bool,
        headers: &[(&'static str, &'static str)],
    ) -> Response {
        let mut response = revalidation(status, "\"v1\"", headers);
        response.extensions_mut().insert(Representation {
            content_type: Some(HeaderValue::from_static(content_type)),
            length,
            encoded,
            reselect: None,
        });
        response
    }

    #[tokio::test]
    async fn bodyless_file_responses_carry_the_validators_of_their_200() {
        let compression = config(serde_json::json!({}));
        let none: &[(&str, &str)] = &[];
        let no_transform: &[(&str, &str)] = &[("cache-control", "no-transform")];
        // (type, length, own encoding, headers, coding, the ETag, Vary)
        let cases = [
            // Compressed for this client: the weak tag of the compressed 200.
            (
                "text/css",
                4096,
                false,
                none,
                Some(Coding::Gzip),
                "W/\"v1\"",
                true,
            ),
            (
                "text/css",
                4096,
                false,
                none,
                Some(Coding::Brotli),
                "W/\"v1\"",
                true,
            ),
            // Not compressed for this client, or too small: the file's tag.
            ("text/css", 4096, false, none, None, "\"v1\"", true),
            (
                "text/css",
                10,
                false,
                none,
                Some(Coding::Gzip),
                "\"v1\"",
                true,
            ),
            // Never compressed: exactly as the file layer made it.
            (
                "image/png",
                4096,
                false,
                none,
                Some(Coding::Gzip),
                "\"v1\"",
                false,
            ),
            (
                "text/css",
                4096,
                true,
                none,
                Some(Coding::Gzip),
                "\"v1\"",
                false,
            ),
            (
                "text/css",
                4096,
                false,
                no_transform,
                Some(Coding::Gzip),
                "\"v1\"",
                false,
            ),
        ];
        for (content_type, length, encoded, headers, coding, etag, vary) in cases {
            for status in [StatusCode::NOT_MODIFIED, StatusCode::PARTIAL_CONTENT] {
                let response = of_file(status, content_type, length, encoded, headers);
                let response = compression.complete(coding, None, response, &roomy()).await;
                let case = format!("{status} {content_type} {length} {encoded} {coding:?}");
                // A range is of the identity representation: its strong tag.
                let etag = if status == StatusCode::PARTIAL_CONTENT {
                    "\"v1\""
                } else {
                    etag
                };
                assert_eq!(response.headers()[header::ETAG], etag, "{case}");
                assert_eq!(
                    response.headers().contains_key(header::VARY),
                    vary,
                    "{case}"
                );
                assert!(response.extensions().get::<Representation>().is_none());
            }
        }

        // An application's own 304 says nothing of its 200: left alone.
        let own = revalidation(StatusCode::NOT_MODIFIED, "\"v1\"", &[]);
        let weak = HeaderValue::from_static("W/\"v1\"");
        let own = compression
            .complete(Some(Coding::Gzip), Some(&weak), own, &roomy())
            .await;
        let own = compression.apply(Some(Coding::Gzip), own, &roomy()).await;
        assert_eq!(own.headers()[header::ETAG], "\"v1\"");
        assert!(!own.headers().contains_key(header::VARY));
    }

    #[derive(Clone)]
    struct Marker;

    #[tokio::test]
    async fn a_not_modified_stands_only_for_the_tag_sent_to_that_client() {
        let compression = config(serde_json::json!({}));
        let asked = Arc::new(Mutex::new(Vec::new()));
        let not_modified = |asked: &Arc<Mutex<Vec<bool>>>| {
            let mut response = of_file(StatusCode::NOT_MODIFIED, "text/css", 4096, false, &[]);
            response.extensions_mut().insert(Marker);
            let asked = asked.clone();
            response
                .extensions_mut()
                .get_mut::<Representation>()
                .unwrap()
                .reselect = Some(Reselect::new(move |weak| {
                asked.lock().unwrap().push(weak);
                let mut again = Response::new(Body::from("full"));
                again
                    .headers_mut()
                    .insert(header::CONTENT_TYPE, HeaderValue::from_static("text/css"));
                again
                    .headers_mut()
                    .insert(header::ETAG, HeaderValue::from_static("\"v1\""));
                again
            }));
            response
        };
        // (coding, If-None-Match, whether it is sent again, the weak argument)
        for (coding, if_none_match, again) in [
            (Some(Coding::Gzip), "W/\"v1\"", None),
            (Some(Coding::Gzip), "\"x\", W/\"v1\"", None),
            (Some(Coding::Gzip), "*", None),
            (None, "\"v1\"", None),
            // The client holds the identity representation, but is sent gzip.
            (Some(Coding::Gzip), "\"v1\"", Some(true)),
            // The client holds the compressed variant, but is sent identity.
            (None, "W/\"v1\"", Some(false)),
        ] {
            asked.lock().unwrap().clear();
            let value = HeaderValue::from_static(if_none_match);
            let response = compression
                .complete(coding, Some(&value), not_modified(&asked), &roomy())
                .await;
            let case = format!("{coding:?} {if_none_match}");
            assert_eq!(*asked.lock().unwrap(), Vec::from_iter(again), "{case}");
            let status = if again.is_some() {
                StatusCode::OK
            } else {
                StatusCode::NOT_MODIFIED
            };
            assert_eq!(response.status(), status, "{case}");
            assert!(response.extensions().get::<Marker>().is_some(), "{case}");
        }
    }

    #[test]
    fn if_none_match_lists_the_tag_in_the_form_sent() {
        let tag = |value: &'static str| HeaderValue::from_static(value);
        let strong = tag("\"v1\"");
        let weak = tag("W/\"v1\"");
        assert!(lists_sent_tag(Some(&tag("\"v1\"")), Some(&strong), false));
        assert!(!lists_sent_tag(
            Some(&tag("W/\"v1\"")),
            Some(&strong),
            false
        ));
        assert!(lists_sent_tag(
            Some(&tag(" W/\"v1\" ")),
            Some(&strong),
            true
        ));
        assert!(!lists_sent_tag(Some(&tag("\"v1\"")), Some(&strong), true));
        // A tag that is weak already is sent as it is.
        assert!(lists_sent_tag(Some(&tag("W/\"v1\"")), Some(&weak), false));
        assert!(!lists_sent_tag(Some(&tag("\"v1\"")), Some(&weak), false));
        assert!(lists_sent_tag(Some(&tag("\"a\", *")), Some(&strong), true));
        assert!(!lists_sent_tag(Some(&tag("\"v10\"")), Some(&strong), false));
    }

    #[test]
    fn head_carries_the_headers_of_the_get() {
        let compression = config(serde_json::json!({}));
        let head = |length: &'static str| {
            let mut response = revalidation(
                StatusCode::OK,
                "\"v1\"",
                &[
                    ("content-type", "text/css"),
                    ("content-length", length),
                    ("accept-ranges", "bytes"),
                ],
            );
            *response.body_mut() = Body::empty();
            response
        };
        let mut large = head("4096");
        assert!(compression.prepare_head(Some(Coding::Brotli), &mut large));
        let headers = large.headers();
        assert_eq!(headers[header::CONTENT_ENCODING], "br");
        assert_eq!(headers[header::ETAG], "W/\"v1\"");
        assert_eq!(headers[header::VARY], "Accept-Encoding");
        assert!(!headers.contains_key(header::CONTENT_LENGTH));
        assert!(!headers.contains_key(header::ACCEPT_RANGES));
        for (mut response, coding) in [(head("10"), Some(Coding::Gzip)), (head("4096"), None)] {
            assert!(!compression.prepare_head(coding, &mut response));
            let headers = response.headers();
            assert!(!headers.contains_key(header::CONTENT_ENCODING));
            assert_eq!(headers[header::ETAG], "\"v1\"");
            assert_eq!(headers[header::VARY], "Accept-Encoding");
            assert!(headers.contains_key(header::CONTENT_LENGTH));
        }
    }

    #[derive(Clone)]
    struct Chunks(std::collections::VecDeque<Bytes>);

    fn futures_stream(chunks: impl Iterator<Item = Bytes>) -> Chunks {
        Chunks(chunks.collect())
    }

    impl http_body::Body for Chunks {
        type Data = Bytes;
        type Error = std::convert::Infallible;

        fn poll_frame(
            mut self: Pin<&mut Self>,
            _: &mut Context<'_>,
        ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
            Poll::Ready(self.0.pop_front().map(|chunk| Ok(Frame::data(chunk))))
        }
    }
}
