//! Delay-based admission. Suspended handlers consume memory, not loop time:
//! a healthy sleeping workload may use the safety ceiling without being shed.
//!
//! The signal is how long a request waits for its event loop to start it. A
//! wait above the target that persists marks the loop congested. Not every
//! long wait is the loop's doing, though. With the GIL, threads running
//! CPU-bound sync handlers, CPU executor work or the application's own
//! threads hold it in turn, and the loop thread waits a switch interval or
//! more each time it wants it back. The loop has almost nothing to do and is
//! not overloaded; shedding requests for it would refuse work the server can
//! do. So a late request counts only if the loop thread worked while it
//! waited, or the rest of the process was idle (a loop blocked in a call, or
//! a host with no CPU to give). It is excused when the loop thread used less
//! than a target of CPU time and the rest of the process was running for at
//! least half of the wait.
use std::sync::{
    Mutex, OnceLock,
    atomic::{AtomicBool, AtomicU32, Ordering},
};
use std::time::Duration;
use tokio::time::Instant;

pub(crate) const CEILING: usize = 4096;

/// Batches whose CPU readings are kept. A request is started in the batch
/// after the one it was queued behind, so two would do; four leaves room.
const MARKS: usize = 4;

/// CPU time of the loop's thread and of the whole process, read on that
/// thread. `None` where the platform has no such clocks: every late request
/// then counts, as it did before these were consulted.
pub(crate) type Cpu = Option<(Duration, Duration)>;

#[derive(Default)]
pub(crate) struct Delay {
    target: OnceLock<Duration>,
    congested: AtomicBool,
    tracking: AtomicBool,
    /// Number of the newest mark, or zero while none is being kept. A
    /// request carries the number current when it was queued.
    newest: AtomicU32,
    state: Mutex<State>,
}

#[derive(Default)]
struct State {
    above_since: Option<Instant>,
    until: Option<Instant>,
    marks: [Option<Mark>; MARKS],
}

/// The clocks at the end of one batch.
#[derive(Clone, Copy)]
struct Mark {
    number: u32,
    at: Instant,
    worked: Duration,
    process: Duration,
}

impl Delay {
    pub(crate) fn enable(&self, timeout: Duration) {
        let target = (timeout / 10).clamp(Duration::from_micros(100), Duration::from_millis(5));
        let _ = self.target.set(target);
    }

    /// Precompute the sample's target deadline once when it is queued.
    /// Comparing instants avoids two timespec subtractions on healthy requests.
    #[inline]
    pub(crate) fn deadline(&self, started: Instant) -> Instant {
        self.target
            .get()
            .map_or(started, |target| started + *target)
    }

    /// What a request being queued keeps to be judged against later: which
    /// batch the loop had last finished. One atomic load, on any thread.
    #[inline]
    pub(crate) fn stamp(&self) -> u32 {
        self.newest.load(Ordering::Relaxed)
    }

    /// Queue observation reuses invocation timestamps; batch observation adds
    /// one completion sample. Measure loop work and queueing, not handler RTT.
    /// `cpu` is only called for a late sample, on the loop's thread.
    #[inline]
    pub(crate) fn observe(
        &self,
        deadline: Instant,
        now: Instant,
        stamp: u32,
        cpu: impl FnOnce() -> Cpu,
    ) {
        if now <= deadline {
            if self.tracking.load(Ordering::Relaxed) {
                self.tracking.store(false, Ordering::Relaxed);
                self.newest.store(0, Ordering::Relaxed);
                self.state.lock().unwrap().above_since = None;
            }
            return;
        }
        self.delayed(now, stamp, cpu());
    }

    /// The loop has finished a batch. While requests are running late its
    /// clocks are kept, so that the next late request can be judged by what
    /// happened while it waited. Costs one atomic load otherwise.
    #[inline]
    pub(crate) fn batch_done(&self, now: Instant, cpu: impl FnOnce() -> Cpu) {
        if self.tracking.load(Ordering::Relaxed) {
            self.mark(now, cpu());
        }
    }

    #[cold]
    fn mark(&self, now: Instant, cpu: Cpu) {
        let Some((worked, process)) = cpu else {
            return;
        };
        let mut state = self.state.lock().unwrap();
        // Zero means no mark, so the count skips it when it wraps.
        let number = match self.newest.load(Ordering::Relaxed).wrapping_add(1) {
            0 => 1,
            number => number,
        };
        state.marks[number as usize % MARKS] = Some(Mark {
            number,
            at: now,
            worked,
            process,
        });
        self.newest.store(number, Ordering::Relaxed);
    }

    #[cold]
    fn delayed(&self, now: Instant, stamp: u32, cpu: Cpu) {
        let Some(target) = self.target.get() else {
            return;
        };
        let mut state = self.state.lock().unwrap();
        self.tracking.store(true, Ordering::Relaxed);
        let mark = state.marks[stamp as usize % MARKS].filter(|mark| mark.number == stamp);
        if let (Some((worked, process)), Some(mark)) = (cpu, mark)
            && stamp != 0
        {
            let worked = worked.saturating_sub(mark.worked);
            let others = process.saturating_sub(mark.process).saturating_sub(worked);
            let waited = now.saturating_duration_since(mark.at);
            if worked <= *target && others * 2 >= waited {
                // The loop was kept from this request by the rest of the
                // process, not by work of its own. That is not congestion,
                // and it ends a run of samples that looked like it.
                state.above_since = None;
                return;
            }
        }
        let since = *state.above_since.get_or_insert(now);
        // A single scheduling pause or a healthy burst is not persistent
        // congestion. Require delay above target over twenty target periods.
        if now.saturating_duration_since(since) > *target * 20 {
            state.until = Some(now + *target * 4);
            self.congested.store(true, Ordering::Release);
        }
    }

    pub(crate) fn overloaded(&self) -> bool {
        if !self.congested.load(Ordering::Acquire) {
            return false;
        }
        let mut state = self.state.lock().unwrap();
        if state.until.is_some_and(|until| Instant::now() < until) {
            return true;
        }
        state.until = None;
        state.above_since = None;
        self.tracking.store(false, Ordering::Relaxed);
        self.newest.store(0, Ordering::Relaxed);
        self.congested.store(false, Ordering::Release);
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MS: Duration = Duration::from_millis(1);

    fn none() -> Cpu {
        None
    }

    #[tokio::test(start_paused = true)]
    async fn queue_delay_sheds_then_recovers_without_releasing_permits() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        let queued = Instant::now();
        tokio::time::advance(Duration::from_millis(101)).await;
        delay.observe(delay.deadline(queued), Instant::now(), 0, none);
        assert!(!delay.overloaded());
        tokio::time::advance(Duration::from_millis(101)).await;
        delay.observe(delay.deadline(queued), Instant::now(), 0, none);
        assert!(delay.overloaded());
        tokio::time::advance(Duration::from_millis(20)).await;
        assert!(!delay.overloaded());
        let queued = Instant::now();
        tokio::time::advance(Duration::from_millis(1)).await;
        delay.observe(delay.deadline(queued), Instant::now(), 0, none);
        assert!(!delay.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn target_deadline_boundary_is_healthy_and_clears_tracking() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        let started = Instant::now();
        let deadline = delay.deadline(started);
        assert_eq!(deadline - started, Duration::from_millis(5));
        delay.observe(deadline, deadline + Duration::from_nanos(1), 0, none);
        assert!(delay.tracking.load(Ordering::Relaxed));
        delay.batch_done(deadline, || Some((MS, MS)));
        assert_ne!(delay.stamp(), 0);
        delay.observe(deadline, deadline, 0, none);
        assert!(!delay.tracking.load(Ordering::Relaxed));
        assert_eq!(delay.stamp(), 0);
        assert!(delay.state.lock().unwrap().above_since.is_none());
        delay.observe(deadline, started, 0, none);
        assert!(!delay.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn fixed_caps_never_enable_delay_shedding() {
        let delay = Delay::default();
        let started = Instant::now();
        tokio::time::advance(Duration::from_secs(60)).await;
        delay.observe(delay.deadline(started), Instant::now(), 0, none);
        assert!(!delay.overloaded());
    }

    /// One batch every `gap`: each request is queued just after a batch
    /// ends, and waits the whole gap. `work` is the loop thread's CPU time
    /// over a gap and `process` the whole process's.
    async fn run(delay: &Delay, batches: u32, gap: Duration, work: Duration, process: Duration) {
        let mut worked = Duration::ZERO;
        let mut total = Duration::ZERO;
        for _ in 0..batches {
            let stamp = delay.stamp();
            let queued = Instant::now();
            tokio::time::advance(gap).await;
            worked += work;
            total += process;
            let now = Instant::now();
            delay.observe(delay.deadline(queued), now, stamp, || Some((worked, total)));
            delay.batch_done(now, || Some((worked, total)));
        }
    }

    #[tokio::test(start_paused = true)]
    async fn a_loop_kept_waiting_by_the_rest_of_the_process_is_not_congested() {
        // Four threads of CPU-bound sync handlers under the GIL: the loop
        // gets it every 20 ms, does a fifth of a millisecond of dispatching,
        // and the process as a whole never stops running.
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run(&delay, 50, 20 * MS, MS / 5, 20 * MS).await;
        assert!(!delay.overloaded());
        // Only the first sample, which had no mark to be judged against,
        // counted as late, and the next one cleared it.
        assert!(delay.state.lock().unwrap().above_since.is_none());
        assert!(delay.tracking.load(Ordering::Relaxed));
    }

    #[tokio::test(start_paused = true)]
    async fn work_of_the_loops_own_ahead_of_a_request_is_congestion() {
        // The same waits, but the loop thread itself ran for 8 ms of each
        // 20: requests are queueing behind its work, whatever else runs.
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run(&delay, 10, 20 * MS, 8 * MS, 20 * MS).await;
        assert!(delay.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn a_blocked_loop_in_an_idle_process_is_congestion() {
        // The loop thread sat in a blocking call: it used no CPU, and
        // neither did anything else.
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run(&delay, 10, 20 * MS, MS / 10, MS).await;
        assert!(delay.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn a_process_running_for_less_than_half_the_wait_does_not_excuse_it() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run(&delay, 10, 20 * MS, MS / 5, 9 * MS).await;
        assert!(delay.overloaded());
        // Exactly half does.
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run(&delay, 10, 20 * MS, MS / 5, 10 * MS + MS / 5).await;
        assert!(!delay.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn late_requests_count_where_there_are_no_cpu_clocks() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        for _ in 0..10 {
            let stamp = delay.stamp();
            let queued = Instant::now();
            tokio::time::advance(20 * MS).await;
            let now = Instant::now();
            delay.observe(delay.deadline(queued), now, stamp, none);
            delay.batch_done(now, none);
            assert_eq!(delay.stamp(), 0);
        }
        assert!(delay.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn a_request_whose_mark_has_gone_counts_as_late() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run(&delay, 3, 20 * MS, MS / 5, 20 * MS).await;
        let old = delay.stamp();
        // More batches than marks are kept pass before it is started.
        run(&delay, MARKS as u32, 20 * MS, MS / 5, 20 * MS).await;
        assert!(delay.state.lock().unwrap().above_since.is_none());
        let now = Instant::now();
        delay.observe(now - 20 * MS, now, old, || {
            Some((Duration::from_secs(1), Duration::from_secs(9)))
        });
        assert_eq!(delay.state.lock().unwrap().above_since, Some(now));
        // So does one queued before any mark was kept.
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        let now = Instant::now();
        delay.observe(now - 20 * MS, now, 0, || Some((MS, 20 * MS)));
        assert_eq!(delay.state.lock().unwrap().above_since, Some(now));
    }

    #[tokio::test(start_paused = true)]
    async fn an_on_time_request_drops_the_marks_and_the_count_skips_zero() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run(&delay, 2, 20 * MS, MS / 5, 20 * MS).await;
        assert_ne!(delay.stamp(), 0);
        let now = Instant::now();
        delay.observe(now + MS, now, delay.stamp(), none);
        assert_eq!(delay.stamp(), 0);
        // Nothing is marked while nothing is late.
        delay.batch_done(now, || Some((MS, MS)));
        assert_eq!(delay.stamp(), 0);
        delay.tracking.store(true, Ordering::Relaxed);
        delay.newest.store(u32::MAX, Ordering::Relaxed);
        delay.batch_done(now, || Some((MS, MS)));
        assert_eq!(delay.stamp(), 1);
    }
}
