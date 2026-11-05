//! Request deadlines without a Tokio timer per request.
//!
//! Every request with the same timeout gets a deadline later than (or equal
//! to) the one before it, so one FIFO per distinct timeout holds requests in
//! deadline order and needs no heap. A single sweeper task per server sleeps
//! until the earliest deadline at the front of any FIFO, marks the expired
//! requests and wakes them through the waker each registered.
//!
//! A request registers only once it first waits (a request that completes
//! without waiting never touches the queue), and the slot of a finished request
//! at the front of its FIFO is reused by the next registration, so a steady
//! stream of requests neither allocates nor touches Tokio's timer wheel.
//!
//! The sweeper never sleeps past now + the shortest timeout while requests
//! arrive, so a new registration, whose deadline is at least that far away,
//! only has to wake it in the rare case that its deadline is earlier than the
//! one the sweeper armed (a new, shorter timeout, or a registration that raced
//! the sweeper). After a whole idle period the sweeper parks without a timer
//! and the next registration wakes it.

use std::{
    collections::VecDeque,
    future::Future,
    pin::Pin,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU8, Ordering},
    },
    task::{Context, Poll, Waker},
    time::Duration,
};
use tokio::{sync::Notify, time::Instant};

const WAITING: u8 = 0;
const FIRED: u8 = 1;
const DONE: u8 = 2;
/// A FIFO is compacted (finished entries behind a waiting one removed) when
/// it reaches this length, then at twice its compacted length.
const COMPACT_AT: usize = 64;

/// The deadline passed before the request finished.
#[derive(Debug)]
pub(crate) struct Elapsed;

struct Slot {
    state: AtomicU8,
    waker: Mutex<Option<Waker>>,
}

struct Entry {
    deadline: Instant,
    slot: Arc<Slot>,
}

struct Lane {
    timeout: Duration,
    entries: VecDeque<Entry>,
    compact_at: usize,
}

#[derive(Default)]
struct Queue {
    lanes: Vec<Lane>,
    shortest: Option<Duration>,
    /// The sweeper task exists.
    running: bool,
    /// When the sweeper will next look; `None` while it is parked or has not
    /// looked yet.
    armed: Option<Instant>,
    /// A request registered since the sweeper last looked.
    registered: bool,
    closed: bool,
}

#[derive(Default)]
struct Shared {
    queue: Mutex<Queue>,
    notify: Notify,
}

/// The deadline queue of one server.
#[derive(Default)]
pub(crate) struct Deadlines {
    shared: Arc<Shared>,
}

impl Drop for Deadlines {
    fn drop(&mut self) {
        self.shared.queue.lock().unwrap().closed = true;
        self.shared.notify.notify_one();
    }
}

impl Deadlines {
    /// Run `future` until it completes or `deadline` (which is `timeout` after
    /// the request started) passes. Dropping the returned future drops
    /// `future`, as `tokio::time::timeout_at` does.
    pub(crate) fn within<F: Future + Unpin>(
        &self,
        timeout: Duration,
        deadline: Instant,
        future: F,
    ) -> Within<'_, F> {
        Within {
            deadlines: self,
            timeout,
            deadline,
            slot: None,
            future,
        }
    }

    fn register(&self, timeout: Duration, deadline: Instant, waker: &Waker) -> Arc<Slot> {
        let mut guard = self.shared.queue.lock().unwrap();
        let queue = &mut *guard;
        let index = match queue.lanes.iter().position(|lane| lane.timeout == timeout) {
            Some(index) => index,
            None => {
                queue.lanes.push(Lane {
                    timeout,
                    entries: VecDeque::new(),
                    compact_at: COMPACT_AT,
                });
                queue.shortest = Some(queue.shortest.map_or(timeout, |s| s.min(timeout)));
                queue.lanes.len() - 1
            }
        };
        let lane = &mut queue.lanes[index];
        // Finished requests at the front leave; the first one nobody else
        // holds becomes this request's slot.
        let mut reused = None;
        while lane
            .entries
            .front()
            .is_some_and(|front| front.slot.state.load(Ordering::Acquire) == DONE)
        {
            let mut entry = lane.entries.pop_front().unwrap();
            if reused.is_none()
                && let Some(slot) = Arc::get_mut(&mut entry.slot)
            {
                *slot.state.get_mut() = WAITING;
                let stored = slot.waker.get_mut().unwrap();
                if !stored.as_ref().is_some_and(|old| old.will_wake(waker)) {
                    *stored = Some(waker.clone());
                }
                reused = Some(entry.slot);
            }
        }
        let slot = reused.unwrap_or_else(|| {
            Arc::new(Slot {
                state: AtomicU8::new(WAITING),
                waker: Mutex::new(Some(waker.clone())),
            })
        });
        if lane.entries.len() >= lane.compact_at {
            lane.entries
                .retain(|entry| entry.slot.state.load(Ordering::Acquire) != DONE);
            lane.compact_at = (lane.entries.len() * 2).max(COMPACT_AT);
        }
        // Deadlines of one timeout are registered nearly in order (a request
        // registers when it first waits, not when its deadline was taken).
        let mut at = lane.entries.len();
        while at > 0 && lane.entries[at - 1].deadline > deadline {
            at -= 1;
        }
        lane.entries.insert(
            at,
            Entry {
                deadline,
                slot: slot.clone(),
            },
        );
        queue.registered = true;
        let start = !queue.running;
        let notify = match queue.armed {
            _ if start => false,
            None => true,
            Some(armed) => deadline < armed,
        };
        queue.running = true;
        drop(guard);
        if start {
            tokio::spawn(sweep(self.shared.clone()));
        } else if notify {
            self.shared.notify.notify_one();
        }
        slot
    }
}

/// Marks the sweeper gone if its task is dropped (runtime shutdown), so a
/// later registration would start another.
struct Running(Arc<Shared>);

impl Drop for Running {
    fn drop(&mut self) {
        let mut queue = self.0.queue.lock().unwrap();
        queue.running = false;
        queue.armed = None;
    }
}

async fn sweep(shared: Arc<Shared>) {
    let _running = Running(shared.clone());
    let sleep = tokio::time::sleep_until(Instant::now());
    tokio::pin!(sleep);
    let mut wakers = Vec::new();
    loop {
        let target = {
            let mut guard = shared.queue.lock().unwrap();
            let queue = &mut *guard;
            if queue.closed {
                return;
            }
            let now = Instant::now();
            let mut target = now + queue.shortest.unwrap_or(Duration::from_secs(1));
            let mut empty = true;
            for lane in &mut queue.lanes {
                while let Some(front) = lane.entries.front() {
                    if front.slot.state.load(Ordering::Acquire) == DONE {
                        lane.entries.pop_front();
                        continue;
                    }
                    if front.deadline > now {
                        target = target.min(front.deadline);
                        empty = false;
                        break;
                    }
                    let entry = lane.entries.pop_front().unwrap();
                    if entry
                        .slot
                        .state
                        .compare_exchange(WAITING, FIRED, Ordering::AcqRel, Ordering::Acquire)
                        .is_ok()
                        && let Some(waker) = entry.slot.waker.lock().unwrap().take()
                    {
                        wakers.push(waker);
                    }
                }
            }
            let park = empty && !queue.registered;
            queue.registered = false;
            queue.armed = (!park).then_some(target);
            (!park).then_some(target)
        };
        for waker in wakers.drain(..) {
            waker.wake();
        }
        let notified = shared.notify.notified();
        match target {
            Some(target) => {
                sleep.as_mut().reset(target);
                tokio::select! {
                    biased;
                    () = sleep.as_mut() => {}
                    () = notified => {}
                }
            }
            None => notified.await,
        }
    }
}

/// A request future bounded by its deadline.
pub(crate) struct Within<'a, F> {
    deadlines: &'a Deadlines,
    timeout: Duration,
    deadline: Instant,
    slot: Option<Arc<Slot>>,
    future: F,
}

impl<F: Future + Unpin> Future for Within<'_, F> {
    type Output = Result<F::Output, Elapsed>;

    fn poll(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<Self::Output> {
        let this = &mut *self;
        // Like Tokio's `Timeout`, the request is polled before its deadline;
        // a late ready response is turned into a timeout by the caller.
        if let Poll::Ready(output) = Pin::new(&mut this.future).poll(context) {
            return Poll::Ready(Ok(output));
        }
        match &this.slot {
            None => {
                this.slot = Some(this.deadlines.register(
                    this.timeout,
                    this.deadline,
                    context.waker(),
                ));
            }
            Some(slot) => {
                if slot.state.load(Ordering::Acquire) == FIRED {
                    return Poll::Ready(Err(Elapsed));
                }
                {
                    let mut stored = slot.waker.lock().unwrap();
                    if !stored
                        .as_ref()
                        .is_some_and(|old| old.will_wake(context.waker()))
                    {
                        *stored = Some(context.waker().clone());
                    }
                }
                // The sweeper marks a slot fired before taking its waker under
                // the same lock, so this second look cannot miss a firing.
                if slot.state.load(Ordering::Acquire) == FIRED {
                    return Poll::Ready(Err(Elapsed));
                }
            }
        }
        Poll::Pending
    }
}

impl<F> Drop for Within<'_, F> {
    fn drop(&mut self) {
        if let Some(slot) = &self.slot {
            slot.state.store(DONE, Ordering::Release);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::future::{pending, ready};

    fn waiting() -> Pin<Box<std::future::Pending<()>>> {
        Box::pin(pending())
    }

    async fn expire_on(deadlines: &Deadlines, timeout: Duration) -> Duration {
        let start = Instant::now();
        let result = deadlines.within(timeout, start + timeout, waiting()).await;
        assert!(result.is_err());
        start.elapsed()
    }

    #[tokio::test(flavor = "current_thread")]
    async fn fires_on_the_single_thread_runtime() {
        let deadlines = Deadlines::default();
        let elapsed = expire_on(&deadlines, Duration::from_millis(30)).await;
        assert!(elapsed >= Duration::from_millis(30), "{elapsed:?}");
        assert!(elapsed < Duration::from_secs(2), "{elapsed:?}");
        // The sweeper is now armed; a second request on the same lane fires too.
        let elapsed = expire_on(&deadlines, Duration::from_millis(30)).await;
        assert!(elapsed >= Duration::from_millis(30), "{elapsed:?}");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn fires_on_the_multi_thread_runtime() {
        let deadlines = Arc::new(Deadlines::default());
        let mut tasks = Vec::new();
        for _ in 0..8 {
            let deadlines = deadlines.clone();
            tasks.push(tokio::spawn(async move {
                expire_on(&deadlines, Duration::from_millis(20)).await
            }));
        }
        for task in tasks {
            let elapsed = task.await.unwrap();
            assert!(elapsed >= Duration::from_millis(20), "{elapsed:?}");
        }
    }

    #[tokio::test(start_paused = true)]
    async fn ready_requests_pass_and_completed_slots_are_reused() {
        let deadlines = Deadlines::default();
        let timeout = Duration::from_secs(30);
        // A request that never waits does not register.
        let result = deadlines
            .within(timeout, Instant::now() + timeout, Box::pin(ready(7)))
            .await;
        assert_eq!(result.unwrap(), 7);
        assert!(deadlines.shared.queue.lock().unwrap().lanes.is_empty());
        for _ in 0..1000 {
            let (sender, receiver) = tokio::sync::oneshot::channel();
            let request = deadlines.within(timeout, Instant::now() + timeout, receiver);
            let (result, _) = tokio::join!(request, async move {
                tokio::task::yield_now().await;
                sender.send(1).unwrap();
            });
            assert_eq!(result.unwrap().unwrap(), 1);
        }
        // Sequential requests reuse the one finished slot at the front.
        let queue = deadlines.shared.queue.lock().unwrap();
        assert_eq!(queue.lanes.len(), 1);
        assert_eq!(queue.lanes[0].entries.len(), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn a_shorter_timeout_registered_later_still_fires_on_time() {
        let deadlines = Deadlines::default();
        let long = Duration::from_secs(30);
        let start = Instant::now();
        let slow = deadlines.within(long, start + long, waiting());
        let quick = async {
            tokio::time::sleep(Duration::from_secs(1)).await;
            let short = Duration::from_millis(100);
            let begun = Instant::now();
            let result = deadlines.within(short, begun + short, waiting()).await;
            assert!(result.is_err());
            begun.elapsed()
        };
        tokio::select! {
            _ = slow => panic!("the long deadline fired first"),
            elapsed = quick => assert!(elapsed < Duration::from_millis(200), "{elapsed:?}"),
        }
        let result = deadlines.within(long, start + long, waiting()).await;
        assert!(result.is_err());
        assert!(start.elapsed() >= long);
    }

    #[tokio::test(start_paused = true)]
    async fn dropped_requests_never_fire_and_are_compacted() {
        let deadlines = Deadlines::default();
        let timeout = Duration::from_secs(30);
        // A waiting request at the front keeps finished ones behind it queued
        // until compaction bounds them.
        let mut blocker = Box::pin(deadlines.within(timeout, Instant::now() + timeout, waiting()));
        assert!(futures_poll(blocker.as_mut()).is_pending());
        for _ in 0..1000 {
            let mut request =
                Box::pin(deadlines.within(timeout, Instant::now() + timeout, waiting()));
            assert!(futures_poll(request.as_mut()).is_pending());
        }
        let length = deadlines.shared.queue.lock().unwrap().lanes[0]
            .entries
            .len();
        assert!(length <= 2 * COMPACT_AT, "{length}");
        drop(blocker);
        tokio::time::sleep(timeout * 2).await;
        tokio::task::yield_now().await;
        let queue = deadlines.shared.queue.lock().unwrap();
        assert!(queue.lanes[0].entries.is_empty());
    }

    fn futures_poll<F: Future>(future: Pin<&mut F>) -> Poll<F::Output> {
        let mut context = Context::from_waker(Waker::noop());
        future.poll(&mut context)
    }
}
