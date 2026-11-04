//! Native response compression negotiated from `Accept-Encoding`.
//!
//! Buffered bodies are compressed whole, and kept only when that saves bytes.
//! Streaming bodies are compressed frame by frame with a sync flush after each
//! frame, so server-sent events and other incremental responses still arrive
//! promptly. Compressed output owns the original bytes it replaced, which
//! keeps the response byte budget charged until Hyper releases the data.

use axum::{
    body::{Body, Bytes},
    http::{HeaderMap, HeaderValue, Method, StatusCode, header},
    response::Response,
};
use http_body::{Body as _, Frame, SizeHint};
use http_body_util::BodyExt;
use serde_json::Value;
use std::{
    io::Write,
    pin::Pin,
    task::{Context, Poll, ready},
};

/// Bodies above this size are compressed on the blocking pool.
const INLINE_LIMIT: usize = 64 * 1024;
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
    pub(crate) fn negotiate(&self, method: &Method, accept: &HeaderMap) -> Option<Coding> {
        if method == Method::HEAD {
            return None;
        }
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

    /// Compress a response for the negotiated coding when that is safe.
    pub(crate) async fn apply(&self, coding: Option<Coding>, response: Response) -> Response {
        if !compressible(&response) {
            return response;
        }
        let mut response = response;
        append_vary(response.headers_mut());
        let Some(coding) = coding else {
            return response;
        };
        let hint = response.body().size_hint();
        match hint.exact() {
            Some(size) if size < self.min_size.max(1) => response,
            Some(_) => buffered(coding, response).await,
            None => streamed(coding, response),
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
    if headers
        .get_all(header::CACHE_CONTROL)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .any(|value| {
            value
                .split(',')
                .any(|directive| directive.trim().eq_ignore_ascii_case("no-transform"))
        })
    {
        return false;
    }
    headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .is_some_and(compressible_type)
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

async fn buffered(coding: Coding, response: Response) -> Response {
    let (mut parts, body) = response.into_parts();
    let original = match body.collect().await {
        Ok(collected) => collected.to_bytes(),
        Err(_) => {
            return Response::from_parts(parts, Body::empty());
        }
    };
    let compressed = if original.len() > INLINE_LIMIT {
        let input = original.clone();
        match tokio::task::spawn_blocking(move || compress_all(coding, &input)).await {
            Ok(compressed) => compressed,
            Err(_) => return Response::from_parts(parts, Body::from(original)),
        }
    } else {
        compress_all(coding, &original)
    };
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
        trailers: None,
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

struct Streamed {
    inner: Body,
    encoder: Option<Encoder>,
    trailers: Option<HeaderMap>,
}

impl http_body::Body for Streamed {
    type Data = Bytes;
    type Error = axum::Error;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
        loop {
            if self.encoder.is_none() {
                return Poll::Ready(self.trailers.take().map(|t| Ok(Frame::trailers(t))));
            }
            match ready!(Pin::new(&mut self.inner).poll_frame(cx)) {
                Some(Ok(frame)) => match frame.into_data() {
                    Ok(data) => {
                        if data.is_empty() {
                            continue;
                        }
                        let encoder = self.encoder.as_mut().expect("checked above");
                        encoder.write(&data);
                        let output = encoder.flush();
                        if output.is_empty() {
                            continue;
                        }
                        return Poll::Ready(Some(Ok(Frame::data(Bytes::from_owner(Encoded {
                            data: output,
                            _original: data,
                        })))));
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
        self.encoder.is_none() && self.trailers.is_none()
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
        let get = Method::GET;
        assert_eq!(
            both.negotiate(&get, &accept("gzip, br")),
            Some(Coding::Brotli)
        );
        assert_eq!(
            both.negotiate(&get, &accept("gzip;q=1, br;q=0.5")),
            Some(Coding::Gzip)
        );
        assert_eq!(
            both.negotiate(&get, &accept("br;q=0, gzip")),
            Some(Coding::Gzip)
        );
        assert_eq!(both.negotiate(&get, &accept("*")), Some(Coding::Brotli));
        assert_eq!(both.negotiate(&get, &accept("*;q=0")), None);
        assert_eq!(both.negotiate(&get, &accept("identity")), None);
        assert_eq!(both.negotiate(&get, &HeaderMap::new()), None);
        assert_eq!(both.negotiate(&Method::HEAD, &accept("gzip")), None);
        let gzip_only = config(serde_json::json!({"br": false}));
        assert_eq!(
            gzip_only.negotiate(&get, &accept("br, gzip;q=0.1")),
            Some(Coding::Gzip)
        );
        assert_eq!(gzip_only.negotiate(&get, &accept("br")), None);
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
            let encoded = compression.apply(Some(coding), original).await;
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
            )
            .await;
        assert!(!small.headers().contains_key(header::CONTENT_ENCODING));
        assert_eq!(small.headers()[header::VARY], "Accept-Encoding");
        let image = compression
            .apply(
                Some(Coding::Gzip),
                response(Body::from(text.clone()), "image/png"),
            )
            .await;
        assert!(!image.headers().contains_key(header::CONTENT_ENCODING));
        assert!(!image.headers().contains_key(header::VARY));
        let mut opted_out = response(Body::from(text.clone()), "text/plain");
        opted_out.headers_mut().insert(
            header::CACHE_CONTROL,
            HeaderValue::from_static("public, no-transform"),
        );
        let opted_out = compression.apply(Some(Coding::Gzip), opted_out).await;
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
