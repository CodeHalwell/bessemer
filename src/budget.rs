//! Logical retained-payload budgets. These do not bound application heap or sockets.

use axum::body::{Body, Bytes};
use http_body::Body as _;
use std::future::poll_fn;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::task::Poll;
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

#[derive(Clone)]
pub(crate) struct ByteBudget(Arc<Semaphore>);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Full;

/// Marks a response whose body is produced as it is sent (a file, read a
/// chunk at a time) and so reserves nothing in the response byte budget.
/// Whatever reads such a body into memory whole reserves its bytes first.
#[derive(Clone, Copy)]
pub(crate) struct Unreserved;

#[derive(Default)]
pub(crate) struct BytePermit(Option<OwnedSemaphorePermit>);

impl ByteBudget {
    pub(crate) fn new(bytes: usize) -> Self {
        Self(Arc::new(Semaphore::new(bytes)))
    }

    #[cfg(test)]
    pub(crate) fn available(&self) -> usize {
        self.0.available_permits()
    }

    pub(crate) fn acquire(&self, bytes: usize) -> Result<BytePermit, Full> {
        if bytes == 0 {
            return Ok(BytePermit::default());
        }
        let count = u32::try_from(bytes).map_err(|_| Full)?;
        self.0
            .clone()
            .try_acquire_many_owned(count)
            .map(|permit| BytePermit(Some(permit)))
            .map_err(|_| Full)
    }

    pub(crate) fn own<T>(&self, data: T) -> Result<Bytes, Full>
    where
        T: AsRef<[u8]> + Send + 'static,
    {
        let permit = self.acquire(data.as_ref().len())?;
        // Hyper can retain data Bytes after Body has yielded its final frame.
        // The permit must therefore belong to the Bytes owner, not a Body guard.
        Ok(Bytes::from_owner(Payload {
            data,
            _permit: permit,
        }))
    }
}

impl ByteBudget {
    /// Like `own`, for data whose bytes were reserved already by `acquire`.
    pub(crate) fn own_reserved<T>(data: T, permit: BytePermit) -> Bytes
    where
        T: AsRef<[u8]> + Send + 'static,
    {
        Bytes::from_owner(Payload {
            data,
            _permit: permit,
        })
    }

    /// Like `own`, but waits for capacity instead of failing while the budget is
    /// temporarily full. Fails only when the data can never fit.
    pub(crate) async fn own_waiting<T>(&self, data: T) -> Result<Bytes, Full>
    where
        T: AsRef<[u8]> + Send + 'static,
    {
        let count = u32::try_from(data.as_ref().len()).map_err(|_| Full)?;
        let permit = self
            .0
            .clone()
            .acquire_many_owned(count)
            .await
            .map_err(|_| Full)?;
        Ok(Bytes::from_owner(Payload {
            data,
            _permit: BytePermit(Some(permit)),
        }))
    }
}

impl BytePermit {
    /// Take over `other`'s reservation.
    pub(crate) fn absorb(&mut self, other: BytePermit) {
        if let Some(next) = other.0 {
            match &mut self.0 {
                Some(held) => held.merge(next),
                empty => *empty = Some(next),
            }
        }
    }

    pub(crate) fn extend(&mut self, budget: &ByteBudget, bytes: usize) -> Result<(), Full> {
        let next = budget.acquire(bytes)?;
        if let Some(next) = next.0 {
            match &mut self.0 {
                Some(held) => held.merge(next),
                empty => *empty = Some(next),
            }
        }
        Ok(())
    }

    pub(crate) fn extend_available(
        &mut self,
        budget: &ByteBudget,
        maximum: usize,
    ) -> Result<usize, Full> {
        if maximum == 0 {
            return Ok(0);
        }
        let count = maximum
            .min(budget.0.available_permits())
            .min(u32::MAX as usize);
        if count == 0 {
            return Err(Full);
        }
        let permit = budget.acquire(count).or_else(|_| budget.acquire(1))?;
        let reserved = permit.0.as_ref().unwrap().num_permits();
        self.absorb(permit);
        Ok(reserved)
    }
}

struct Payload<T> {
    data: T,
    _permit: BytePermit,
}

impl<T: AsRef<[u8]>> AsRef<[u8]> for Payload<T> {
    fn as_ref(&self) -> &[u8] {
        self.data.as_ref()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ReadError {
    Full,
    TooLarge,
    Malformed,
    /// More of the body had to be waited for on an HTTP/2 connection that has
    /// used its own places, and the worker's shared places are all taken.
    Waiting,
}

/// How many streams of one HTTP/2 connection may always wait for their bodies
/// without an admission slot, whatever other connections do.
///
/// Clients that upload in parallel commonly run four to eight transfers at
/// once (browsers kept six connections a host before HTTP/2), and a client
/// such as the `h2` library sends every request head before any body, so each
/// of those transfers waits at least briefly. Each place costs the memory of a
/// parked stream, about 7 KiB, so the worst case grows by this many streams a
/// connection beyond the shared pool.
pub(crate) const HTTP2_CONNECTION_WAITS: usize = 8;

/// The worker's shared places for requests waiting, without an admission
/// slot, for more of their bodies (see [`buffer_body`]).
///
/// A waiting request holds a connection, a deadline and its future but no
/// `max_in_flight` slot, so without a bound HTTP/2, which carries many streams
/// a connection, would let a client park that many per connection. HTTP/1.1
/// carries one request at a time, so its connections bound it already: its
/// reads take no place and are never refused. An HTTP/2 stream takes one of
/// its own connection's [`HTTP2_CONNECTION_WAITS`] places first and draws on
/// this pool, sized by `max_connections`, only beyond them. Parked streams can
/// therefore use up the pool but never another connection's own places, and
/// the worker's total is at most `HTTP2_CONNECTION_WAITS` a connection plus
/// the pool.
pub(crate) struct BodyWaits {
    waiting: AtomicUsize,
    limit: usize,
}

/// One HTTP/2 connection's own places to wait in, as a request extension.
/// Created once a connection; a request clones only the `Arc`.
#[derive(Clone, Default)]
pub(crate) struct ConnectionWaits(Arc<AtomicUsize>);

/// One request's place among those waiting for a body, given back on drop.
/// HTTP/1.1 reads hold one that counts nothing.
pub(crate) struct BodyWait<'a>(Option<&'a AtomicUsize>);

/// Take one of `limit` places counted by `count`, if one is free.
fn take(count: &AtomicUsize, limit: usize) -> Option<BodyWait<'_>> {
    count
        .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |waiting| {
            (waiting < limit).then_some(waiting + 1)
        })
        .ok()
        .map(|_| BodyWait(Some(count)))
}

impl BodyWaits {
    pub(crate) fn new(limit: usize) -> Self {
        Self {
            waiting: AtomicUsize::new(0),
            limit,
        }
    }

    /// A place to wait in for a request on `connection` (`None` for HTTP/1.1,
    /// which needs none), or `None` when the connection's own places and the
    /// shared ones are all taken. Only counts: nothing waits for a place, so
    /// no waiter list is kept or locked.
    pub(crate) fn enter<'a>(
        &'a self,
        connection: Option<&'a ConnectionWaits>,
    ) -> Option<BodyWait<'a>> {
        let Some(connection) = connection else {
            return Some(BodyWait(None));
        };
        take(&connection.0, HTTP2_CONNECTION_WAITS).or_else(|| take(&self.waiting, self.limit))
    }

    #[cfg(test)]
    pub(crate) fn waiting(&self) -> usize {
        self.waiting.load(Ordering::Relaxed)
    }
}

impl ConnectionWaits {
    #[cfg(test)]
    pub(crate) fn waiting(&self) -> usize {
        self.0.load(Ordering::Relaxed)
    }
}

impl Drop for BodyWait<'_> {
    fn drop(&mut self) {
        if let Some(count) = self.0 {
            count.fetch_sub(1, Ordering::Relaxed);
        }
    }
}

/// Frames at least this large are kept as they are while a body is
/// buffered; smaller ones are gathered into owned pieces of this size.
const SHARED_FRAME: usize = 8 * 1024;

enum Piece {
    Shared(Bytes),
    Owned(Vec<u8>),
}

/// Retain one non-empty frame of a body being buffered.
fn keep(pieces: &mut Vec<Piece>, data: Bytes) {
    // The first frame stays shared: a body of one frame needs no copy.
    let first = pieces.is_empty();
    match pieces.last_mut() {
        Some(Piece::Owned(owned)) if data.len() < SHARED_FRAME => {
            owned.extend_from_slice(&data);
        }
        _ if data.len() < SHARED_FRAME && !first => {
            // Room for a few more small frames, but never far beyond what the
            // byte budget counted: a tiny frame between large ones must not
            // reserve 8 KiB for each byte received.
            let mut owned = Vec::with_capacity((data.len() * 2).min(SHARED_FRAME));
            owned.extend_from_slice(&data);
            pieces.push(Piece::Owned(owned));
        }
        _ => pieces.push(Piece::Shared(data)),
    }
}

/// Read a whole request body into memory, charging `budget` as it arrives.
///
/// `slot` is the request's `max_in_flight` permit. It is given up the first
/// time the read has to wait for the client, so a request holds no slot while
/// its body is outstanding: a client that declares a body and sends it slowly,
/// or never, would otherwise keep one until the request deadline. The caller
/// takes a slot again once the body is complete. A body that is already here
/// (one that arrived with its head) never waits and keeps the slot it has.
///
/// A read that waits on an HTTP/2 connection (one whose request `extensions`
/// carry its [`ConnectionWaits`]) takes a place instead, held until it returns
/// or is dropped, and is refused (`ReadError::Waiting`) when none is free. A
/// body that never waits, including an empty one, looks for no place.
pub(crate) async fn buffer_body(
    mut body: Body,
    maximum: usize,
    budget: &ByteBudget,
    waits: &BodyWaits,
    extensions: &axum::http::Extensions,
    slot: &mut Option<OwnedSemaphorePermit>,
) -> Result<(Bytes, BytePermit), ReadError> {
    // A body that arrives as one frame is returned as it is, with no copy;
    // several are kept as they arrive and copied once, into an exact-size
    // buffer, at the end (one growing buffer copies them about twice).
    // Small frames are gathered into owned pieces as they arrive, so a body
    // of many tiny frames does not hold a frame handle per byte or two.
    let mut pieces: Vec<Piece> = Vec::new();
    let mut received = 0usize;
    let mut permit = BytePermit::default();
    // Dropped on every return, and with this future when it is cancelled.
    let mut waiting: Option<BodyWait<'_>> = None;
    loop {
        let frame = poll_fn(|context| match Pin::new(&mut body).poll_frame(context) {
            Poll::Pending => {
                *slot = None;
                if waiting.is_none() {
                    match waits.enter(extensions.get::<ConnectionWaits>()) {
                        Some(place) => waiting = Some(place),
                        None => return Poll::Ready(Err(ReadError::Waiting)),
                    }
                }
                Poll::Pending
            }
            Poll::Ready(frame) => Poll::Ready(Ok(frame)),
        })
        .await?;
        let Some(frame) = frame else {
            break;
        };
        let frame = frame.map_err(|_| ReadError::Malformed)?;
        let Ok(data) = frame.into_data() else {
            continue;
        };
        if received
            .checked_add(data.len())
            .is_none_or(|length| length > maximum)
        {
            return Err(ReadError::TooLarge);
        }
        // Charge actual received bytes before retaining our copy. A single merged
        // permit avoids allocating one guard for every tiny/chunked frame.
        permit
            .extend(budget, data.len())
            .map_err(|_| ReadError::Full)?;
        received += data.len();
        if data.is_empty() {
            continue;
        }
        // A terminal frame needs neither a piece allocation nor another
        // poll. An end-of-stream hint guarantees there are no more frames.
        if pieces.is_empty() && body.is_end_stream() {
            return Ok((data, permit));
        }
        keep(&mut pieces, data);
    }
    let body = match pieces.len() {
        0 => Bytes::new(),
        1 => match pieces.pop().expect("one piece") {
            Piece::Shared(data) => data,
            Piece::Owned(data) => Bytes::from(data),
        },
        _ => {
            let mut output = Vec::with_capacity(received);
            for piece in &pieces {
                output.extend_from_slice(match piece {
                    Piece::Shared(data) => data,
                    Piece::Owned(data) => data,
                });
            }
            Bytes::from(output)
        }
    };
    Ok((body, permit))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::Extensions;
    use http_body::{Body as HttpBody, Frame};
    use std::{
        collections::VecDeque,
        pin::Pin,
        task::{Context, Poll},
        time::Duration,
    };

    struct Frames {
        items: VecDeque<Result<Frame<Bytes>, std::io::Error>>,
        pending: bool,
    }

    impl HttpBody for Frames {
        type Data = Bytes;
        type Error = std::io::Error;

        fn poll_frame(
            mut self: Pin<&mut Self>,
            _: &mut Context<'_>,
        ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
            if let Some(item) = self.items.pop_front() {
                Poll::Ready(Some(item))
            } else if self.pending {
                Poll::Pending
            } else {
                Poll::Ready(None)
            }
        }
    }

    static UNBOUNDED: BodyWaits = BodyWaits {
        waiting: AtomicUsize::new(0),
        limit: usize::MAX,
    };

    fn frames(items: &[&'static [u8]], pending: bool) -> Body {
        Body::new(Frames {
            items: items
                .iter()
                .map(|value| Ok(Frame::data(Bytes::from_static(value))))
                .collect(),
            pending,
        })
    }

    #[test]
    fn weighted_guards_merge_and_release_as_one_owner() {
        let budget = ByteBudget::new(8);
        let mut permit = BytePermit::default();
        for _ in 0..8 {
            permit.extend(&budget, 1).unwrap();
        }
        assert_eq!(budget.0.available_permits(), 0);
        assert_eq!(permit.0.as_ref().unwrap().num_permits(), 8);
        assert_eq!(permit.extend(&budget, 1), Err(Full));
        drop(permit);
        assert_eq!(budget.0.available_permits(), 8);
    }

    #[test]
    fn partial_reservations_use_available_capacity_and_merge() {
        let budget = ByteBudget::new(8);
        let mut permit = BytePermit::default();
        assert_eq!(permit.extend_available(&budget, 3), Ok(3));
        assert_eq!(permit.extend_available(&budget, 10), Ok(5));
        assert_eq!(budget.available(), 0);
        assert_eq!(permit.0.as_ref().unwrap().num_permits(), 8);
        assert_eq!(permit.extend_available(&budget, 1), Err(Full));
        assert_eq!(permit.extend_available(&budget, 0), Ok(0));
        drop(permit);
        assert_eq!(budget.available(), 8);
    }

    #[test]
    fn output_budget_lives_until_final_data_clone_or_slice_is_dropped() {
        let budget = ByteBudget::new(8);
        let bytes = budget.own(vec![1u8; 8]).unwrap();
        let clone = bytes.clone();
        let slice = bytes.slice(0..1);
        drop(bytes);
        drop(clone);
        assert_eq!(budget.0.available_permits(), 0);
        assert_eq!(budget.own(vec![0u8]), Err(Full));
        assert!(budget.own(Vec::<u8>::new()).is_ok());
        drop(slice);
        assert_eq!(budget.0.available_permits(), 8);
    }

    #[test]
    fn static_output_shares_startup_storage_but_reserves_each_response() {
        let budget = ByteBudget::new(8);
        let data: Arc<[u8]> = vec![2u8; 4].into();
        let first = budget.own(data.clone()).unwrap();
        let second = budget.own(data.clone()).unwrap();
        assert_eq!(first.as_ptr(), data.as_ptr());
        assert_eq!(second.as_ptr(), data.as_ptr());
        assert_eq!(budget.0.available_permits(), 0);
        drop(first);
        assert_eq!(budget.0.available_permits(), 4);
        drop(second);
        assert_eq!(budget.0.available_permits(), 8);
    }

    #[tokio::test]
    async fn input_charges_actual_chunks_and_retains_the_merged_guard() {
        let budget = ByteBudget::new(8);
        let (data, permit) = buffer_body(
            frames(&[b"12", b"345"], false),
            8,
            &budget,
            &UNBOUNDED,
            &Extensions::new(),
            &mut None,
        )
        .await
        .unwrap();
        assert_eq!(&data[..], b"12345");
        assert_eq!(budget.0.available_permits(), 3);
        drop(data);
        assert_eq!(budget.0.available_permits(), 3);
        drop(permit);
        assert_eq!(budget.0.available_permits(), 8);
    }

    #[tokio::test]
    async fn large_and_small_frames_are_joined_in_order() {
        static LARGE: [u8; 9000] = [7u8; 9000];
        static OTHER: [u8; 8192] = [9u8; 8192];
        let budget = ByteBudget::new(64 * 1024);
        let items: &[&'static [u8]] = &[b"ab", &LARGE, b"c", b"", b"de", &OTHER, b"f"];
        let (data, permit) = buffer_body(
            frames(items, false),
            64 * 1024,
            &budget,
            &UNBOUNDED,
            &Extensions::new(),
            &mut None,
        )
        .await
        .unwrap();
        assert_eq!(data, items.concat());
        assert_eq!(budget.0.available_permits(), 64 * 1024 - data.len());
        drop(permit);
    }

    #[test]
    fn small_frames_between_large_ones_reserve_at_most_twice_their_bytes() {
        static LARGE: [u8; 8192] = [1u8; 8192];
        let mut pieces = Vec::new();
        for _ in 0..64 {
            keep(&mut pieces, Bytes::from_static(&LARGE));
            keep(&mut pieces, Bytes::from_static(b"x"));
        }
        let reserved: usize = pieces
            .iter()
            .map(|piece| match piece {
                Piece::Owned(owned) => owned.capacity(),
                Piece::Shared(_) => 0,
            })
            .sum();
        assert_eq!(pieces.len(), 128);
        assert!(reserved <= 2 * 64, "reserved {reserved} bytes for 64");
    }

    #[tokio::test]
    async fn a_single_frame_is_returned_without_copying() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        struct SingleFrame {
            frame: Option<Bytes>,
            polls: Arc<AtomicUsize>,
            reports_end: bool,
        }

        impl HttpBody for SingleFrame {
            type Data = Bytes;
            type Error = std::io::Error;

            fn poll_frame(
                mut self: Pin<&mut Self>,
                _: &mut Context<'_>,
            ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
                self.polls.fetch_add(1, Ordering::Relaxed);
                Poll::Ready(self.frame.take().map(Frame::data).map(Ok))
            }

            fn is_end_stream(&self) -> bool {
                self.reports_end && self.frame.is_none()
            }
        }

        for (reports_end, expected_polls) in [(true, 1), (false, 2)] {
            let budget = ByteBudget::new(8);
            let frame = Bytes::from_static(b"1234");
            let address = frame.as_ptr();
            let polls = Arc::new(AtomicUsize::new(0));
            let body = Body::new(SingleFrame {
                frame: Some(frame),
                polls: polls.clone(),
                reports_end,
            });
            let admission = Arc::new(Semaphore::new(1));
            let mut slot = Some(admission.try_acquire_owned().unwrap());
            let (data, permit) =
                buffer_body(body, 4, &budget, &UNBOUNDED, &Extensions::new(), &mut slot)
                    .await
                    .unwrap();
            assert_eq!(data.as_ptr(), address);
            assert_eq!(polls.load(Ordering::Relaxed), expected_polls);
            assert!(slot.is_some());
            assert_eq!(budget.0.available_permits(), 4);
            drop(permit);
            assert_eq!(budget.0.available_permits(), 8);
        }
    }

    #[tokio::test]
    async fn rejected_uploads_release_all_prior_chunk_reservations() {
        let budget = ByteBudget::new(4);
        let result = buffer_body(
            frames(&[b"123", b"45"], false),
            8,
            &budget,
            &UNBOUNDED,
            &Extensions::new(),
            &mut None,
        )
        .await;
        assert!(matches!(result, Err(ReadError::Full)));
        assert_eq!(budget.0.available_permits(), 4);
        let result = buffer_body(
            frames(&[b"123", b"45"], false),
            4,
            &budget,
            &UNBOUNDED,
            &Extensions::new(),
            &mut None,
        )
        .await;
        assert!(matches!(result, Err(ReadError::TooLarge)));
        assert_eq!(budget.0.available_permits(), 4);
        let body = Body::new(Frames {
            items: [
                Ok(Frame::data(Bytes::from_static(b"123"))),
                Err(std::io::Error::other("invalid frame")),
            ]
            .into(),
            pending: false,
        });
        assert!(matches!(
            buffer_body(body, 4, &budget, &UNBOUNDED, &Extensions::new(), &mut None).await,
            Err(ReadError::Malformed)
        ));
        assert_eq!(budget.0.available_permits(), 4);
    }

    /// A body whose first frame is here and whose second has to be waited for.
    struct Late(u8);

    impl HttpBody for Late {
        type Data = Bytes;
        type Error = std::io::Error;

        fn poll_frame(
            mut self: Pin<&mut Self>,
            context: &mut Context<'_>,
        ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
            self.0 += 1;
            match self.0 {
                1 => Poll::Ready(Some(Ok(Frame::data(Bytes::from_static(b"12"))))),
                2 => {
                    context.waker().wake_by_ref();
                    Poll::Pending
                }
                3 => Poll::Ready(Some(Ok(Frame::data(Bytes::from_static(b"34"))))),
                _ => Poll::Ready(None),
            }
        }
    }

    #[tokio::test]
    async fn a_slot_is_kept_by_a_body_that_is_here_and_given_up_by_one_that_is_not() {
        let budget = ByteBudget::new(8);
        let capacity = Arc::new(Semaphore::new(1));
        let mut slot = Some(capacity.clone().try_acquire_owned().unwrap());
        let (data, _permit) = buffer_body(
            frames(&[b"12", b"34"], false),
            8,
            &budget,
            &UNBOUNDED,
            &Extensions::new(),
            &mut slot,
        )
        .await
        .unwrap();
        assert_eq!(&data[..], b"1234");
        assert!(slot.is_some());
        assert_eq!(capacity.available_permits(), 0);

        let (data, _permit) = buffer_body(
            Body::new(Late(0)),
            8,
            &budget,
            &UNBOUNDED,
            &Extensions::new(),
            &mut slot,
        )
        .await
        .unwrap();
        assert_eq!(&data[..], b"1234");
        assert!(slot.is_none());
        assert_eq!(capacity.available_permits(), 1);
    }

    #[tokio::test]
    async fn a_body_that_never_arrives_holds_no_slot() {
        let budget = ByteBudget::new(8);
        let capacity = Arc::new(Semaphore::new(1));
        let mut slot = Some(capacity.clone().try_acquire_owned().unwrap());
        let http1 = Extensions::new();
        let mut read = std::pin::pin!(buffer_body(
            frames(&[b"1"], true),
            8,
            &budget,
            &UNBOUNDED,
            &http1,
            &mut slot
        ));
        let mut context = Context::from_waker(std::task::Waker::noop());
        assert!(std::future::Future::poll(read.as_mut(), &mut context).is_pending());
        // Still being read, and its received byte is still charged.
        assert_eq!(capacity.available_permits(), 1);
        assert_eq!(budget.available(), 7);
    }

    /// The request extensions of a stream on the HTTP/2 connection `own`.
    fn http2(own: &ConnectionWaits) -> Extensions {
        let mut extensions = Extensions::new();
        extensions.insert(own.clone());
        extensions
    }

    /// Take every one of `own`'s places, as its other parked streams would.
    fn fill<'a>(waits: &'a BodyWaits, own: &'a ConnectionWaits) -> Vec<BodyWait<'a>> {
        (0..HTTP2_CONNECTION_WAITS)
            .map(|_| waits.enter(Some(own)).unwrap())
            .collect()
    }

    #[tokio::test]
    async fn a_body_that_is_here_never_takes_a_place_to_wait() {
        // No place at all: a read that asked for one would be refused.
        let waits = BodyWaits::new(0);
        let own = ConnectionWaits::default();
        let _full = fill(&waits, &own);
        let extensions = http2(&own);
        let budget = ByteBudget::new(16);
        let (data, _permit) = buffer_body(
            frames(&[b"12", b"34"], false),
            8,
            &budget,
            &waits,
            &extensions,
            &mut None,
        )
        .await
        .unwrap();
        assert_eq!(&data[..], b"1234");
        let (data, _permit) =
            buffer_body(Body::empty(), 8, &budget, &waits, &extensions, &mut None)
                .await
                .unwrap();
        assert!(data.is_empty());
        let (data, _permit) = buffer_body(
            Body::from("1234"),
            8,
            &budget,
            &waits,
            &extensions,
            &mut None,
        )
        .await
        .unwrap();
        assert_eq!(&data[..], b"1234");
        assert_eq!(waits.waiting(), 0);
        assert_eq!(own.waiting(), HTTP2_CONNECTION_WAITS);
        // The first wait is refused, with its received bytes released.
        let available = budget.available();
        assert_eq!(
            buffer_body(
                Body::new(Late(0)),
                8,
                &budget,
                &waits,
                &extensions,
                &mut None
            )
            .await
            .err(),
            Some(ReadError::Waiting)
        );
        assert_eq!(budget.available(), available);
        assert_eq!(own.waiting(), HTTP2_CONNECTION_WAITS);
    }

    #[tokio::test]
    async fn http1_reads_take_no_place_and_are_never_refused() {
        // Not a single shared place, and none needed.
        let waits = BodyWaits::new(0);
        let budget = ByteBudget::new(8);
        let http1 = Extensions::new();
        let mut context = Context::from_waker(std::task::Waker::noop());
        let mut slots: Vec<Option<OwnedSemaphorePermit>> = (0..4).map(|_| None).collect();
        let mut parked = Vec::new();
        for slot in &mut slots {
            let mut read = Box::pin(buffer_body(
                frames(&[b"1"], true),
                8,
                &budget,
                &waits,
                &http1,
                slot,
            ));
            assert!(std::future::Future::poll(read.as_mut(), &mut context).is_pending());
            parked.push(read);
        }
        let (data, _permit) =
            buffer_body(Body::new(Late(0)), 8, &budget, &waits, &http1, &mut None)
                .await
                .unwrap();
        assert_eq!(&data[..], b"1234");
        assert_eq!(waits.waiting(), 0);
    }

    #[tokio::test]
    async fn waiting_reads_are_bounded_and_give_their_places_back() {
        let waits = BodyWaits::new(1);
        let own = ConnectionWaits::default();
        let other = ConnectionWaits::default();
        let extensions = http2(&own);
        let budget = ByteBudget::new(8);
        let unbudgeted = ByteBudget::new(64);
        let capacity = Arc::new(Semaphore::new(2));
        let mut context = Context::from_waker(std::task::Waker::noop());

        // The connection's own places are used before the shared one.
        let mut slots: Vec<Option<OwnedSemaphorePermit>> =
            (0..HTTP2_CONNECTION_WAITS).map(|_| None).collect();
        let mut parked = Vec::new();
        for (count, slot) in (1..).zip(&mut slots) {
            let mut read = Box::pin(buffer_body(
                frames(&[], true),
                8,
                &unbudgeted,
                &waits,
                &extensions,
                slot,
            ));
            assert!(std::future::Future::poll(read.as_mut(), &mut context).is_pending());
            parked.push(read);
            assert_eq!((own.waiting(), waits.waiting()), (count, 0));
        }
        let mut first_slot = Some(capacity.clone().try_acquire_owned().unwrap());
        let mut first = Box::pin(buffer_body(
            frames(&[b"1"], true),
            8,
            &budget,
            &waits,
            &extensions,
            &mut first_slot,
        ));
        assert!(std::future::Future::poll(first.as_mut(), &mut context).is_pending());
        assert_eq!(
            (own.waiting(), waits.waiting()),
            (HTTP2_CONNECTION_WAITS, 1)
        );

        // Every place is taken: the next read that has to wait is refused at
        // once, gives up its slot and keeps none of its bytes.
        let mut second_slot = Some(capacity.clone().try_acquire_owned().unwrap());
        let refused = buffer_body(
            frames(&[b"2"], true),
            8,
            &budget,
            &waits,
            &extensions,
            &mut second_slot,
        )
        .await;
        assert_eq!(refused.err(), Some(ReadError::Waiting));
        assert!(second_slot.is_none());
        assert_eq!(budget.available(), 7);
        assert_eq!(waits.waiting(), 1);
        // A body that is here is still read in full.
        let (data, permit) =
            buffer_body(Body::from("34"), 8, &budget, &waits, &extensions, &mut None)
                .await
                .unwrap();
        assert_eq!(&data[..], b"34");
        drop(permit);
        // Another connection still has its own places.
        let elsewhere = http2(&other);
        let (data, _permit) = buffer_body(
            Body::new(Late(0)),
            8,
            &unbudgeted,
            &waits,
            &elsewhere,
            &mut None,
        )
        .await
        .unwrap();
        assert_eq!(&data[..], b"1234");
        assert_eq!(other.waiting(), 0);

        // Cancelled (a request deadline, a reset stream): its place is free.
        drop(first);
        assert_eq!(waits.waiting(), 0);
        assert_eq!(budget.available(), 8);
        drop(parked);
        assert_eq!(own.waiting(), 0);

        // One that waits and completes holds its place only until it returns.
        let (data, _permit) = buffer_body(
            Body::new(Late(0)),
            8,
            &budget,
            &waits,
            &extensions,
            &mut None,
        )
        .await
        .unwrap();
        assert_eq!(&data[..], b"1234");
        assert_eq!((own.waiting(), waits.waiting()), (0, 0));
        // And a failed one gives it back too.
        assert_eq!(
            buffer_body(
                Body::new(Late(0)),
                3,
                &budget,
                &waits,
                &extensions,
                &mut None
            )
            .await
            .err(),
            Some(ReadError::TooLarge)
        );
        assert_eq!((own.waiting(), waits.waiting()), (0, 0));
    }

    #[test]
    fn places_to_wait_are_counted_exactly() {
        let waits = BodyWaits::new(2);
        let (first, second) = (ConnectionWaits::default(), ConnectionWaits::default());
        // HTTP/1.1 counts nothing.
        let http1: Vec<_> = (0..100).map(|_| waits.enter(None).unwrap()).collect();
        assert_eq!(waits.waiting(), 0);
        let own = fill(&waits, &first);
        assert_eq!(
            (first.waiting(), waits.waiting()),
            (HTTP2_CONNECTION_WAITS, 0)
        );
        let shared = waits.enter(Some(&first)).unwrap();
        let more = waits.enter(Some(&first)).unwrap();
        assert!(waits.enter(Some(&first)).is_none());
        assert_eq!(waits.waiting(), 2);
        // The pool is gone, but the second connection's own places are not.
        let theirs = fill(&waits, &second);
        assert!(waits.enter(Some(&second)).is_none());
        // A shared place given back goes to whoever asks next.
        drop(shared);
        let taken = waits.enter(Some(&second)).unwrap();
        assert!(waits.enter(Some(&first)).is_none());
        drop((http1, own, more, theirs, taken));
        assert_eq!(
            (first.waiting(), second.waiting(), waits.waiting()),
            (0, 0, 0)
        );
    }

    #[test]
    fn concurrent_takes_and_returns_never_exceed_or_leak_a_place() {
        use std::sync::atomic::AtomicBool;
        const POOL: usize = 3;
        let waits = BodyWaits::new(POOL);
        let connections: Vec<ConnectionWaits> =
            (0..4).map(|_| ConnectionWaits::default()).collect();
        let over = AtomicBool::new(false);
        let mut taken = [0usize; 8];
        std::thread::scope(|scope| {
            for (thread, taken) in taken.iter_mut().enumerate() {
                let (waits, connections, over) = (&waits, &connections, &over);
                scope.spawn(move || {
                    let own = &connections[thread % connections.len()];
                    let mut held = Vec::new();
                    for round in 0..20_000usize {
                        if round % 3 == 2 || held.len() > HTTP2_CONNECTION_WAITS {
                            held.pop();
                        } else if let Some(place) = waits.enter(Some(own)) {
                            held.push(place);
                            *taken += 1;
                        }
                        if own.waiting() > HTTP2_CONNECTION_WAITS || waits.waiting() > POOL {
                            over.store(true, Ordering::Relaxed);
                        }
                    }
                });
            }
        });
        assert!(!over.load(Ordering::Relaxed));
        // Places were taken; which thread got them is up to the scheduler.
        assert!(taken.iter().sum::<usize>() > 0);
        assert_eq!(waits.waiting(), 0);
        assert!(connections.iter().all(|own| own.waiting() == 0));
    }

    #[tokio::test]
    async fn dropping_a_partial_upload_releases_its_reservation() {
        let budget = ByteBudget::new(4);
        let result = tokio::time::timeout(
            Duration::from_millis(1),
            buffer_body(
                frames(&[b"123"], true),
                4,
                &budget,
                &UNBOUNDED,
                &Extensions::new(),
                &mut None,
            ),
        )
        .await;
        assert!(result.is_err());
        assert_eq!(budget.0.available_permits(), 4);
    }
}
