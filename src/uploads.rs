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
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

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
    /// Disk budget for `file`, held until the request completes.
    pub(crate) disk: BytePermit,
    /// Request byte budget for the field values held in memory.
    pub(crate) memory: BytePermit,
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
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)?;
    // Unlinked at once: the data lives only as long as open descriptors.
    std::fs::remove_file(&path)?;
    Ok(file)
}

/// Read and parse `body`, spooling file parts to disk.
pub(crate) async fn spool(
    mut body: Body,
    boundary: &str,
    settings: &Settings,
    max_fields: usize,
    memory_budget: &ByteBudget,
) -> Result<Spooled, Error> {
    let mut stream = forms::MultipartStream::new(boundary);
    let mut events = Vec::new();
    let mut received: u64 = 0;
    let mut field_bytes = 0usize;
    let mut file: Option<File> = None;
    let mut written = 0usize;
    let mut current = Current::None;
    let mut spooled = Spooled {
        fields: Vec::new(),
        files: Vec::new(),
        file: None,
        disk: BytePermit::default(),
        memory: BytePermit::default(),
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
                        spooled
                            .memory
                            .extend(memory_budget, bytes.len())
                            .map_err(|_| Error::Full)?;
                        value.extend_from_slice(&bytes);
                    }
                    Current::File(..) => {
                        spooled
                            .disk
                            .extend(&settings.budget, bytes.len())
                            .map_err(|_| Error::Full)?;
                        if file.is_none() {
                            file = Some(create(&settings.directory).map_err(|_| Error::Io)?);
                        }
                        let target = file.as_mut().expect("created above");
                        target.write_all(&bytes).map_err(|_| Error::Io)?;
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
    }
    stream.finish().map_err(Error::Malformed)?;
    if let Some(mut target) = file {
        target.flush().map_err(|_| Error::Io)?;
        spooled.file = Some(Arc::new(target));
    }
    Ok(spooled)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::FileExt;

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
        let spooled = spool(
            chunked(multipart(&contents), 4093),
            "b",
            &settings,
            64,
            &memory,
        )
        .await
        .unwrap();
        assert_eq!(
            spooled.fields,
            vec![("note".to_owned(), "hello".to_owned())]
        );
        assert_eq!(spooled.files.len(), 1);
        let part = &spooled.files[0];
        assert_eq!(part.filename.as_deref(), Some("data.bin"));
        let file = spooled.file.as_ref().unwrap();
        let mut read = vec![0u8; part.data.len()];
        file.read_exact_at(&mut read, part.data.start as u64)
            .unwrap();
        assert_eq!(read, contents);
        // Disk and memory are charged while the request holds them.
        assert_eq!(settings.budget.available(), (1 << 20) - contents.len());
        assert_eq!(memory.available(), 1024 - 5);
        drop(spooled);
        assert_eq!(settings.budget.available(), 1 << 20);
        assert_eq!(memory.available(), 1024);
    }

    #[tokio::test]
    async fn limits_are_enforced_while_reading() {
        let memory = ByteBudget::new(1024);
        let body = multipart(&[7u8; 5000]);
        let small = settings(4096, 1 << 20);
        assert_eq!(
            spool(chunked(body.clone(), 512), "b", &small, 64, &memory)
                .await
                .err(),
            Some(Error::TooLarge)
        );
        let tight_disk = settings(1 << 20, 1000);
        assert_eq!(
            spool(chunked(body.clone(), 512), "b", &tight_disk, 64, &memory)
                .await
                .err(),
            Some(Error::Full)
        );
        assert_eq!(tight_disk.budget.available(), 1000);
        let roomy = settings(1 << 20, 1 << 20);
        assert_eq!(
            spool(chunked(body.clone(), 512), "b", &roomy, 4, &memory)
                .await
                .err(),
            Some(Error::FieldsTooLarge)
        );
        let truncated = body[..body.len() - 8].to_vec();
        assert!(matches!(
            spool(chunked(truncated, 512), "b", &roomy, 64, &memory)
                .await
                .err(),
            Some(Error::Malformed(_))
        ));
        assert_eq!(memory.available(), 1024);
        assert_eq!(roomy.budget.available(), 1 << 20);
    }
}
