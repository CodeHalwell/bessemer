//! Disk-spooled `multipart/form-data` bodies for uploads larger than the
//! in-memory body limit.
//!
//! The body is parsed as it arrives ([`forms::MultipartStream`]): field
//! values stay in memory, bounded by the body limit, and file data is
//! appended to one anonymous temporary file per request, which is unlinked as
//! soon as it is created and so disappears when the last descriptor closes.
//! Disk use is charged to a server-wide budget for the life of the request.

use crate::budget::{ByteBudget, BytePermit};
use crate::forms::{self, Event, Part};
use axum::body::Body;
use http_body_util::BodyExt;
use std::fs::File;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::sync::OwnedSemaphorePermit;

/// Server-wide spooling settings, present when `max_upload_size` is set.
pub(crate) struct Settings {
    /// The largest multipart body a file route accepts.
    pub(crate) max_upload_size: u64,
    /// Where spool files are created (and immediately unlinked).
    pub(crate) directory: PathBuf,
    /// Disk bytes held by spooled requests across the server.
    pub(crate) budget: ByteBudget,
}

/// A spooled multipart body.
pub(crate) struct Spooled {
    pub(crate) fields: Vec<(String, String)>,
    /// File parts, their data ranges into `file`.
    pub(crate) files: Vec<Part>,
    pub(crate) file: Option<Arc<File>>,
}

/// Everything a spooled request holds against server limits. It moves into
/// each blocking spool operation and back ([`blocking`]), so a request that is
/// cancelled mid-operation keeps its `max_in_flight` slot and its byte
/// budgets until the thread has actually finished.
#[derive(Default)]
pub(crate) struct Held {
    pub(crate) admission: Option<OwnedSemaphorePermit>,
    /// Request byte budget for field values and materialized `bytes` parts.
    pub(crate) memory: BytePermit,
    /// Disk budget for the spool file.
    pub(crate) disk: BytePermit,
}

/// Run `work` on a blocking thread with `held` moved into it.
async fn blocking<R: Send + 'static>(
    held: &mut Held,
    work: impl FnOnce() -> R + Send + 'static,
) -> Result<R, Error> {
    let taken = std::mem::take(held);
    let (taken, result) = tokio::task::spawn_blocking(move || {
        let result = work();
        (taken, result)
    })
    .await
    .map_err(|_| Error::Io)?;
    *held = taken;
    Ok(result)
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Error {
    /// The body is larger than `max_upload_size`.
    TooLarge,
    /// The field values (not files) are larger than `max_body_size`.
    FieldsTooLarge,
    /// The disk or memory budget is full.
    Full,
    Malformed(&'static str),
    /// The body stream failed.
    Body,
    /// The spool file could not be written.
    Io,
}

enum Current {
    None,
    Field(Part, Vec<u8>),
    File(Part, usize),
}

static SEQUENCE: AtomicU64 = AtomicU64::new(0);

fn create(directory: &std::path::Path) -> std::io::Result<File> {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_nanos());
    let path = directory.join(format!(
        ".bessemer-upload-{}-{}-{nanos}",
        std::process::id(),
        SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ));
    // Gone at once (or on close, on Windows): the data lives only as long as
    // open handles.
    crate::platform::anonymous_file(&path)
}

/// Read and parse `body`, spooling file parts to disk.
pub(crate) async fn spool(
    mut body: Body,
    boundary: &str,
    settings: &Settings,
    max_fields: usize,
    memory_budget: &ByteBudget,
    held: &mut Held,
) -> Result<Spooled, Error> {
    let mut stream = forms::MultipartStream::new(boundary);
    let mut events = Vec::new();
    let mut received: u64 = 0;
    let mut field_bytes = 0usize;
    let mut file: Option<File> = None;
    let mut written = 0usize;
    // File bytes from the current frame, written in one blocking call.
    let mut pending = Vec::new();
    let mut current = Current::None;
    let mut spooled = Spooled {
        fields: Vec::new(),
        files: Vec::new(),
        file: None,
    };
    while let Some(frame) = body.frame().await {
        let frame = frame.map_err(|_| Error::Body)?;
        let Ok(data) = frame.into_data() else {
            continue;
        };
        received = received.saturating_add(data.len() as u64);
        if received > settings.max_upload_size {
            return Err(Error::TooLarge);
        }
        stream.push(&data, &mut events).map_err(Error::Malformed)?;
        for event in events.drain(..) {
            match event {
                Event::Begin(part) => {
                    current = if part.filename.is_some() {
                        Current::File(part, written)
                    } else {
                        Current::Field(part, Vec::new())
                    };
                }
                Event::Data(bytes) => match &mut current {
                    Current::Field(_, value) => {
                        field_bytes += bytes.len();
                        if field_bytes > max_fields {
                            return Err(Error::FieldsTooLarge);
                        }
                        held.memory
                            .extend(memory_budget, bytes.len())
                            .map_err(|_| Error::Full)?;
                        value.extend_from_slice(&bytes);
                    }
                    Current::File(..) => {
                        held.disk
                            .extend(&settings.budget, bytes.len())
                            .map_err(|_| Error::Full)?;
                        pending.extend_from_slice(&bytes);
                        written += bytes.len();
                    }
                    Current::None => unreachable!("data only follows a part's start"),
                },
                Event::End => match std::mem::replace(&mut current, Current::None) {
                    Current::Field(part, value) => {
                        let value = String::from_utf8(value)
                            .map_err(|_| Error::Malformed("Form fields must be UTF-8"))?;
                        spooled.fields.push((part.name, value));
                    }
                    Current::File(part, start) => {
                        spooled.files.push(Part {
                            data: start..written,
                            ..part
                        });
                    }
                    Current::None => unreachable!("a part ends after it starts"),
                },
            }
        }
        if !pending.is_empty() {
            let data = std::mem::take(&mut pending);
            let directory = settings.directory.clone();
            file = Some(
                blocking(held, move || {
                    let mut target = match file {
                        Some(target) => target,
                        None => create(&directory)?,
                    };
                    target.write_all(&data)?;
                    Ok::<_, std::io::Error>(target)
                })
                .await?
                .map_err(|_| Error::Io)?,
            );
        }
    }
    stream.finish().map_err(Error::Malformed)?;
    spooled.file = file.map(Arc::new);
    Ok(spooled)
}

/// Read spooled parts bound to `bytes` parameters into memory on a blocking
/// thread, so the Python loop never reads the disk while converting
/// arguments. `held` travels with the read, as for writes.
pub(crate) async fn materialize(
    uploads: Vec<forms::Upload>,
    held: &mut Held,
) -> Result<Vec<forms::Upload>, Error> {
    blocking(held, move || {
        uploads
            .into_iter()
            .map(|mut upload| {
                if let (true, forms::UploadData::Spooled { file, range }) =
                    (upload.raw, &upload.data)
                {
                    let mut data = vec![0u8; range.len()];
                    crate::platform::read_exact_at(file, &mut data, range.start as u64)?;
                    upload.data = forms::UploadData::Memory(data.into());
                }
                Ok(upload)
            })
            .collect::<std::io::Result<Vec<_>>>()
    })
    .await?
    .map_err(|_| Error::Io)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn settings(max_upload_size: u64, disk: usize) -> Settings {
        Settings {
            max_upload_size,
            directory: std::env::temp_dir(),
            budget: ByteBudget::new(disk),
        }
    }

    fn multipart(file: &[u8]) -> Vec<u8> {
        let mut body = b"--b\r\nContent-Disposition: form-data; name=\"note\"\r\n\r\nhello\r\n--b\r\nContent-Disposition: form-data; name=\"upload\"; filename=\"data.bin\"\r\nContent-Type: application/octet-stream\r\n\r\n".to_vec();
        body.extend_from_slice(file);
        body.extend_from_slice(b"\r\n--b--\r\n");
        body
    }

    struct Chunks(std::collections::VecDeque<bytes::Bytes>);

    impl http_body::Body for Chunks {
        type Data = bytes::Bytes;
        type Error = std::io::Error;

        fn poll_frame(
            mut self: std::pin::Pin<&mut Self>,
            _: &mut std::task::Context<'_>,
        ) -> std::task::Poll<Option<Result<http_body::Frame<bytes::Bytes>, Self::Error>>> {
            std::task::Poll::Ready(
                self.0
                    .pop_front()
                    .map(|chunk| Ok(http_body::Frame::data(chunk))),
            )
        }
    }

    fn chunked(data: Vec<u8>, size: usize) -> Body {
        Body::new(Chunks(
            data.chunks(size)
                .map(bytes::Bytes::copy_from_slice)
                .collect(),
        ))
    }

    #[tokio::test]
    async fn files_are_spooled_and_fields_kept_in_memory() {
        let contents: Vec<u8> = (0..=255u8).cycle().take(300_000).collect();
        let settings = settings(1 << 20, 1 << 20);
        let memory = ByteBudget::new(1024);
        let capacity = Arc::new(tokio::sync::Semaphore::new(1));
        let mut held = Held {
            admission: Some(capacity.clone().try_acquire_owned().unwrap()),
            ..Held::default()
        };
        let spooled = spool(
            chunked(multipart(&contents), 4093),
            "b",
            &settings,
            64,
            &memory,
            &mut held,
        )
        .await
        .unwrap();
        // The admission permit comes back from the blocking writes.
        assert!(held.admission.is_some());
        assert_eq!(capacity.available_permits(), 0);
        assert_eq!(
            spooled.fields,
            vec![("note".to_owned(), "hello".to_owned())]
        );
        assert_eq!(spooled.files.len(), 1);
        let part = &spooled.files[0];
        assert_eq!(part.filename.as_deref(), Some("data.bin"));
        let file = spooled.file.as_ref().unwrap();
        let mut read = vec![0u8; part.data.len()];
        crate::platform::read_exact_at(file, &mut read, part.data.start as u64).unwrap();
        assert_eq!(read, contents);
        // Disk and memory are charged while the request holds them.
        assert_eq!(settings.budget.available(), (1 << 20) - contents.len());
        assert_eq!(memory.available(), 1024 - 5);
        drop(spooled);
        drop(held);
        assert_eq!(capacity.available_permits(), 1);
        assert_eq!(settings.budget.available(), 1 << 20);
        assert_eq!(memory.available(), 1024);
    }

    #[tokio::test]
    async fn limits_are_enforced_while_reading() {
        let memory = ByteBudget::new(1024);
        let body = multipart(&[7u8; 5000]);
        let small = settings(4096, 1 << 20);
        assert_eq!(
            spool(
                chunked(body.clone(), 512),
                "b",
                &small,
                64,
                &memory,
                &mut Held::default()
            )
            .await
            .err(),
            Some(Error::TooLarge)
        );
        let tight_disk = settings(1 << 20, 1000);
        assert_eq!(
            spool(
                chunked(body.clone(), 512),
                "b",
                &tight_disk,
                64,
                &memory,
                &mut Held::default()
            )
            .await
            .err(),
            Some(Error::Full)
        );
        assert_eq!(tight_disk.budget.available(), 1000);
        let roomy = settings(1 << 20, 1 << 20);
        assert_eq!(
            spool(
                chunked(body.clone(), 512),
                "b",
                &roomy,
                4,
                &memory,
                &mut Held::default()
            )
            .await
            .err(),
            Some(Error::FieldsTooLarge)
        );
        let truncated = body[..body.len() - 8].to_vec();
        assert!(matches!(
            spool(
                chunked(truncated, 512),
                "b",
                &roomy,
                64,
                &memory,
                &mut Held::default()
            )
            .await
            .err(),
            Some(Error::Malformed(_))
        ));
        assert_eq!(memory.available(), 1024);
        assert_eq!(roomy.budget.available(), 1 << 20);
    }
}
