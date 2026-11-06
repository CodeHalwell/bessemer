//! Logical retained-payload budgets. These do not bound application heap or sockets.

use axum::body::{Body, Bytes};
use http_body_util::BodyExt;
use std::sync::Arc;
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

#[derive(Clone)]
pub(crate) struct ByteBudget(Arc<Semaphore>);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Full;

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
}

pub(crate) async fn buffer_body(
    mut body: Body,
    maximum: usize,
    budget: &ByteBudget,
) -> Result<(Bytes, BytePermit), ReadError> {
    // A body that arrives as one frame is returned as it is, with no copy.
    let mut first: Option<Bytes> = None;
    let mut output = Vec::new();
    let mut received = 0usize;
    let mut permit = BytePermit::default();
    while let Some(frame) = body.frame().await {
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
        if first.is_none() && output.is_empty() {
            first = Some(data);
        } else {
            if let Some(head) = first.take() {
                output.extend_from_slice(&head);
            }
            output.extend_from_slice(&data);
        }
    }
    let body = match first {
        Some(data) => data,
        None => Bytes::from(output),
    };
    Ok((body, permit))
}

#[cfg(test)]
mod tests {
    use super::*;
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
        let (data, permit) = buffer_body(frames(&[b"12", b"345"], false), 8, &budget)
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
    async fn a_single_frame_is_returned_without_copying() {
        let budget = ByteBudget::new(8);
        let frame = Bytes::from_static(b"1234");
        let address = frame.as_ptr();
        let body = Body::from(frame);
        let (data, permit) = buffer_body(body, 8, &budget).await.unwrap();
        assert_eq!(data.as_ptr(), address);
        assert_eq!(budget.0.available_permits(), 4);
        drop(permit);
        assert_eq!(budget.0.available_permits(), 8);
    }

    #[tokio::test]
    async fn rejected_uploads_release_all_prior_chunk_reservations() {
        let budget = ByteBudget::new(4);
        let result = buffer_body(frames(&[b"123", b"45"], false), 8, &budget).await;
        assert!(matches!(result, Err(ReadError::Full)));
        assert_eq!(budget.0.available_permits(), 4);
        let result = buffer_body(frames(&[b"123", b"45"], false), 4, &budget).await;
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
            buffer_body(body, 4, &budget).await,
            Err(ReadError::Malformed)
        ));
        assert_eq!(budget.0.available_permits(), 4);
    }

    #[tokio::test]
    async fn dropping_a_partial_upload_releases_its_reservation() {
        let budget = ByteBudget::new(4);
        let result = tokio::time::timeout(
            Duration::from_millis(1),
            buffer_body(frames(&[b"123"], true), 4, &budget),
        )
        .await;
        assert!(result.is_err());
        assert_eq!(budget.0.available_permits(), 4);
    }
}
