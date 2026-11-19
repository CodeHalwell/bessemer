//! Native static file serving: `app.static("/assets", directory=...)`.
//!
//! Files are resolved under one canonical root (symlinks that leave it are
//! refused), validated with `ETag` and `Last-Modified`, answered with `304`
//! for conditional requests, sliced for single byte ranges, and served from
//! precompressed `.br` or `.gz` siblings when the client accepts them. Bodies
//! stream from disk in fixed-size chunks, so memory per response is bounded
//! whatever the file size.
//!
//! A body, a mount's or a route's `FileResponse`, reads its file through the
//! process's [`OpenFiles`] table: one descriptor per distinct file being
//! sent, shared by every response sending it and read by position.

use axum::{
    body::{Body, Bytes},
    http::{HeaderMap, HeaderValue, StatusCode, Uri, header},
    response::Response,
};
use http_body::{Frame, SizeHint};
use std::{
    collections::HashMap,
    fs::File,
    future::Future,
    path::{Component, Path, PathBuf},
    pin::Pin,
    sync::{
        Arc, LazyLock, Mutex, PoisonError, Weak,
        atomic::{AtomicUsize, Ordering},
    },
    task::{Context, Poll, ready},
    time::{SystemTime, UNIX_EPOCH},
};

const CHUNK: usize = 64 * 1024;

/// The files with a response body in progress, each held open once.
///
/// Static responses run no handler, and a `FileResponse` body outlives its
/// handler's admission, so without a bound HTTP/2 streams that stop reading
/// could each hold a descriptor and exhaust the process's with a handful of
/// connections. A body holds its file's entry rather than a descriptor of its
/// own, so a hundred streams sending one file cost one descriptor, and the
/// bound is on distinct files: a body for a file that is not open yet is
/// refused with `503` while `capacity` files are. The capacity is the
/// server's `max_connections` ([`limit_open_files`]): HTTP/1.1 sends one
/// response at a time per connection, so it never reaches the bound, and
/// HTTP/2 cannot hold more files open than HTTP/1.1 connections could.
pub(crate) struct OpenFiles {
    capacity: AtomicUsize,
    files: Mutex<HashMap<FileKey, Weak<SharedFile>>>,
}

/// One table per process, since descriptors are a per-process resource.
static OPEN_FILES: LazyLock<Arc<OpenFiles>> = LazyLock::new(|| OpenFiles::new(1024));

/// Size the process's table of open files; called with `max_connections`
/// when a server is built.
pub(crate) fn limit_open_files(max_connections: usize) {
    OPEN_FILES
        .capacity
        .store(max_connections.max(1), Ordering::Relaxed);
}

impl OpenFiles {
    fn new(capacity: usize) -> Arc<Self> {
        Arc::new(Self {
            capacity: AtomicUsize::new(capacity.max(1)),
            files: Mutex::new(HashMap::new()),
        })
    }

    /// The shared entry for the file just opened as `file`, or `None` when
    /// that file is not open yet and the table is full. When it is open
    /// already, `file` is closed and the open descriptor used: both name the
    /// same inode, so they read the same bytes.
    fn share(
        self: &Arc<Self>,
        file: File,
        metadata: &std::fs::Metadata,
    ) -> Option<Arc<SharedFile>> {
        let key = file_key(metadata);
        let mut files = self.files.lock().unwrap_or_else(PoisonError::into_inner);
        if let Some(shared) = files.get(&key).and_then(Weak::upgrade) {
            return Some(shared);
        }
        let capacity = self.capacity.load(Ordering::Relaxed);
        if files.len() >= capacity {
            // An entry whose last body is being dropped can linger briefly.
            files.retain(|_, entry| entry.strong_count() > 0);
            if files.len() >= capacity {
                return None;
            }
        }
        let shared = Arc::new(SharedFile {
            file,
            key,
            table: self.clone(),
        });
        files.insert(key, Arc::downgrade(&shared));
        Some(shared)
    }

    #[cfg(test)]
    fn open(&self) -> usize {
        let files = self.files.lock().unwrap();
        files
            .values()
            .filter(|entry| entry.strong_count() > 0)
            .count()
    }
}

/// An open file in [`OpenFiles`], closed with its last body.
struct SharedFile {
    file: File,
    key: FileKey,
    table: Arc<OpenFiles>,
}

impl Drop for SharedFile {
    fn drop(&mut self) {
        let mut files = self
            .table
            .files
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        // Unless a new open of the same file has replaced this entry.
        if files
            .get(&self.key)
            .is_some_and(|entry| entry.strong_count() == 0)
        {
            files.remove(&self.key);
        }
    }
}

/// A file's identity: its device and inode, taken from the opened
/// descriptor, so a path replaced by another file is a new entry while the
/// responses already sending the old one finish with the old one.
#[cfg(unix)]
type FileKey = (u64, u64);

#[cfg(unix)]
fn file_key(metadata: &std::fs::Metadata) -> FileKey {
    use std::os::unix::fs::MetadataExt;
    (metadata.dev(), metadata.ino())
}

/// Without a portable file identity each body is its own entry, so the
/// bound counts responses rather than files.
#[cfg(not(unix))]
type FileKey = u64;

#[cfg(not(unix))]
fn file_key(_: &std::fs::Metadata) -> FileKey {
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    NEXT.fetch_add(1, Ordering::Relaxed)
}

#[cfg(unix)]
fn read_at(file: &File, buffer: &mut [u8], offset: u64) -> std::io::Result<usize> {
    std::os::unix::fs::FileExt::read_at(file, buffer, offset)
}

#[cfg(windows)]
fn read_at(file: &File, buffer: &mut [u8], offset: u64) -> std::io::Result<usize> {
    std::os::windows::fs::FileExt::seek_read(file, buffer, offset)
}

/// Up to `want` bytes at `offset`; fewer only at the end of the file.
fn read_chunk(file: &File, offset: u64, want: usize) -> std::io::Result<Bytes> {
    let mut buffer = vec![0; want];
    let mut filled = 0;
    while filled < want {
        match read_at(file, &mut buffer[filled..], offset + filled as u64) {
            Ok(0) => break,
            Ok(read) => filled += read,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {}
            Err(error) => return Err(error),
        }
    }
    buffer.truncate(filled);
    Ok(Bytes::from(buffer))
}

fn chunk_len(remaining: u64) -> usize {
    usize::try_from(remaining).map_or(CHUNK, |left| left.min(CHUNK))
}

/// The request headers a file response depends on, copied for the blocking
/// pool.
fn conditions(headers: &HeaderMap) -> HeaderMap {
    let mut kept = HeaderMap::new();
    for name in [
        header::IF_NONE_MATCH,
        header::IF_MODIFIED_SINCE,
        header::RANGE,
        header::IF_RANGE,
        header::ACCEPT_ENCODING,
    ] {
        for value in headers.get_all(&name) {
            kept.append(name.clone(), value.clone());
        }
    }
    kept
}

fn capacity_full() -> Response {
    crate::capacity_response("Static file capacity is full", false)
}

fn internal_error() -> Response {
    crate::error(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error")
}

pub(crate) struct StaticDir {
    root: PathBuf,
    root_fd: Arc<RootHandle>,
    html: bool,
    cache_control: Option<HeaderValue>,
    /// The process's table of open files, shared with every other mount and
    /// with `FileResponse` bodies.
    open: Arc<OpenFiles>,
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
            open: OPEN_FILES.clone(),
        })
    }

    pub(crate) async fn serve(
        self: &Arc<Self>,
        relative: &str,
        uri: &Uri,
        headers: &HeaderMap,
        head: bool,
    ) -> Response {
        let Some(cleaned) = clean(relative) else {
            return not_found();
        };
        // Relative links in an index page resolve against its directory, so
        // the URL must end in a slash before the index is served.
        let needs_slash = !relative.is_empty() && !relative.ends_with('/');
        let request = conditions(headers);
        let files = self.clone();
        let uri = uri.clone();
        // Resolving, probing siblings, opening, validating and the first
        // read are one trip to the blocking pool.
        tokio::task::spawn_blocking(move || {
            files.respond(&cleaned, needs_slash, &uri, &request, head)
        })
        .await
        .unwrap_or_else(|_| internal_error())
    }

    fn respond(
        &self,
        cleaned: &Path,
        needs_slash: bool,
        uri: &Uri,
        headers: &HeaderMap,
        head: bool,
    ) -> Response {
        let Some((identity, metadata, directory, logical)) = self.resolve(cleaned) else {
            return not_found();
        };
        if directory && needs_slash {
            return redirect_to_directory(uri);
        }
        let mut response_headers = HeaderMap::new();
        response_headers.insert(header::CONTENT_TYPE, content_type(&logical));
        if let Some(cache_control) = &self.cache_control {
            response_headers.insert(header::CACHE_CONTROL, cache_control.clone());
        }

        // Choose the representation first: validators, Vary and the body all
        // belong to it, and conditional requests are judged against it. A
        // `304` carries Vary too, so siblings are probed before validators
        // are compared.
        let (variants_exist, encoded) = self.precompressed(&logical, headers);
        let has_encoding = encoded.is_some();
        if variants_exist {
            response_headers.append(header::VARY, HeaderValue::from_static("Accept-Encoding"));
        }
        let (file, metadata, etag) = match encoded {
            Some((coding, sibling, sibling_metadata)) => {
                response_headers.insert(header::CONTENT_ENCODING, HeaderValue::from_static(coding));
                // Each encoding is its own representation, validated by the
                // sibling's own metadata, so regenerating it changes its tag.
                let tag = entity_tag(sibling_metadata.len(), sibling_metadata.modified().ok());
                let mut tag = tag.as_bytes()[..tag.len() - 1].to_vec();
                tag.extend_from_slice(format!("-{coding}\"").as_bytes());
                (
                    sibling,
                    sibling_metadata,
                    HeaderValue::from_bytes(&tag).unwrap(),
                )
            }
            None => {
                let tag = entity_tag(metadata.len(), metadata.modified().ok());
                (identity, metadata, tag)
            }
        };
        let length = metadata.len();
        let modified = metadata.modified().ok();
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
        let mut status = StatusCode::OK;
        let (mut start, mut size) = (0, length);
        if !has_encoding {
            // Byte ranges are offered on the identity representation only.
            response_headers.insert(header::ACCEPT_RANGES, HeaderValue::from_static("bytes"));
            let range = headers
                .get(header::RANGE)
                .filter(|_| if_range_matches(headers, &etag, modified))
                .and_then(|value| value.to_str().ok());
            if let Some(range) = range {
                match parse_range(range, length) {
                    Range::Satisfiable(first, last) => {
                        response_headers.insert(
                            header::CONTENT_RANGE,
                            HeaderValue::from_str(&format!("bytes {first}-{last}/{length}"))
                                .unwrap(),
                        );
                        status = StatusCode::PARTIAL_CONTENT;
                        (start, size) = (first, last - first + 1);
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
        let body = if head {
            // Only the length is needed: no entry is taken and nothing read.
            Body::new(Unsent(size))
        } else {
            let Some(shared) = self.open.share(file, &metadata) else {
                return capacity_full();
            };
            Body::new(FileBody::start(shared, start, size))
        };
        let mut response = with_headers(status, response_headers, body);
        if status == StatusCode::OK {
            response.extensions_mut().insert(crate::budget::Unreserved);
        }
        response
    }

    /// Whether `.br`/`.gz` siblings exist, and the one the client accepts.
    fn precompressed(
        &self,
        path: &Path,
        headers: &HeaderMap,
    ) -> (bool, Option<(&'static str, File, std::fs::Metadata)>) {
        let mut variants_exist = false;
        for (coding, suffix) in [("br", ".br"), ("gzip", ".gz")] {
            let mut sibling = path.to_path_buf().into_os_string();
            sibling.push(suffix);
            let sibling = PathBuf::from(sibling);
            let Some(canonical) = self.canonical_contained(&sibling) else {
                continue;
            };
            let Ok((file, metadata)) = self.open_file(&canonical) else {
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
    /// target may carry a different name. Runs on the blocking pool.
    fn resolve(&self, relative: &Path) -> Option<(File, std::fs::Metadata, bool, PathBuf)> {
        let mut logical = self.root.join(relative);
        let mut path = std::fs::canonicalize(&logical).ok()?;
        if !self.serves(&path) {
            return None;
        }
        let directory = std::fs::metadata(&path).ok()?.is_dir();
        if directory {
            if !self.html {
                return None;
            }
            logical.push("index.html");
            path = std::fs::canonicalize(path.join("index.html")).ok()?;
            if !self.serves(&path) {
                return None;
            }
        }
        let (file, metadata) = self.open_file(&path).ok()?;
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

    fn canonical_contained(&self, path: &Path) -> Option<PathBuf> {
        std::fs::canonicalize(path)
            .ok()
            .filter(|path| self.serves(path))
    }

    /// Open a checked canonical path beneath the pinned root.
    fn open_file(&self, path: &Path) -> std::io::Result<(File, std::fs::Metadata)> {
        let relative = path
            .strip_prefix(&self.root)
            .map_err(std::io::Error::other)?;
        open_beneath(&self.root_fd, relative)
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

type RawFileHeaders = Vec<(Vec<u8>, Vec<u8>)>;

enum Pending {
    /// Not opened yet: the native path opens it on the blocking pool, and
    /// the ASGI path in `prepare`, which runs on the worker's pool.
    Path(PathBuf),
    Open(File, std::fs::Metadata, Option<u64>),
    Closed,
}

/// A file returned from Python's `FileResponse` to be served natively.
#[derive(Clone)]
#[pyo3::pyclass(module = "bessemer._native", name = "_ResponseFile", from_py_object)]
pub(crate) struct PendingFile(Arc<Mutex<Pending>>);

impl PendingFile {
    /// The path to serve on the native path; `None` once taken or opened.
    pub(crate) fn take(&self) -> Option<PathBuf> {
        let mut pending = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        match std::mem::replace(&mut *pending, Pending::Closed) {
            Pending::Path(path) => Some(path),
            other => {
                *pending = other;
                None
            }
        }
    }
}

/// Open a `FileResponse` path for reading without waiting on anything that
/// is not a regular file: with `O_NONBLOCK`, opening a FIFO returns at once,
/// and the type is checked on the opened descriptor, so a path replaced after
/// `FileResponse` checked it is refused rather than read.
fn open_regular(path: &Path) -> std::io::Result<(File, std::fs::Metadata)> {
    #[cfg(unix)]
    let file = {
        use rustix::fs::{Mode, OFlags, open};
        File::from(open(
            path,
            OFlags::RDONLY | OFlags::NONBLOCK | OFlags::CLOEXEC,
            Mode::empty(),
        )?)
    };
    #[cfg(not(unix))]
    let file = File::open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() {
        return Err(std::io::ErrorKind::InvalidData.into());
    }
    Ok((file, metadata))
}

#[pyo3::pymethods]
impl PendingFile {
    #[new]
    fn py_new(path: &str) -> Self {
        // Called on the event loop with the GIL held, so nothing is opened
        // here.
        Self(Arc::new(Mutex::new(Pending::Path(PathBuf::from(path)))))
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
            let mut guard = self.0.lock().unwrap_or_else(PoisonError::into_inner);
            if let Pending::Path(path) = &*guard {
                let (file, metadata) = open_regular(path).map_err(|error| {
                    pyo3::exceptions::PyRuntimeError::new_err(
                        if error.kind() == std::io::ErrorKind::InvalidData {
                            "file response path is not a regular file"
                        } else {
                            "file response cannot be opened"
                        },
                    )
                })?;
                *guard = Pending::Open(file, metadata, None);
            }
            let Pending::Open(file, metadata, remaining) = &mut *guard else {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(
                    "file response is closed",
                ));
            };
            let (mut response, selected) =
                select_response_file(metadata, response, &parse(request_headers)?, head);
            *remaining = Some(0);
            if let Some((start, length)) = selected {
                use std::io::{Seek, SeekFrom};
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
            let mut guard = self.0.lock().unwrap_or_else(PoisonError::into_inner);
            let Pending::Open(file, _, remaining) = &mut *guard else {
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
        py.detach(|| {
            *self.0.lock().unwrap_or_else(PoisonError::into_inner) = Pending::Closed;
        });
    }
}

/// Serve a route handler's `FileResponse` on the native static-file path,
/// supporting `If-None-Match`, `If-Modified-Since`, `If-Range`, and single
/// byte ranges (`206 Partial Content`, `416 Range Not Satisfiable`). The file
/// is opened, checked and first read on the blocking pool, and its body
/// shares the process's table of open files with the static mounts.
pub(crate) async fn serve_response_file(
    path: PathBuf,
    response: Response,
    request_headers: &HeaderMap,
    head: bool,
) -> Response {
    respond_with_file(&OPEN_FILES, path, response, request_headers, head).await
}

async fn respond_with_file(
    open: &Arc<OpenFiles>,
    path: PathBuf,
    response: Response,
    request_headers: &HeaderMap,
    head: bool,
) -> Response {
    let open = open.clone();
    let request = conditions(request_headers);
    tokio::task::spawn_blocking(move || {
        let Ok((file, metadata)) = open_regular(&path) else {
            return internal_error();
        };
        let (mut response, selected) = select_response_file(&metadata, response, &request, head);
        if let Some((start, length)) = selected {
            let Some(shared) = open.share(file, &metadata) else {
                return capacity_full();
            };
            *response.body_mut() = Body::new(FileBody::start(shared, start, length));
            response.extensions_mut().insert(crate::budget::Unreserved);
        }
        response
    })
    .await
    .unwrap_or_else(|_| internal_error())
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

/// A file body read in fixed-size chunks by position from a shared
/// descriptor, with an exact length.
struct FileBody {
    file: Arc<SharedFile>,
    /// The next byte to read.
    offset: u64,
    /// Bytes not yet sent, `ready` included.
    remaining: u64,
    ready: Option<Bytes>,
    reading: Option<tokio::task::JoinHandle<std::io::Result<Bytes>>>,
}

impl FileBody {
    /// Called on the blocking thread that opened the file, which reads the
    /// first chunk too, so a small file is sent without another trip to the
    /// blocking pool.
    fn start(file: Arc<SharedFile>, start: u64, length: u64) -> Self {
        let want = chunk_len(length);
        let ready = (want > 0)
            .then(|| read_chunk(&file.file, start, want).ok())
            .flatten()
            .filter(|chunk| !chunk.is_empty());
        let offset = start + ready.as_ref().map_or(0, |chunk| chunk.len() as u64);
        Self {
            file,
            offset,
            remaining: length,
            ready,
            reading: None,
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
        let this = &mut *self;
        if this.remaining == 0 {
            return Poll::Ready(None);
        }
        let chunk = match this.ready.take() {
            Some(chunk) => chunk,
            None => {
                let (file, offset, want) = (&this.file, this.offset, chunk_len(this.remaining));
                let reading = this.reading.get_or_insert_with(|| {
                    let file = file.clone();
                    tokio::task::spawn_blocking(move || read_chunk(&file.file, offset, want))
                });
                let read = ready!(Pin::new(reading).poll(cx));
                this.reading = None;
                match read {
                    Ok(Ok(chunk)) => {
                        this.offset += chunk.len() as u64;
                        chunk
                    }
                    Ok(Err(error)) => return Poll::Ready(Some(Err(error))),
                    Err(error) => return Poll::Ready(Some(Err(std::io::Error::other(error)))),
                }
            }
        };
        if chunk.is_empty() {
            // The file shrank after its length was read; end with an error
            // rather than a short body that claims to be complete.
            return Poll::Ready(Some(Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "static file truncated while serving",
            ))));
        }
        this.remaining -= chunk.len() as u64;
        Poll::Ready(Some(Ok(Frame::data(chunk))))
    }

    fn is_end_stream(&self) -> bool {
        self.remaining == 0
    }

    fn size_hint(&self) -> SizeHint {
        SizeHint::with_exact(self.remaining)
    }
}

/// A HEAD response's body: it reports the length a GET would send, and the
/// router replaces it with an empty body before anything is written.
struct Unsent(u64);

impl http_body::Body for Unsent {
    type Data = Bytes;
    type Error = std::io::Error;

    fn poll_frame(
        self: Pin<&mut Self>,
        _: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
        Poll::Ready(
            (self.0 > 0).then(|| Err(std::io::Error::other("a HEAD response sends no body"))),
        )
    }

    fn is_end_stream(&self) -> bool {
        self.0 == 0
    }

    fn size_hint(&self) -> SizeHint {
        SizeHint::with_exact(self.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use http_body_util::BodyExt;

    fn scratch(name: &str) -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root =
            std::env::temp_dir().join(format!("bessemer-{name}-{}-{unique}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        root
    }

    fn mount(root: &Path, open: &Arc<OpenFiles>) -> Arc<StaticDir> {
        let mut files = StaticDir::new(root.to_str().unwrap(), false, None).unwrap();
        files.open = open.clone();
        Arc::new(files)
    }

    async fn get(files: &Arc<StaticDir>, name: &str, headers: &HeaderMap) -> Response {
        let uri: Uri = format!("/{name}").parse().unwrap();
        files.serve(name, &uri, headers, false).await
    }

    async fn collect(response: Response) -> Result<Vec<u8>, axum::Error> {
        Ok(response.into_body().collect().await?.to_bytes().to_vec())
    }

    /// Descriptors this process holds on `path`.
    #[cfg(target_os = "linux")]
    fn descriptors(path: &Path) -> usize {
        std::fs::read_dir("/proc/self/fd")
            .unwrap()
            .filter_map(|entry| std::fs::read_link(entry.ok()?.path()).ok())
            .filter(|target| target == path)
            .count()
    }

    #[tokio::test]
    async fn bodies_share_one_descriptor_per_file_and_distinct_files_are_bounded() {
        let root = scratch("open-files");
        for name in ["a", "b", "c"] {
            std::fs::write(root.join(name), name.repeat(100_000)).unwrap();
        }
        let open = OpenFiles::new(2);
        let files = mount(&root, &open);
        let headers = HeaderMap::new();
        // A hundred bodies of one file, none sent yet, hold one descriptor.
        let mut held = Vec::new();
        for _ in 0..100 {
            let response = get(&files, "a", &headers).await;
            assert_eq!(response.status(), StatusCode::OK);
            held.push(response);
        }
        assert_eq!(open.open(), 1);
        #[cfg(target_os = "linux")]
        assert_eq!(descriptors(&root.canonicalize().unwrap().join("a")), 1);
        // A second file takes the second entry. A third is refused while
        // both are open, but more bodies of an open file are not.
        let second = get(&files, "b", &headers).await;
        assert_eq!(second.status(), StatusCode::OK);
        let refused = get(&files, "c", &headers).await;
        assert_eq!(refused.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(refused.headers()[header::RETRY_AFTER], "1");
        assert_eq!(get(&files, "b", &headers).await.status(), StatusCode::OK);
        // A missing file, HEAD, a conditional hit and an unsatisfiable range
        // open nothing, so they are answered while the table is full.
        assert_eq!(
            get(&files, "missing", &headers).await.status(),
            StatusCode::NOT_FOUND
        );
        let head = files
            .serve("c", &"/c".parse().unwrap(), &headers, true)
            .await;
        assert_eq!(head.status(), StatusCode::OK);
        assert_eq!(
            http_body::Body::size_hint(head.body()).exact(),
            Some(100_000)
        );
        let mut conditional = HeaderMap::new();
        conditional.insert(header::IF_NONE_MATCH, head.headers()[header::ETAG].clone());
        assert_eq!(
            get(&files, "c", &conditional).await.status(),
            StatusCode::NOT_MODIFIED
        );
        let mut range = HeaderMap::new();
        range.insert(header::RANGE, HeaderValue::from_static("bytes=200000-"));
        assert_eq!(
            get(&files, "c", &range).await.status(),
            StatusCode::RANGE_NOT_SATISFIABLE
        );
        assert_eq!(open.open(), 2);
        // A body that was sent or abandoned gives its file back.
        drop(held);
        assert_eq!(open.open(), 1);
        let third = get(&files, "c", &headers).await;
        assert_eq!(third.status(), StatusCode::OK);
        assert_eq!(collect(third).await.unwrap(), b"c".repeat(100_000));
        range.insert(header::RANGE, HeaderValue::from_static("bytes=99990-"));
        let partial = get(&files, "b", &range).await;
        assert_eq!(partial.status(), StatusCode::PARTIAL_CONTENT);
        assert_eq!(collect(partial).await.unwrap(), b"b".repeat(10));
        drop(second);
        assert_eq!(open.open(), 0);
        #[cfg(target_os = "linux")]
        assert_eq!(descriptors(&root.canonicalize().unwrap().join("a")), 0);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn replaced_and_resized_files_keep_each_response_consistent() {
        let root = scratch("resized");
        let path = root.join("item");
        let old = vec![b'o'; 150_000];
        std::fs::write(&path, &old).unwrap();
        let open = OpenFiles::new(4);
        let files = mount(&root, &open);
        let headers = HeaderMap::new();

        // A file replaced while being sent: the response under way finishes
        // with the file its headers describe, and a new one gets the new file.
        let first = get(&files, "item", &headers).await;
        std::fs::write(root.join("item.new"), b"new").unwrap();
        std::fs::rename(root.join("item.new"), &path).unwrap();
        let second = get(&files, "item", &headers).await;
        assert_eq!(open.open(), 2);
        assert_eq!(collect(first).await.unwrap(), old);
        assert_eq!(collect(second).await.unwrap(), b"new");
        assert_eq!(open.open(), 0);

        // Truncated in place while being sent: the body ends with an error
        // rather than short.
        std::fs::write(&path, vec![b'x'; 200_000]).unwrap();
        let shrinking = get(&files, "item", &headers).await;
        File::options()
            .write(true)
            .open(&path)
            .unwrap()
            .set_len(10)
            .unwrap();
        assert!(collect(shrinking).await.is_err());

        // Grown in place: the body sends the length its headers announced.
        std::fs::write(&path, vec![b'g'; 100]).unwrap();
        let growing = get(&files, "item", &headers).await;
        {
            use std::io::Write;
            let mut appending = File::options().append(true).open(&path).unwrap();
            appending.write_all(&[b'h'; 1000]).unwrap();
        }
        assert_eq!(collect(growing).await.unwrap(), vec![b'g'; 100]);
        assert_eq!(open.open(), 0);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn file_responses_share_the_table_and_refuse_what_is_not_a_file() {
        let root = scratch("file-response");
        for name in ["a", "b", "c"] {
            std::fs::write(root.join(name), name.repeat(100_000)).unwrap();
        }
        let open = OpenFiles::new(2);
        let headers = HeaderMap::new();
        let respond = |name: &str, head: bool| {
            respond_with_file(
                &open,
                root.join(name),
                Response::new(Body::empty()),
                &headers,
                head,
            )
        };
        let mut held = Vec::new();
        for _ in 0..100 {
            let response = respond("a", false).await;
            assert_eq!(response.status(), StatusCode::OK);
            held.push(response);
        }
        assert_eq!(open.open(), 1);
        #[cfg(target_os = "linux")]
        assert_eq!(descriptors(&root.canonicalize().unwrap().join("a")), 1);
        let second = respond("b", false).await;
        assert_eq!(second.status(), StatusCode::OK);
        assert_eq!(
            respond("c", false).await.status(),
            StatusCode::SERVICE_UNAVAILABLE
        );
        let head = respond("c", true).await;
        assert_eq!(head.status(), StatusCode::OK);
        assert_eq!(head.headers()[header::CONTENT_LENGTH], "100000");
        drop(held);
        assert_eq!(collect(second).await.unwrap(), b"b".repeat(100_000));
        assert_eq!(open.open(), 0);

        // A path that became a FIFO is refused at once, not waited on.
        let created = std::process::Command::new("mkfifo")
            .arg(root.join("pipe"))
            .status()
            .unwrap();
        assert!(created.success());
        let refused =
            tokio::time::timeout(std::time::Duration::from_secs(5), respond("pipe", false))
                .await
                .expect("opening a FIFO must not wait for a writer");
        assert_eq!(refused.status(), StatusCode::INTERNAL_SERVER_ERROR);
        assert_eq!(
            respond("missing", false).await.status(),
            StatusCode::INTERNAL_SERVER_ERROR
        );
        assert_eq!(open.open(), 0);
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
        use std::io::Read;
        use std::os::unix::fs::symlink;

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
        let files = Arc::new(StaticDir::new(root.to_str().unwrap(), false, None).unwrap());

        for name in ["item", "item.br"] {
            let path = root.join(name);
            let checked = files.canonical_contained(&path).unwrap();
            std::fs::remove_file(&path).unwrap();
            symlink(&outside, &path).unwrap();
            assert!(files.open_file(&checked).is_err(), "{name}");
        }

        let checked = files
            .canonical_contained(&root.join("nested/item"))
            .unwrap();
        std::fs::rename(root.join("nested"), root.join("moved")).unwrap();
        symlink(&base, root.join("nested")).unwrap();
        assert!(files.open_file(&checked).is_err());

        symlink(root.join("moved/item"), root.join("alias")).unwrap();
        let checked = files.canonical_contained(&root.join("alias")).unwrap();
        let (mut safe, metadata) = files.open_file(&checked).unwrap();
        assert_eq!(metadata.len(), 6);
        let mut data = Vec::new();
        safe.read_to_end(&mut data).unwrap();
        assert_eq!(data, b"nested");

        std::fs::write(root.join("fresh"), b"a").unwrap();
        let checked = files.canonical_contained(&root.join("fresh")).unwrap();
        std::fs::write(root.join("fresh"), b"replacement").unwrap();
        let (_, metadata) = files.open_file(&checked).unwrap();
        assert_eq!(metadata.len(), 11);

        std::fs::write(root.join("pipe"), b"before").unwrap();
        let checked = files.canonical_contained(&root.join("pipe")).unwrap();
        std::fs::remove_file(root.join("pipe")).unwrap();
        let created = std::process::Command::new("mkfifo")
            .arg(root.join("pipe"))
            .status()
            .unwrap();
        assert!(created.success());
        let probe = files.clone();
        let attempted = tokio::time::timeout(
            std::time::Duration::from_millis(500),
            tokio::task::spawn_blocking(move || probe.open_file(&checked).is_err()),
        )
        .await;
        assert!(attempted.is_ok_and(|refused| refused.unwrap()));
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
