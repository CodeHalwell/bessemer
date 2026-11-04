//! Native static file serving: `app.static("/assets", directory=...)`.
//!
//! Files are resolved under one canonical root (symlinks that leave it are
//! refused), validated with `ETag` and `Last-Modified`, answered with `304`
//! for conditional requests, sliced for single byte ranges, and served from
//! precompressed `.br` or `.gz` siblings when the client accepts them. Bodies
//! stream from disk in fixed-size chunks, so memory per response is bounded
//! whatever the file size.

use axum::{
    body::{Body, Bytes},
    http::{HeaderMap, HeaderValue, StatusCode, Uri, header},
    response::Response,
};
use http_body::{Frame, SizeHint};
use std::{
    io::SeekFrom,
    path::{Component, Path, PathBuf},
    pin::Pin,
    task::{Context, Poll, ready},
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::io::{AsyncRead, AsyncSeekExt, ReadBuf};

const CHUNK: usize = 64 * 1024;

#[derive(Debug)]
pub(crate) struct StaticDir {
    root: PathBuf,
    html: bool,
    cache_control: Option<HeaderValue>,
}

impl StaticDir {
    pub(crate) fn new(
        directory: &str,
        html: bool,
        cache_control: Option<&str>,
    ) -> Result<Self, String> {
        let root = std::fs::canonicalize(directory)
            .map_err(|_| format!("static directory does not exist: {directory}"))?;
        if !root.is_dir() {
            return Err(format!("static directory is not a directory: {directory}"));
        }
        let cache_control = cache_control
            .map(HeaderValue::from_str)
            .transpose()
            .map_err(|_| "static cache_control is not a valid header value".to_owned())?;
        Ok(Self {
            root,
            html,
            cache_control,
        })
    }

    pub(crate) async fn serve(&self, relative: &str, uri: &Uri, headers: &HeaderMap) -> Response {
        let Some(cleaned) = clean(relative) else {
            return not_found();
        };
        let Some((path, metadata, directory)) = self.resolve(&cleaned).await else {
            return not_found();
        };
        if directory && !relative.is_empty() && !relative.ends_with('/') {
            // Relative links in an index page resolve against its directory,
            // so the URL must end in a slash before the index is served.
            return redirect_to_directory(uri);
        }
        let mut response_headers = HeaderMap::new();
        response_headers.insert(header::CONTENT_TYPE, content_type(&path));
        if let Some(cache_control) = &self.cache_control {
            response_headers.insert(header::CACHE_CONTROL, cache_control.clone());
        }

        // Choose the representation first: validators, Vary and the body all
        // belong to it, and conditional requests are judged against it.
        let (variants_exist, encoded) = self.precompressed(&path, headers).await;
        if variants_exist {
            response_headers.append(header::VARY, HeaderValue::from_static("Accept-Encoding"));
        }
        let (file_path, length, modified, etag) = match &encoded {
            Some((coding, sibling, sibling_metadata)) => {
                response_headers.insert(header::CONTENT_ENCODING, HeaderValue::from_static(coding));
                // Each encoding is its own representation, validated by the
                // sibling's own metadata, so regenerating it changes its tag.
                let length = sibling_metadata.len();
                let modified = sibling_metadata.modified().ok();
                let tag = entity_tag(length, modified);
                let mut tag = tag.as_bytes()[..tag.len() - 1].to_vec();
                tag.extend_from_slice(format!("-{coding}\"").as_bytes());
                (
                    sibling,
                    length,
                    modified,
                    HeaderValue::from_bytes(&tag).unwrap(),
                )
            }
            None => {
                let modified = metadata.modified().ok();
                (
                    &path,
                    metadata.len(),
                    modified,
                    entity_tag(metadata.len(), modified),
                )
            }
        };
        if let Some(value) = modified
            .map(httpdate::fmt_http_date)
            .and_then(|value| HeaderValue::from_str(&value).ok())
        {
            response_headers.insert(header::LAST_MODIFIED, value);
        }
        response_headers.insert(header::ETAG, etag.clone());
        if not_modified(headers, &etag, modified) {
            response_headers.remove(header::CONTENT_TYPE);
            response_headers.remove(header::CONTENT_ENCODING);
            return with_headers(StatusCode::NOT_MODIFIED, response_headers, Body::empty());
        }
        if encoded.is_none() {
            // Byte ranges are offered on the identity representation only.
            response_headers.insert(header::ACCEPT_RANGES, HeaderValue::from_static("bytes"));
            let range = headers
                .get(header::RANGE)
                .filter(|_| if_range_matches(headers, &etag, modified))
                .and_then(|value| value.to_str().ok());
            if let Some(range) = range {
                match parse_range(range, length) {
                    Range::Satisfiable(start, end) => {
                        let Ok(file) = open_at(file_path, start).await else {
                            return not_found();
                        };
                        response_headers.insert(
                            header::CONTENT_RANGE,
                            HeaderValue::from_str(&format!("bytes {start}-{end}/{length}"))
                                .unwrap(),
                        );
                        let body = FileBody::new(file, end - start + 1);
                        return with_headers(
                            StatusCode::PARTIAL_CONTENT,
                            response_headers,
                            Body::new(body),
                        );
                    }
                    Range::Unsatisfiable => {
                        response_headers.remove(header::CONTENT_TYPE);
                        response_headers.insert(
                            header::CONTENT_RANGE,
                            HeaderValue::from_str(&format!("bytes */{length}")).unwrap(),
                        );
                        return with_headers(
                            StatusCode::RANGE_NOT_SATISFIABLE,
                            response_headers,
                            Body::empty(),
                        );
                    }
                    Range::Ignored => {}
                }
            }
        }
        let Ok(file) = open_at(file_path, 0).await else {
            return not_found();
        };
        with_headers(
            StatusCode::OK,
            response_headers,
            Body::new(FileBody::new(file, length)),
        )
    }

    /// Whether `.br`/`.gz` siblings exist, and the one the client accepts.
    async fn precompressed(
        &self,
        path: &Path,
        headers: &HeaderMap,
    ) -> (bool, Option<(&'static str, PathBuf, std::fs::Metadata)>) {
        let mut variants_exist = false;
        for (coding, suffix) in [("br", ".br"), ("gzip", ".gz")] {
            let mut sibling = path.to_path_buf().into_os_string();
            sibling.push(suffix);
            let sibling = PathBuf::from(sibling);
            let Ok(metadata) = tokio::fs::metadata(&sibling).await else {
                continue;
            };
            if !metadata.is_file() || !self.contains(&sibling).await {
                continue;
            }
            variants_exist = true;
            if accepts(headers, coding) {
                return (true, Some((coding, sibling, metadata)));
            }
        }
        (variants_exist, None)
    }

    /// The canonical file for a cleaned relative path, inside the root.
    async fn resolve(&self, relative: &Path) -> Option<(PathBuf, std::fs::Metadata, bool)> {
        let mut path = tokio::fs::canonicalize(self.root.join(relative))
            .await
            .ok()?;
        if !self.serves(&path) {
            return None;
        }
        let mut metadata = tokio::fs::metadata(&path).await.ok()?;
        let directory = metadata.is_dir();
        if directory {
            if !self.html {
                return None;
            }
            path = tokio::fs::canonicalize(path.join("index.html"))
                .await
                .ok()?;
            if !self.serves(&path) {
                return None;
            }
            metadata = tokio::fs::metadata(&path).await.ok()?;
        }
        metadata.is_file().then_some((path, metadata, directory))
    }

    /// A canonical path is servable when it lies under the root and no part
    /// of it below the root is hidden, so a visible symlink cannot expose a
    /// dotfile such as `.env` or a `.git` directory.
    fn serves(&self, canonical: &Path) -> bool {
        canonical.strip_prefix(&self.root).is_ok_and(|inside| {
            inside
                .components()
                .all(|part| !part.as_os_str().as_encoded_bytes().starts_with(b"."))
        })
    }

    async fn contains(&self, path: &Path) -> bool {
        tokio::fs::canonicalize(path)
            .await
            .is_ok_and(|path| self.serves(&path))
    }
}

/// Only plain, visible path segments; anything else is treated as absent.
fn clean(relative: &str) -> Option<PathBuf> {
    if relative.contains(['\0', '\\']) {
        return None;
    }
    let mut cleaned = PathBuf::new();
    for component in Path::new(relative).components() {
        match component {
            Component::Normal(segment) => {
                // Dotfiles such as .env and .git are never served.
                if segment.as_encoded_bytes().first() == Some(&b'.') {
                    return None;
                }
                cleaned.push(segment);
            }
            Component::CurDir => {}
            _ => return None,
        }
    }
    Some(cleaned)
}

fn with_headers(status: StatusCode, headers: HeaderMap, body: Body) -> Response {
    let mut response = Response::new(body);
    *response.status_mut() = status;
    *response.headers_mut() = headers;
    response
}

fn redirect_to_directory(uri: &Uri) -> Response {
    let mut location = format!("{}/", uri.path());
    if let Some(query) = uri.query() {
        location.push('?');
        location.push_str(query);
    }
    let mut response = Response::new(Body::empty());
    *response.status_mut() = StatusCode::TEMPORARY_REDIRECT;
    if let Ok(value) = HeaderValue::from_str(&location) {
        response.headers_mut().insert(header::LOCATION, value);
    }
    response
}

fn not_found() -> Response {
    crate::error(StatusCode::NOT_FOUND, "Not Found")
}

async fn open_at(path: &Path, offset: u64) -> std::io::Result<tokio::fs::File> {
    let mut file = tokio::fs::File::open(path).await?;
    if offset > 0 {
        file.seek(SeekFrom::Start(offset)).await?;
    }
    Ok(file)
}

fn entity_tag(length: u64, modified: Option<SystemTime>) -> HeaderValue {
    let stamp = modified
        .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
        .map_or(0, |duration| duration.as_nanos());
    HeaderValue::from_str(&format!("\"{length:x}-{stamp:x}\"")).unwrap()
}

fn weak_equal(a: &[u8], b: &[u8]) -> bool {
    let strip = |tag: &[u8]| tag.strip_prefix(b"W/").map_or(tag.to_vec(), <[u8]>::to_vec);
    strip(a) == strip(b)
}

fn whole_seconds(time: SystemTime) -> u64 {
    time.duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_secs())
}

fn not_modified(headers: &HeaderMap, etag: &HeaderValue, modified: Option<SystemTime>) -> bool {
    if let Some(value) = headers.get(header::IF_NONE_MATCH) {
        // If-None-Match takes precedence over If-Modified-Since.
        return value
            .as_bytes()
            .split(|byte| *byte == b',')
            .any(|candidate| {
                let candidate = candidate.trim_ascii();
                candidate == b"*" || weak_equal(candidate, etag.as_bytes())
            });
    }
    let (Some(since), Some(modified)) = (
        headers
            .get(header::IF_MODIFIED_SINCE)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| httpdate::parse_http_date(value).ok()),
        modified,
    ) else {
        return false;
    };
    whole_seconds(modified) <= whole_seconds(since)
}

fn if_range_matches(headers: &HeaderMap, etag: &HeaderValue, modified: Option<SystemTime>) -> bool {
    let Some(value) = headers.get(header::IF_RANGE) else {
        return true;
    };
    let bytes = value.as_bytes();
    if bytes.starts_with(b"\"") {
        // A strong comparison: a weak tag never matches.
        return bytes == etag.as_bytes();
    }
    match (
        value
            .to_str()
            .ok()
            .and_then(|v| httpdate::parse_http_date(v).ok()),
        modified,
    ) {
        (Some(date), Some(modified)) => whole_seconds(modified) == whole_seconds(date),
        _ => false,
    }
}

#[derive(Debug, PartialEq, Eq)]
enum Range {
    Satisfiable(u64, u64),
    Unsatisfiable,
    /// Malformed or multi-range requests get the whole file, as RFC 9110 allows.
    Ignored,
}

fn parse_range(value: &str, length: u64) -> Range {
    let Some(spec) = value.trim().strip_prefix("bytes=") else {
        return Range::Ignored;
    };
    if spec.contains(',') {
        return Range::Ignored;
    }
    let Some((start, end)) = spec.trim().split_once('-') else {
        return Range::Ignored;
    };
    let (start, end) = (start.trim(), end.trim());
    let digits = |text: &str| !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit());
    if start.is_empty() {
        // A suffix range: the last N bytes.
        if !digits(end) {
            return Range::Ignored;
        }
        let Ok(suffix) = end.parse::<u64>() else {
            return Range::Ignored;
        };
        if suffix == 0 || length == 0 {
            return Range::Unsatisfiable;
        }
        return Range::Satisfiable(length.saturating_sub(suffix), length - 1);
    }
    if !digits(start) || !(end.is_empty() || digits(end)) {
        return Range::Ignored;
    }
    let Ok(start) = start.parse::<u64>() else {
        return Range::Unsatisfiable;
    };
    if start >= length {
        return Range::Unsatisfiable;
    }
    let end = if end.is_empty() {
        length - 1
    } else {
        match end.parse::<u64>() {
            Ok(end) if end >= start => end.min(length - 1),
            Ok(_) => return Range::Ignored,
            Err(_) => length - 1,
        }
    };
    Range::Satisfiable(start, end)
}

/// Whether `Accept-Encoding` allows a coding with a nonzero quality.
fn accepts(headers: &HeaderMap, coding: &str) -> bool {
    let mut explicit = None;
    let mut any = None;
    for value in headers.get_all(header::ACCEPT_ENCODING) {
        let Ok(value) = value.to_str() else { continue };
        for item in value.split(',') {
            let mut parts = item.split(';');
            let name = parts.next().unwrap_or("").trim();
            let quality = parts
                .filter_map(|parameter| {
                    let parameter = parameter.trim();
                    parameter
                        .strip_prefix("q=")
                        .or_else(|| parameter.strip_prefix("Q="))
                })
                .next_back()
                .map_or(1.0, |raw| raw.trim().parse::<f32>().unwrap_or(0.0));
            if name.eq_ignore_ascii_case(coding) {
                explicit = Some(quality);
            } else if name == "*" {
                any = Some(quality);
            }
        }
    }
    explicit.or(any).is_some_and(|quality| quality > 0.0)
}

/// Media types by extension; a small table instead of a MIME database crate.
fn content_type(path: &Path) -> HeaderValue {
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    HeaderValue::from_static(match extension.as_str() {
        "html" | "htm" => "text/html; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "json" | "map" => "application/json",
        "webmanifest" => "application/manifest+json",
        "txt" => "text/plain; charset=utf-8",
        "md" => "text/markdown; charset=utf-8",
        "csv" => "text/csv; charset=utf-8",
        "xml" => "application/xml",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "avif" => "image/avif",
        "ico" => "image/x-icon",
        "woff" => "font/woff",
        "woff2" => "font/woff2",
        "ttf" => "font/ttf",
        "otf" => "font/otf",
        "wasm" => "application/wasm",
        "pdf" => "application/pdf",
        "zip" => "application/zip",
        "gz" => "application/gzip",
        "mp4" => "video/mp4",
        "webm" => "video/webm",
        "mp3" => "audio/mpeg",
        "ogg" => "audio/ogg",
        "wav" => "audio/wav",
        _ => "application/octet-stream",
    })
}

/// A file body read in fixed-size chunks, with an exact length.
struct FileBody {
    file: tokio::fs::File,
    remaining: u64,
    buffer: Vec<u8>,
}

impl FileBody {
    fn new(file: tokio::fs::File, length: u64) -> Self {
        Self {
            file,
            remaining: length,
            buffer: Vec::new(),
        }
    }
}

impl http_body::Body for FileBody {
    type Data = Bytes;
    type Error = std::io::Error;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
        if self.remaining == 0 {
            return Poll::Ready(None);
        }
        let want = usize::try_from(self.remaining).map_or(CHUNK, |left| left.min(CHUNK));
        let this = &mut *self;
        if this.buffer.len() != want {
            this.buffer.resize(want, 0);
        }
        let mut read = ReadBuf::new(&mut this.buffer);
        ready!(Pin::new(&mut this.file).poll_read(cx, &mut read))?;
        let filled = read.filled().len();
        if filled == 0 {
            // The file shrank after its length was read; end with an error
            // rather than a short body that claims to be complete.
            return Poll::Ready(Some(Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "static file truncated while serving",
            ))));
        }
        this.remaining -= filled as u64;
        let chunk = Bytes::copy_from_slice(&this.buffer[..filled]);
        Poll::Ready(Some(Ok(Frame::data(chunk))))
    }

    fn is_end_stream(&self) -> bool {
        self.remaining == 0
    }

    fn size_hint(&self) -> SizeHint {
        SizeHint::with_exact(self.remaining)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paths_stay_plain_and_visible() {
        assert_eq!(clean("css/site.css"), Some(PathBuf::from("css/site.css")));
        assert_eq!(clean("./a/./b"), Some(PathBuf::from("a/b")));
        for bad in [
            "../secret",
            "a/../../b",
            "/etc/passwd",
            ".env",
            "a/.git/config",
            "a\\b",
            "a\0b",
        ] {
            assert_eq!(clean(bad), None, "{bad}");
        }
    }

    #[test]
    fn ranges_follow_rfc_9110() {
        assert_eq!(parse_range("bytes=0-9", 100), Range::Satisfiable(0, 9));
        assert_eq!(parse_range("bytes=90-", 100), Range::Satisfiable(90, 99));
        assert_eq!(parse_range("bytes=-10", 100), Range::Satisfiable(90, 99));
        assert_eq!(parse_range("bytes=-500", 100), Range::Satisfiable(0, 99));
        assert_eq!(parse_range("bytes=50-500", 100), Range::Satisfiable(50, 99));
        assert_eq!(parse_range("bytes=100-", 100), Range::Unsatisfiable);
        assert_eq!(parse_range("bytes=-0", 100), Range::Unsatisfiable);
        assert_eq!(parse_range("bytes=0-1,5-6", 100), Range::Ignored);
        assert_eq!(parse_range("items=0-1", 100), Range::Ignored);
        assert_eq!(parse_range("bytes=5-1", 100), Range::Ignored);
        assert_eq!(parse_range("bytes=a-b", 100), Range::Ignored);
    }

    #[test]
    fn encodings_need_a_nonzero_quality() {
        let mut headers = HeaderMap::new();
        headers.insert(
            header::ACCEPT_ENCODING,
            HeaderValue::from_static("gzip;q=0, br"),
        );
        assert!(accepts(&headers, "br"));
        assert!(!accepts(&headers, "gzip"));
        headers.insert(header::ACCEPT_ENCODING, HeaderValue::from_static("*"));
        assert!(accepts(&headers, "gzip"));
        assert!(!accepts(&HeaderMap::new(), "gzip"));
    }

    #[test]
    fn validators_compare_weakly_for_not_modified() {
        let etag = HeaderValue::from_static("\"10-20\"");
        let mut headers = HeaderMap::new();
        headers.insert(
            header::IF_NONE_MATCH,
            HeaderValue::from_static("\"x\", W/\"10-20\""),
        );
        assert!(not_modified(&headers, &etag, None));
        headers.insert(header::IF_NONE_MATCH, HeaderValue::from_static("\"x\""));
        assert!(!not_modified(&headers, &etag, None));
        headers.insert(header::IF_RANGE, HeaderValue::from_static("W/\"10-20\""));
        assert!(!if_range_matches(&headers, &etag, None));
    }
}
