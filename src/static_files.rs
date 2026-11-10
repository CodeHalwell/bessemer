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
    fs::File,
    io::SeekFrom,
    path::{Component, Path, PathBuf},
    pin::Pin,
    sync::Arc,
    task::{Context, Poll, ready},
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::io::{AsyncRead, AsyncSeekExt, ReadBuf};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

const CHUNK: usize = 64 * 1024;

#[derive(Debug)]
pub(crate) struct StaticDir {
    root: PathBuf,
    root_fd: Arc<RootHandle>,
    html: bool,
    cache_control: Option<HeaderValue>,
    /// One permit per response of this mount that is in progress. Static
    /// responses run no handler and so take no admission slot; without a
    /// bound, HTTP/2 streams could each hold a file open and exhaust the
    /// process's descriptors with a handful of connections.
    slots: Arc<Semaphore>,
}

impl StaticDir {
    /// `max_open` is how many responses of this mount may be in progress at
    /// once: the server's connection limit, so HTTP/1 is never affected and
    /// HTTP/2 cannot hold more files open than there may be connections.
    pub(crate) fn new(
        directory: &str,
        html: bool,
        cache_control: Option<&str>,
        max_open: usize,
    ) -> Result<Self, String> {
        let root = std::fs::canonicalize(directory)
            .map_err(|_| format!("static directory does not exist: {directory}"))?;
        if !root.is_dir() {
            return Err(format!("static directory is not a directory: {directory}"));
        }
        let root_fd = pinned_directory(&root)
            .map_err(|_| format!("static directory cannot be opened: {directory}"))?;
        let cache_control = cache_control
            .map(HeaderValue::from_str)
            .transpose()
            .map_err(|_| "static cache_control is not a valid header value".to_owned())?;
        Ok(Self {
            root,
            root_fd: Arc::new(root_fd),
            html,
            cache_control,
            slots: Arc::new(Semaphore::new(max_open.clamp(1, Semaphore::MAX_PERMITS))),
        })
    }

    pub(crate) async fn serve(&self, relative: &str, uri: &Uri, headers: &HeaderMap) -> Response {
        let Some(cleaned) = clean(relative) else {
            return not_found();
        };
        // Taken before any file is opened and held by the body until it has
        // been sent or dropped.
        let Ok(slot) = self.slots.clone().try_acquire_owned() else {
            return crate::capacity_response("Static file capacity is full", false);
        };
        let Some((identity, metadata, directory, logical)) = self.resolve(&cleaned).await else {
            return not_found();
        };
        if directory && !relative.is_empty() && !relative.ends_with('/') {
            // Relative links in an index page resolve against its directory,
            // so the URL must end in a slash before the index is served.
            return redirect_to_directory(uri);
        }
        let mut response_headers = HeaderMap::new();
        response_headers.insert(header::CONTENT_TYPE, content_type(&logical));
        if let Some(cache_control) = &self.cache_control {
            response_headers.insert(header::CACHE_CONTROL, cache_control.clone());
        }

        // Choose the representation first: validators, Vary and the body all
        // belong to it, and conditional requests are judged against it.
        let (variants_exist, encoded) = self.precompressed(&logical, headers).await;
        let has_encoding = encoded.is_some();
        if variants_exist {
            response_headers.append(header::VARY, HeaderValue::from_static("Accept-Encoding"));
        }
        let (mut file, length, modified, etag) = match encoded {
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
                    identity,
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
        if !has_encoding {
            // Byte ranges are offered on the identity representation only.
            response_headers.insert(header::ACCEPT_RANGES, HeaderValue::from_static("bytes"));
            let range = headers
                .get(header::RANGE)
                .filter(|_| if_range_matches(headers, &etag, modified))
                .and_then(|value| value.to_str().ok());
            if let Some(range) = range {
                match parse_range(range, length) {
                    Range::Satisfiable(start, end) => {
                        if file.seek(SeekFrom::Start(start)).await.is_err() {
                            return not_found();
                        }
                        response_headers.insert(
                            header::CONTENT_RANGE,
                            HeaderValue::from_str(&format!("bytes {start}-{end}/{length}"))
                                .unwrap(),
                        );
                        let body = FileBody::new(file, end - start + 1).holding(slot);
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
        let mut response = with_headers(
            StatusCode::OK,
            response_headers,
            Body::new(FileBody::new(file, length).holding(slot)),
        );
        response.extensions_mut().insert(crate::budget::Unreserved);
        response
    }

    /// Whether `.br`/`.gz` siblings exist, and the one the client accepts.
    async fn precompressed(
        &self,
        path: &Path,
        headers: &HeaderMap,
    ) -> (
        bool,
        Option<(&'static str, tokio::fs::File, std::fs::Metadata)>,
    ) {
        let mut variants_exist = false;
        for (coding, suffix) in [("br", ".br"), ("gzip", ".gz")] {
            let mut sibling = path.to_path_buf().into_os_string();
            sibling.push(suffix);
            let sibling = PathBuf::from(sibling);
            let Some(canonical) = self.canonical_contained(&sibling).await else {
                continue;
            };
            let Ok((file, metadata)) = self.open_file(&canonical).await else {
                continue;
            };
            variants_exist = true;
            if accepts(headers, coding) {
                return (true, Some((coding, file, metadata)));
            }
        }
        (variants_exist, None)
    }

    /// The canonical file for a cleaned relative path, inside the root.
    /// Returns the canonical file (for access and containment), its metadata,
    /// whether a directory index was chosen, and the logical path as requested
    /// (for the media type and precompressed siblings), since a symlink's
    /// target may carry a different name.
    async fn resolve(
        &self,
        relative: &Path,
    ) -> Option<(tokio::fs::File, std::fs::Metadata, bool, PathBuf)> {
        let mut logical = self.root.join(relative);
        let mut path = tokio::fs::canonicalize(&logical).await.ok()?;
        if !self.serves(&path) {
            return None;
        }
        let metadata = tokio::fs::metadata(&path).await.ok()?;
        let directory = metadata.is_dir();
        if directory {
            if !self.html {
                return None;
            }
            logical.push("index.html");
            path = tokio::fs::canonicalize(path.join("index.html"))
                .await
                .ok()?;
            if !self.serves(&path) {
                return None;
            }
        }
        let (file, metadata) = self.open_file(&path).await.ok()?;
        Some((file, metadata, directory, logical))
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

    async fn canonical_contained(&self, path: &Path) -> Option<PathBuf> {
        tokio::fs::canonicalize(path)
            .await
            .ok()
            .filter(|path| self.serves(path))
    }

    async fn open_file(
        &self,
        path: &Path,
    ) -> std::io::Result<(tokio::fs::File, std::fs::Metadata)> {
        let relative = path
            .strip_prefix(&self.root)
            .map_err(std::io::Error::other)?
            .to_path_buf();
        let root = self.root_fd.clone();
        let (file, metadata) = tokio::task::spawn_blocking(move || open_beneath(&root, &relative))
            .await
            .map_err(std::io::Error::other)??;
        Ok((tokio::fs::File::from_std(file), metadata))
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

pub(crate) fn redirect_to_directory(uri: &Uri) -> Response {
    // A Location starting `//`, or `/\` which browsers read the same way, is
    // a protocol-relative URL that would send the client to another host.
    if protocol_relative(uri.path()) {
        return not_found();
    }
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

/// Whether a browser would resolve `path`, used as a Location, against
/// another host.
pub(crate) fn protocol_relative(path: &str) -> bool {
    matches!(path.as_bytes().get(1), Some(b'/' | b'\\'))
}

fn not_found() -> Response {
    crate::error(StatusCode::NOT_FOUND, "Not Found")
}

type OpenResponseFile = (File, std::fs::Metadata, Option<u64>);
type RawFileHeaders = Vec<(Vec<u8>, Vec<u8>)>;

/// An open file returned from Python's `FileResponse` to be served natively.
#[derive(Clone)]
#[pyo3::pyclass(module = "bessemer._native", name = "_ResponseFile", from_py_object)]
pub(crate) struct PendingFile(Arc<std::sync::Mutex<Option<OpenResponseFile>>>);

impl PendingFile {
    pub(crate) fn take(&self) -> Option<(File, std::fs::Metadata)> {
        self.0
            .lock()
            .unwrap()
            .take()
            .map(|(file, metadata, _)| (file, metadata))
    }
}

#[pyo3::pymethods]
impl PendingFile {
    #[new]
    fn py_new(path: &str) -> pyo3::PyResult<Self> {
        let file = File::open(path).map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("file response cannot be opened")
        })?;
        let metadata = file.metadata().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("file response cannot be stat'd")
        })?;
        if !metadata.is_file() {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "file response path is not a regular file",
            ));
        }
        Ok(Self(Arc::new(std::sync::Mutex::new(Some((
            file, metadata, None,
        ))))))
    }

    fn prepare(
        &self,
        py: pyo3::Python<'_>,
        status: u16,
        headers: RawFileHeaders,
        request_headers: RawFileHeaders,
        head: bool,
    ) -> pyo3::PyResult<(u16, RawFileHeaders)> {
        py.detach(|| {
            let parse = |headers: RawFileHeaders| -> pyo3::PyResult<HeaderMap> {
                let mut result = HeaderMap::new();
                for (name, value) in headers {
                    let name = header::HeaderName::from_bytes(&name)
                        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
                    let value = HeaderValue::from_bytes(&value)
                        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
                    result.append(name, value);
                }
                Ok(result)
            };
            let mut response = Response::new(Body::empty());
            *response.status_mut() = StatusCode::from_u16(status)
                .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
            *response.headers_mut() = parse(headers)?;
            let mut guard = self.0.lock().unwrap();
            let Some((file, metadata, remaining)) = guard.as_mut() else {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(
                    "file response is closed",
                ));
            };
            let (mut response, selected) =
                select_response_file(metadata, response, &parse(request_headers)?, head);
            *remaining = Some(0);
            if let Some((start, length)) = selected {
                use std::io::Seek;
                file.seek(SeekFrom::Start(start))
                    .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
                *remaining = Some(length);
                response
                    .headers_mut()
                    .insert(header::CONTENT_LENGTH, HeaderValue::from(length));
            }
            Ok((
                response.status().as_u16(),
                response
                    .headers()
                    .iter()
                    .map(|(name, value)| {
                        (name.as_str().as_bytes().to_vec(), value.as_bytes().to_vec())
                    })
                    .collect(),
            ))
        })
    }

    fn read_chunk(&self, py: pyo3::Python<'_>) -> pyo3::PyResult<Vec<u8>> {
        py.detach(|| {
            use std::io::Read;
            let mut guard = self.0.lock().unwrap();
            let Some((file, _, remaining)) = guard.as_mut() else {
                return Ok(Vec::new());
            };
            let length = remaining.map_or(CHUNK, |n| n.min(CHUNK as u64) as usize);
            let mut buf = vec![0u8; length];
            let n = file
                .read(&mut buf)
                .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
            if let Some(remaining) = remaining {
                *remaining -= n as u64;
            }
            buf.truncate(n);
            Ok(buf)
        })
    }

    fn close(&self, py: pyo3::Python<'_>) {
        py.detach(|| self.0.lock().unwrap().take());
    }
}

/// Serve a route handler's `FileResponse` on the native static-file path,
/// supporting `If-None-Match`, `If-Modified-Since`, `If-Range`, and single
/// byte ranges (`206 Partial Content`, `416 Range Not Satisfiable`).
pub(crate) async fn serve_response_file(
    file: File,
    metadata: std::fs::Metadata,
    response: Response,
    request_headers: &HeaderMap,
    head: bool,
) -> Response {
    let (mut response, selected) = select_response_file(&metadata, response, request_headers, head);
    if let Some((start, length)) = selected {
        let mut file = tokio::fs::File::from_std(file);
        if start != 0 && file.seek(SeekFrom::Start(start)).await.is_err() {
            return crate::error(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error");
        }
        *response.body_mut() = Body::new(FileBody::new(file, length));
        response.extensions_mut().insert(crate::budget::Unreserved);
    }
    response
}

/// The same representation selection is used before native and ASGI headers.
fn select_response_file(
    metadata: &std::fs::Metadata,
    mut response: Response,
    request_headers: &HeaderMap,
    head: bool,
) -> (Response, Option<(u64, u64)>) {
    if matches!(response.status().as_u16(), 204 | 205 | 304) {
        let headers = response.headers_mut();
        headers.remove(header::CONTENT_TYPE);
        headers.remove(header::CONTENT_LENGTH);
        return (response, None);
    }
    let length = metadata.len();
    let file_modified = metadata.modified().ok();
    let response_headers = response.headers_mut();
    if !response_headers.contains_key(header::LAST_MODIFIED)
        && let Some(value) = file_modified
            .map(httpdate::fmt_http_date)
            .and_then(|value| HeaderValue::from_str(&value).ok())
    {
        response_headers.insert(header::LAST_MODIFIED, value);
    }
    let modified = response_headers
        .get(header::LAST_MODIFIED)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| httpdate::parse_http_date(value).ok())
        .or(file_modified);
    let etag = match response_headers.get(header::ETAG).cloned() {
        Some(etag) => etag,
        None => {
            let etag = entity_tag(length, modified);
            response_headers.insert(header::ETAG, etag.clone());
            etag
        }
    };
    if response.status() == StatusCode::OK && not_modified(request_headers, &etag, modified) {
        let response_headers = response.headers_mut();
        response_headers.remove(header::CONTENT_TYPE);
        response_headers.remove(header::CONTENT_LENGTH);
        response_headers.remove(header::CONTENT_ENCODING);
        *response.status_mut() = StatusCode::NOT_MODIFIED;
        return (response, None);
    }
    let has_encoding = response.headers().contains_key(header::CONTENT_ENCODING);
    if !has_encoding {
        response
            .headers_mut()
            .entry(header::ACCEPT_RANGES)
            .or_insert(HeaderValue::from_static("bytes"));
        if response.status() == StatusCode::OK {
            let range = request_headers
                .get(header::RANGE)
                .filter(|_| if_range_matches(request_headers, &etag, modified))
                .and_then(|value| value.to_str().ok());
            if let Some(range) = range {
                match parse_range(range, length) {
                    Range::Satisfiable(start, end) => {
                        let slice_len = end - start + 1;
                        let response_headers = response.headers_mut();
                        response_headers.remove(header::CONTENT_LENGTH);
                        response_headers.insert(
                            header::CONTENT_RANGE,
                            HeaderValue::from_str(&format!("bytes {start}-{end}/{length}"))
                                .unwrap(),
                        );
                        *response.status_mut() = StatusCode::PARTIAL_CONTENT;
                        if head {
                            crate::set_head_length(response.headers_mut(), slice_len as usize);
                        }
                        return (response, (!head).then_some((start, slice_len)));
                    }
                    Range::Unsatisfiable => {
                        let response_headers = response.headers_mut();
                        response_headers.remove(header::CONTENT_TYPE);
                        response_headers.remove(header::CONTENT_LENGTH);
                        response_headers.insert(
                            header::CONTENT_RANGE,
                            HeaderValue::from_str(&format!("bytes */{length}")).unwrap(),
                        );
                        *response.status_mut() = StatusCode::RANGE_NOT_SATISFIABLE;
                        return (response, None);
                    }
                    Range::Ignored => {}
                }
            }
        }
    }
    response.headers_mut().remove(header::CONTENT_LENGTH);
    if head {
        crate::set_head_length(response.headers_mut(), length as usize);
    }
    (response, (!head).then_some((0, length)))
}

/// The served root, held open on Unix so later opens resolve beneath it
/// without following symlinks. Windows has no `openat`, so there the root is
/// its canonical path and files are opened by path after the containment
/// check, as before.
#[cfg(unix)]
type RootHandle = File;
#[cfg(not(unix))]
type RootHandle = PathBuf;

#[cfg(not(unix))]
fn pinned_directory(path: &Path) -> std::io::Result<RootHandle> {
    Ok(path.to_path_buf())
}

#[cfg(not(unix))]
fn open_beneath(root: &RootHandle, relative: &Path) -> std::io::Result<(File, std::fs::Metadata)> {
    if !relative
        .components()
        .all(|component| matches!(component, Component::Normal(_)))
    {
        return Err(std::io::ErrorKind::InvalidInput.into());
    }
    let file = File::open(root.join(relative))?;
    let metadata = file.metadata()?;
    if !metadata.is_file() {
        return Err(std::io::ErrorKind::InvalidData.into());
    }
    Ok((file, metadata))
}

#[cfg(unix)]
fn pinned_directory(path: &Path) -> std::io::Result<File> {
    use rustix::fs::{Mode, OFlags, open, openat};
    let flags =
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC | OFlags::NONBLOCK;
    let mut directory = File::from(open("/", flags, Mode::empty())?);
    for component in path.components() {
        match component {
            Component::RootDir => {}
            Component::Normal(name) => {
                directory = File::from(openat(&directory, name, flags, Mode::empty())?);
            }
            _ => return Err(std::io::ErrorKind::InvalidInput.into()),
        }
    }
    Ok(directory)
}

#[cfg(unix)]
fn open_beneath(root: &File, relative: &Path) -> std::io::Result<(File, std::fs::Metadata)> {
    use rustix::fs::{Mode, OFlags, openat};
    let mut components = relative.components().peekable();
    let mut current: Option<File> = None;
    while let Some(component) = components.next() {
        let Component::Normal(name) = component else {
            return Err(std::io::ErrorKind::InvalidInput.into());
        };
        let mut flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC | OFlags::NONBLOCK;
        if components.peek().is_some() {
            flags |= OFlags::DIRECTORY;
        }
        current = Some(File::from(openat(
            current.as_ref().unwrap_or(root),
            name,
            flags,
            Mode::empty(),
        )?));
    }
    let file = current.ok_or(std::io::ErrorKind::InvalidInput)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() {
        return Err(std::io::ErrorKind::InvalidData.into());
    }
    Ok((file, metadata))
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
    /// A static mount's slot, released when the body is dropped.
    _slot: Option<OwnedSemaphorePermit>,
}

impl FileBody {
    fn new(file: tokio::fs::File, length: u64) -> Self {
        Self {
            file,
            remaining: length,
            buffer: Vec::new(),
            _slot: None,
        }
    }

    fn holding(mut self, slot: OwnedSemaphorePermit) -> Self {
        self._slot = Some(slot);
        self
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

    #[tokio::test]
    async fn responses_in_progress_are_bounded_per_mount() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root =
            std::env::temp_dir().join(format!("bessemer-slots-{}-{unique}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("item"), b"public").unwrap();
        let files = StaticDir::new(root.to_str().unwrap(), false, None, 2).unwrap();
        let uri: Uri = "/item".parse().unwrap();
        let headers = HeaderMap::new();
        // Two bodies not yet sent hold both slots; a third response is refused.
        let first = files.serve("item", &uri, &headers).await;
        let second = files.serve("item", &uri, &headers).await;
        assert_eq!(first.status(), StatusCode::OK);
        assert_eq!(second.status(), StatusCode::OK);
        let refused = files.serve("item", &uri, &headers).await;
        assert_eq!(refused.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(refused.headers()[header::RETRY_AFTER], "1");
        // A missing file, a conditional hit and an unsatisfiable range hold
        // nothing once answered.
        drop(first);
        assert_eq!(
            files.serve("missing", &uri, &headers).await.status(),
            StatusCode::NOT_FOUND
        );
        let mut conditional = HeaderMap::new();
        conditional.insert(
            header::IF_NONE_MATCH,
            second.headers()[header::ETAG].clone(),
        );
        assert_eq!(
            files.serve("item", &uri, &conditional).await.status(),
            StatusCode::NOT_MODIFIED
        );
        let third = files.serve("item", &uri, &headers).await;
        assert_eq!(third.status(), StatusCode::OK);
        // A body that was sent or abandoned gives its slot back.
        drop((second, third));
        assert_eq!(files.slots.available_permits(), 2);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn directory_redirects_never_leave_this_host() {
        // `//host` and `/\host` are protocol-relative to a browser.
        for path in ["//evil.example/%ff", "//evil.example", "/\\evil.example/x"] {
            let Ok(uri) = path.parse::<Uri>() else {
                continue;
            };
            let response = redirect_to_directory(&uri);
            assert_eq!(response.status(), StatusCode::NOT_FOUND, "{path}");
            assert!(response.headers().get(header::LOCATION).is_none(), "{path}");
        }
        let response = redirect_to_directory(&"/site?x=1".parse().unwrap());
        assert_eq!(response.status(), StatusCode::TEMPORARY_REDIRECT);
        assert_eq!(response.headers()[header::LOCATION], "/site/?x=1");
        assert!(protocol_relative("//x"));
        assert!(protocol_relative("/\\x"));
        assert!(!protocol_relative("/x//y"));
        assert!(!protocol_relative("/"));
        assert!(!protocol_relative(""));
    }

    #[tokio::test]
    async fn replaced_static_paths_cannot_escape_the_pinned_root() {
        use std::os::unix::fs::symlink;
        use tokio::io::AsyncReadExt;

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let base =
            std::env::temp_dir().join(format!("bessemer-static-{}-{unique}", std::process::id()));
        let root = base.join("public");
        std::fs::create_dir_all(root.join("nested")).unwrap();
        let outside = base.join("secret");
        std::fs::write(&outside, b"secret").unwrap();
        std::fs::write(root.join("item"), b"public").unwrap();
        std::fs::write(root.join("item.br"), b"compressed").unwrap();
        std::fs::write(root.join("nested/item"), b"nested").unwrap();
        let files = StaticDir::new(root.to_str().unwrap(), false, None, 1024).unwrap();

        for name in ["item", "item.br"] {
            let path = root.join(name);
            let checked = files.canonical_contained(&path).await.unwrap();
            std::fs::remove_file(&path).unwrap();
            symlink(&outside, &path).unwrap();
            assert!(files.open_file(&checked).await.is_err(), "{name}");
        }

        let checked = files
            .canonical_contained(&root.join("nested/item"))
            .await
            .unwrap();
        std::fs::rename(root.join("nested"), root.join("moved")).unwrap();
        symlink(&base, root.join("nested")).unwrap();
        assert!(files.open_file(&checked).await.is_err());

        symlink(root.join("moved/item"), root.join("alias")).unwrap();
        let checked = files
            .canonical_contained(&root.join("alias"))
            .await
            .unwrap();
        let (mut safe, metadata) = files.open_file(&checked).await.unwrap();
        assert_eq!(metadata.len(), 6);
        let mut data = Vec::new();
        safe.read_to_end(&mut data).await.unwrap();
        assert_eq!(data, b"nested");

        std::fs::write(root.join("fresh"), b"a").unwrap();
        let checked = files
            .canonical_contained(&root.join("fresh"))
            .await
            .unwrap();
        std::fs::write(root.join("fresh"), b"replacement").unwrap();
        let (_, metadata) = files.open_file(&checked).await.unwrap();
        assert_eq!(metadata.len(), 11);

        std::fs::write(root.join("pipe"), b"before").unwrap();
        let checked = files.canonical_contained(&root.join("pipe")).await.unwrap();
        std::fs::remove_file(root.join("pipe")).unwrap();
        let created = std::process::Command::new("mkfifo")
            .arg(root.join("pipe"))
            .status()
            .unwrap();
        assert!(created.success());
        let attempted = tokio::time::timeout(
            std::time::Duration::from_millis(500),
            files.open_file(&checked),
        )
        .await;
        assert!(attempted.is_ok_and(|result| result.is_err()));
        std::fs::remove_dir_all(base).unwrap();
    }

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
