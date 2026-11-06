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
use axum::body::{Body, Bytes};
use http_body::{Body as HttpBody, Frame};
use std::fs::File;
use std::future::poll_fn;
use std::io::Write;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::task::Poll;
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
    #[cfg(test)]
    writes: usize,
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

#[derive(Default)]
enum Current {
    #[default]
    None,
    Field(Part, Vec<u8>),
    File(Part, usize),
}

const WRITE_BUFFER: usize = 65_536;

#[derive(Default)]
struct SpoolWriter {
    file: Option<File>,
    buffer: Vec<u8>,
    memory: BytePermit,
    #[cfg(test)]
    writes: usize,
}

impl SpoolWriter {
    async fn flush(&mut self, held: &mut Held, directory: &std::path::Path) -> Result<(), Error> {
        if self.buffer.is_empty() {
            return Ok(());
        }
        let directory = self.file.is_none().then(|| directory.to_path_buf());
        self.flush_using(held, move |file, data| {
            let mut target = match file {
                Some(target) => target,
                None => create(directory.as_ref().unwrap())?,
            };
            target.write_all(data)?;
            Ok(target)
        })
        .await
    }

    async fn flush_using(
        &mut self,
        held: &mut Held,
        write: impl FnOnce(Option<File>, &[u8]) -> std::io::Result<File> + Send + 'static,
    ) -> Result<(), Error> {
        if self.buffer.is_empty() {
            return Ok(());
        }
        let mut data = std::mem::take(&mut self.buffer);
        let memory = std::mem::take(&mut self.memory);
        let file = self.file.take();
        let (file, data) = blocking(held, move || {
            let result = write(file, &data);
            data.clear();
            drop(memory);
            result.map(|target| (target, data))
        })
        .await?
        .map_err(|_| Error::Io)?;
        self.file = Some(file);
        self.buffer = data;
        #[cfg(test)]
        {
            self.writes += 1;
        }
        Ok(())
    }

    async fn append(
        &mut self,
        mut data: &[u8],
        held: &mut Held,
        settings: &Settings,
        memory: &ByteBudget,
    ) -> Result<(), Error> {
        while !data.is_empty() {
            let maximum = data.len().min(WRITE_BUFFER - self.buffer.len());
            let count = match self.memory.extend_available(memory, maximum) {
                Ok(count) => count,
                Err(_) if !self.buffer.is_empty() => {
                    self.flush(held, &settings.directory).await?;
                    continue;
                }
                Err(_) => return Err(Error::Full),
            };
            self.buffer.extend_from_slice(&data[..count]);
            data = &data[count..];
            if self.buffer.len() == WRITE_BUFFER {
                self.flush(held, &settings.directory).await?;
            }
        }
        Ok(())
    }

    async fn append_chunk(
        &mut self,
        mut data: Vec<u8>,
        held: &mut Held,
        settings: &Settings,
        memory: &ByteBudget,
    ) -> Result<(), Error> {
        if data.len() >= WRITE_BUFFER {
            self.flush(held, &settings.directory).await?;
            let mut permit = BytePermit::default();
            if permit.extend(memory, data.len()).is_ok() {
                std::mem::swap(&mut self.buffer, &mut data);
                self.memory = permit;
                let result = self.flush(held, &settings.directory).await;
                std::mem::swap(&mut self.buffer, &mut data);
                return result;
            }
        }
        self.append(&data, held, settings, memory).await
    }
}

#[derive(Default)]
struct Parts {
    current: Current,
    field_bytes: usize,
    written: usize,
    fields: Vec<(String, String)>,
    files: Vec<Part>,
}

impl Parts {
    fn consume(
        &mut self,
        event: Event,
        settings: &Settings,
        max_fields: usize,
        memory_budget: &ByteBudget,
        held: &mut Held,
    ) -> Result<Option<Vec<u8>>, Error> {
        match event {
            Event::Begin(part) => {
                self.current = if part.filename.is_some() {
                    Current::File(part, self.written)
                } else {
                    Current::Field(part, Vec::new())
                };
            }
            Event::Data(bytes) => match &mut self.current {
                Current::Field(_, value) => {
                    self.field_bytes += bytes.len();
                    if self.field_bytes > max_fields {
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
                    self.written += bytes.len();
                    return Ok(Some(bytes));
                }
                Current::None => unreachable!("data only follows a part's start"),
            },
            Event::End => match std::mem::replace(&mut self.current, Current::None) {
                Current::Field(part, value) => {
                    let value = String::from_utf8(value)
                        .map_err(|_| Error::Malformed("Form fields must be UTF-8"))?;
                    self.fields.push((part.name, value));
                }
                Current::File(part, start) => {
                    self.files.push(Part {
                        data: start..self.written,
                        ..part
                    });
                }
                Current::None => unreachable!("a part ends after it starts"),
            },
        }
        Ok(None)
    }
}

enum Incoming {
    Flush,
    Frame(Option<Result<Frame<Bytes>, axum::Error>>),
}

async fn next_frame(body: &mut Body, buffered: bool) -> Incoming {
    poll_fn(|context| match Pin::new(&mut *body).poll_frame(context) {
        Poll::Ready(frame) => Poll::Ready(Incoming::Frame(frame)),
        Poll::Pending if buffered => Poll::Ready(Incoming::Flush),
        Poll::Pending => Poll::Pending,
    })
    .await
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
    let mut chunks = Vec::new();
    let mut received: u64 = 0;
    let mut writer = SpoolWriter::default();
    let mut parts = Parts::default();
    loop {
        let frame = match next_frame(&mut body, !writer.buffer.is_empty()).await {
            Incoming::Flush => {
                writer.flush(held, &settings.directory).await?;
                continue;
            }
            Incoming::Frame(None) => break,
            Incoming::Frame(Some(frame)) => frame,
        };
        let parsed = frame.map_err(|_| Error::Body).and_then(|frame| {
            let Ok(data) = frame.into_data() else {
                return Ok(());
            };
            received = received.saturating_add(data.len() as u64);
            if received > settings.max_upload_size {
                return Err(Error::TooLarge);
            }
            stream.push(&data, &mut events).map_err(Error::Malformed)?;
            Ok(())
        });
        if let Err(error) = parsed {
            writer.flush(held, &settings.directory).await?;
            return Err(error);
        }
        for event in events.drain(..) {
            if matches!(
                (&event, &parts.current),
                (Event::Data(_), Current::Field(..))
            ) {
                writer.flush(held, &settings.directory).await?;
            }
            match parts.consume(event, settings, max_fields, memory_budget, held) {
                Ok(Some(bytes)) => chunks.push(bytes),
                Ok(None) => {}
                Err(error) => {
                    writer.flush(held, &settings.directory).await?;
                    return Err(error);
                }
            }
        }
        for chunk in chunks.drain(..) {
            writer
                .append_chunk(chunk, held, settings, memory_budget)
                .await?;
        }
    }
    writer.flush(held, &settings.directory).await?;
    stream.finish().map_err(Error::Malformed)?;
    Ok(Spooled {
        fields: parts.fields,
        files: parts.files,
        file: writer.file.map(Arc::new),
        #[cfg(test)]
        writes: writer.writes,
    })
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

    struct Chunks(std::collections::VecDeque<Result<bytes::Bytes, std::io::Error>>);

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
                    .map(|chunk| chunk.map(http_body::Frame::data)),
            )
        }
    }

    fn chunked(data: Vec<u8>, size: usize) -> Body {
        Body::new(Chunks(
            data.chunks(size)
                .map(|chunk| Ok(bytes::Bytes::copy_from_slice(chunk)))
                .collect(),
        ))
    }

    fn previous_write_count(body: &[u8], size: usize) -> usize {
        let mut parser = forms::MultipartStream::new("b");
        let mut events = Vec::new();
        let mut file = false;
        let mut writes = 0;
        for chunk in body.chunks(size) {
            parser.push(chunk, &mut events).unwrap();
            let mut pending = false;
            for event in events.drain(..) {
                match event {
                    Event::Begin(part) => file = part.filename.is_some(),
                    Event::Data(data) if file && !data.is_empty() => pending = true,
                    _ => {}
                }
            }
            writes += usize::from(pending);
        }
        writes
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

    #[tokio::test]
    async fn ready_frames_share_bounded_writes_and_preserve_large_payloads() {
        let contents: Vec<u8> = (0..=255).cycle().take(1 << 20).collect();
        let body = multipart(&contents);
        for size in [7, 4093, 131_072] {
            let settings = settings(2 << 20, 2 << 20);
            let memory = ByteBudget::new(1 << 20);
            let mut held = Held::default();
            let started = std::time::Instant::now();
            let spooled = spool(
                chunked(body.clone(), size),
                "b",
                &settings,
                64,
                &memory,
                &mut held,
            )
            .await
            .unwrap();
            let elapsed = started.elapsed();
            let previous = previous_write_count(&body, size);
            if size < WRITE_BUFFER {
                assert_eq!(spooled.writes, contents.len().div_ceil(WRITE_BUFFER));
            } else {
                assert_eq!(spooled.writes, previous);
            }
            let mut read = vec![0u8; contents.len()];
            crate::platform::read_exact_at(spooled.file.as_ref().unwrap(), &mut read, 0).unwrap();
            assert_eq!(read, contents);
            assert_eq!(memory.available(), (1 << 20) - 5);
            eprintln!(
                "upload probe: {} bytes / {size}-byte frames / {} -> {} blocking writes / {:?}",
                contents.len(),
                previous,
                spooled.writes,
                elapsed,
            );
        }
    }

    #[tokio::test]
    async fn interleaved_fields_and_files_keep_offsets_with_tight_memory() {
        let mut body =
            b"--b\r\nContent-Disposition: form-data; name=\"first\"; filename=\"a\"\r\n\r\n"
                .to_vec();
        body.extend_from_slice(&[b'a'; 100]);
        body.extend_from_slice(b"\r\n--b\r\nContent-Disposition: form-data; name=\"note\"\r\n\r\n");
        body.extend_from_slice(&[b'n'; 60]);
        body.extend_from_slice(b"\r\n--b\r\nContent-Disposition: form-data; name=\"second\"; filename=\"b\"\r\n\r\nsecond!\r\n--b\r\nContent-Disposition: form-data; name=\"empty\"; filename=\"e\"\r\n\r\n\r\n--b--\r\n");
        for size in [7, body.len()] {
            let settings = settings(4096, 4096);
            let memory = ByteBudget::new(64);
            let mut held = Held::default();
            let spooled = spool(
                chunked(body.clone(), size),
                "b",
                &settings,
                64,
                &memory,
                &mut held,
            )
            .await
            .unwrap();
            assert_eq!(spooled.fields, vec![("note".to_owned(), "n".repeat(60))]);
            assert_eq!(
                spooled
                    .files
                    .iter()
                    .map(|part| part.data.clone())
                    .collect::<Vec<_>>(),
                vec![0..100, 100..107, 107..107],
            );
            let mut read = vec![0u8; 107];
            crate::platform::read_exact_at(spooled.file.as_ref().unwrap(), &mut read, 0).unwrap();
            assert_eq!(&read[..100], &[b'a'; 100]);
            assert_eq!(&read[100..], b"second!");
            assert_eq!(memory.available(), 4);
        }
    }

    #[tokio::test]
    async fn staging_is_charged_then_released_without_reallocating() {
        let settings = settings(4096, 4096);
        let memory = ByteBudget::new(8);
        let mut held = Held::default();
        held.memory.extend(&memory, 3).unwrap();
        let mut writer = SpoolWriter::default();
        writer
            .append(b"first", &mut held, &settings, &memory)
            .await
            .unwrap();
        let pointer = writer.buffer.as_ptr();
        assert_eq!(memory.available(), 0);
        writer.flush(&mut held, &settings.directory).await.unwrap();
        assert_eq!(memory.available(), 5);
        assert_eq!(writer.buffer.as_ptr(), pointer);
        writer
            .append(b"again", &mut held, &settings, &memory)
            .await
            .unwrap();
        assert_eq!(writer.buffer.as_ptr(), pointer);
        assert_eq!(memory.available(), 0);
        drop(writer);
        assert_eq!(memory.available(), 5);
        drop(held);
        assert_eq!(memory.available(), 8);
    }

    #[tokio::test]
    async fn large_frames_use_bounded_staging_when_memory_is_tight() {
        let contents = vec![b'x'; WRITE_BUFFER + 17];
        let settings = settings(1 << 20, 1 << 20);
        let memory = ByteBudget::new(1024);
        let mut held = Held::default();
        let spooled = spool(
            Body::from(multipart(&contents)),
            "b",
            &settings,
            64,
            &memory,
            &mut held,
        )
        .await
        .unwrap();
        assert_eq!(spooled.writes, contents.len().div_ceil(1019));
        assert_eq!(memory.available(), 1019);
        let mut read = vec![0u8; contents.len()];
        crate::platform::read_exact_at(spooled.file.as_ref().unwrap(), &mut read, 0).unwrap();
        assert_eq!(read, contents);
    }

    #[tokio::test]
    async fn cancellation_keeps_staging_and_request_permits_until_write_finishes() {
        let settings = settings(4096, 8);
        let disk = settings.budget.clone();
        let memory = ByteBudget::new(8);
        let capacity = Arc::new(tokio::sync::Semaphore::new(1));
        let mut held = Held {
            admission: Some(capacity.clone().try_acquire_owned().unwrap()),
            ..Held::default()
        };
        held.memory.extend(&memory, 3).unwrap();
        held.disk.extend(&disk, 5).unwrap();
        let mut writer = SpoolWriter::default();
        writer
            .append(b"first", &mut held, &settings, &memory)
            .await
            .unwrap();
        let (started, ready) = tokio::sync::oneshot::channel();
        let (release, blocked) = std::sync::mpsc::channel();
        let writing = tokio::spawn(async move {
            writer
                .flush_using(&mut held, move |_, data| {
                    started.send(()).unwrap();
                    blocked.recv().unwrap();
                    let mut file = create(&settings.directory)?;
                    file.write_all(data)?;
                    Ok(file)
                })
                .await
        });
        ready.await.unwrap();
        writing.abort();
        assert!(writing.await.unwrap_err().is_cancelled());
        assert_eq!(capacity.available_permits(), 0);
        assert_eq!(memory.available(), 0);
        assert_eq!(disk.available(), 3);
        release.send(()).unwrap();
        tokio::time::timeout(std::time::Duration::from_secs(2), async {
            while capacity.available_permits() != 1
                || memory.available() != 8
                || disk.available() != 8
            {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn prior_write_errors_precede_later_frame_errors() {
        let body = multipart(&[7; 128]);
        let mut settings = settings(body.len() as u64, 4096);
        settings.directory.push(format!(
            "bessemer-missing-{}-{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        let memory = ByteBudget::new(4096);
        for last in [
            Ok(Bytes::from_static(b"too large")),
            Err(std::io::Error::other("body failure")),
        ] {
            let frames = Body::new(Chunks([Ok(Bytes::copy_from_slice(&body)), last].into()));
            assert_eq!(
                spool(frames, "b", &settings, 64, &memory, &mut Held::default())
                    .await
                    .err(),
                Some(Error::Io)
            );
            assert_eq!(memory.available(), 4096);
            assert_eq!(settings.budget.available(), 4096);
        }
    }

    #[tokio::test]
    async fn current_frame_validation_precedes_its_write() {
        let mut settings = settings(4096, 4096);
        settings.directory.push(format!(
            "bessemer-missing-{}-{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        let memory = ByteBudget::new(4096);
        let mut body = b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x\"\r\n\r\nfile\r\n--b\r\nContent-Disposition: form-data; name=\"field\"\r\n\r\n".to_vec();
        body.extend_from_slice(&[b'x'; 65]);
        body.extend_from_slice(b"\r\n--b--\r\n");
        assert_eq!(
            spool(
                Body::from(body),
                "b",
                &settings,
                64,
                &memory,
                &mut Held::default()
            )
            .await
            .err(),
            Some(Error::FieldsTooLarge)
        );
        assert_eq!(memory.available(), 4096);
        assert_eq!(settings.budget.available(), 4096);
    }

    struct Paused {
        first: Option<Bytes>,
        last: Option<Bytes>,
        paused: bool,
        memory: ByteBudget,
    }

    impl http_body::Body for Paused {
        type Data = Bytes;
        type Error = std::io::Error;

        fn poll_frame(
            mut self: Pin<&mut Self>,
            context: &mut std::task::Context<'_>,
        ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
            if let Some(first) = self.first.take() {
                return Poll::Ready(Some(Ok(Frame::data(first))));
            }
            if !self.paused {
                self.paused = true;
                assert!(self.memory.available() < 1019);
                context.waker().wake_by_ref();
                return Poll::Pending;
            }
            if let Some(last) = self.last.take() {
                assert_eq!(self.memory.available(), 1019);
                return Poll::Ready(Some(Ok(Frame::data(last))));
            }
            Poll::Ready(None)
        }
    }

    #[tokio::test]
    async fn pending_body_flushes_staging_before_waiting_for_input() {
        let body = multipart(&[7; 128]);
        let split = body.len() - 9;
        let settings = settings(4096, 4096);
        let memory = ByteBudget::new(1024);
        let source = Body::new(Paused {
            first: Some(Bytes::copy_from_slice(&body[..split])),
            last: Some(Bytes::copy_from_slice(&body[split..])),
            paused: false,
            memory: memory.clone(),
        });
        let mut held = Held::default();
        let spooled = spool(source, "b", &settings, 64, &memory, &mut held)
            .await
            .unwrap();
        assert_eq!(spooled.writes, 2);
        assert_eq!(memory.available(), 1019);
    }
}
