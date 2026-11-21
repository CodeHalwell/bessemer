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
//! do.
//!
//! Three readings, all taken on the loop's thread, tell that loop from one
//! that is overloaded: its own CPU time, the process's, and the wall-clock
//! time it has spent running request handlers. A late request is excused
//! when, since the reading it is judged against,
//!
//! - the loop thread used at most a target of CPU time, so it was not
//!   working;
//! - other threads used at least a target and at least twice the loop's, so
//!   they were; and
//! - other threads used at least a quarter as much CPU time as the loop
//!   spent inside handlers, so it was not blocked in one of them. A handler
//!   that sleeps or waits on a call takes wall-clock time there which
//!   nothing else in the process has to match; a handler held up by the GIL
//!   takes it while another thread is running.
//!
//! The first two count work done, not a share of wall time: a busy or paused
//! host stretches a GIL wait without making the loop congested. The third
//! does compare with wall time, but only time inside handlers, and a loop
//! handing sync work to its threads spends next to none there. For a
//! handler that lets go of the GIL and waits to have it back, a quarter
//! leaves room for a host that gives the process a quarter of a core.
//!
//! Several short scheduling slices can supply the evidence together; each
//! slice need not have a full target of CPU time to give the process. Work
//! added up that way must still be going on while the request waited: what
//! other threads did before it was queued says nothing about what kept it.
//!
//! An excused request ends the run of late ones that leads to shedding, so
//! the third test is also made over the whole of that run. A request can be
//! judged against a reading only a few milliseconds old, and a short burst
//! of work elsewhere fills such a window however long the loop has been
//! blocked.
//!
//! A loop marked congested stays so for four targets. They run from the end
//! of the batch in which it last started a request late, not from that
//! request: its handler may block for longer than they last, and a loop
//! still inside the batch that made it late has not recovered.
//!
//! For the same reason a request started on time does not end a run of late
//! ones when a late one was counted earlier in the same batch. A batch takes
//! the requests in the order they came, so the last of them came last and
//! may be on time only because it came just before the batch: that says
//! nothing about the loop. A request started on time in a batch of its own,
//! or first in one, does end the run.
//!
//! A batch-end mark can also contain loop CPU from before a request queued.
//! While tracking, the queue keeps a process-only CPU reading at that edge.
//! With a valid old mark, wall time bounds how much of its loop CPU could
//! precede the request. If post-queue loop work remains uncertain and a
//! conservative lower bound proves substantive other-thread work since the
//! queue edge, that sample is neutral: it cannot establish congestion or
//! release an existing congestion hold. This is an interval bound, not an
//! exact queue-time reading of the loop's CPU clock.
use std::sync::{
    Mutex, OnceLock,
    atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering},
};
use std::time::Duration;
use tokio::time::Instant;

pub(crate) const CEILING: usize = 4096;

/// Cooldown after the batch that most recently observed congestion.
pub(crate) const RECOVERY_INTERVALS: u32 = 4;

/// Longer than any request waits: the deadline where there is no target.
const NEVER: Duration = Duration::from_secs(365 * 24 * 60 * 60);

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
    /// The last congestion observation showed the loop making progress.
    productive: AtomicBool,
    tracking: AtomicBool,
    /// Number of the newest mark, or zero while none is being kept. A
    /// request carries the number current when it was queued.
    newest: AtomicU32,
    /// Wall-clock nanoseconds the loop's thread has spent running request
    /// handlers. Written on that thread only.
    inside: AtomicU64,
    /// Whether the loop is still inside the batch in which it last started
    /// a request late. A mark does not lapse meanwhile.
    held: AtomicBool,
    state: Mutex<State>,
}

#[derive(Default)]
struct State {
    above_since: Option<Instant>,
    /// The readings when the run of late requests counted from
    /// `above_since` began.
    run: Option<Mark>,
    until: Option<Instant>,
    /// Rolling CPU baseline for attributing several short scheduling slices.
    initial: Option<Mark>,
    /// Unlike `newest`, this does not reset on a healthy observation: a
    /// request retaining an old mark must not match a different batch's mark.
    number: u32,
    marks: [Option<Mark>; MARKS],
}

/// The readings at the end of one batch or at the first late observation.
#[derive(Clone, Copy)]
pub(crate) struct Mark {
    number: u32,
    at: Instant,
    worked: Duration,
    process: Duration,
    inside: Duration,
}

/// What happened between a mark and a later moment.
#[derive(Clone, Copy)]
struct Work {
    /// Time by the clock.
    wall: Duration,
    /// CPU time of the loop's thread.
    worked: Duration,
    /// CPU time of every other thread of the process.
    others: Duration,
    /// Time by the clock that the loop's thread spent running handlers.
    inside: Duration,
}

/// A valid wait can be accounted for, ambiguous at the queue boundary, or
/// counted toward persistent congestion. Missing evidence is still counted.
enum Attribution {
    External,
    Uncertain,
    Counted,
}

impl State {
    fn job_mark(
        &self,
        first_cold: &mut Option<Mark>,
        current: Option<Mark>,
        stamp: u32,
        target: Duration,
    ) -> Option<Mark> {
        if stamp != 0 {
            return self.marks[stamp as usize % MARKS].filter(|mark| mark.number == stamp);
        }
        let first = *first_cold;
        if first.is_none() {
            *first_cold = current;
        }
        // Keep the first cold sample's CPU limit for this whole batch,
        // even when an attribution advances the rolling initial window.
        first
            .filter(|mark| {
                current.is_some_and(|current| {
                    mark.work_since(
                        current.at,
                        Some((current.worked, current.process)),
                        current.inside,
                    )
                    .is_some_and(|work| work.worked <= target)
                })
            })
            .map(|first| {
                self.initial
                    .filter(|mark| mark.at >= first.at)
                    .unwrap_or(first)
            })
    }

    fn externally_delayed(
        &self,
        own: Option<Work>,
        stamp: u32,
        now: Instant,
        cpu: Cpu,
        inside: Duration,
        target: Duration,
    ) -> bool {
        own.filter(|own| own.worked <= target && !own.blocked())
            .is_some_and(|own| {
                own.external(target)
                    || (stamp != 0
                        && self
                            .initial
                            .filter(|mark| now.saturating_duration_since(mark.at) <= target * 20)
                            .and_then(|mark| mark.work_since(now, cpu, inside))
                            .is_some_and(|streak| {
                                streak.external(target) && own.keeps_pace_with(streak)
                            }))
            })
    }
}

impl Work {
    /// A busy loop must spend real CPU time and account for at least half of
    /// its handler time to justify waiting for it to clear congestion.
    fn productive(self, target: Duration) -> bool {
        self.worked >= target && self.worked.saturating_mul(2) >= self.inside
    }

    /// Whether other threads, and not the loop's own work or a call it was
    /// blocked in, account for the loop's time. See the module's notes.
    fn external(self, target: Duration) -> bool {
        self.worked <= target
            && self.others >= target
            && self.others >= self.worked * 2
            && !self.blocked()
    }

    /// Whether the loop spent more time inside handlers than other threads
    /// can account for by having run for a quarter of it.
    fn blocked(self) -> bool {
        self.others * 4 < self.inside
    }

    /// Whether other threads worked through this, a request's wait, at no
    /// less than half the rate they did over `streak`, the longer run of
    /// waits whose readings are being added up. Work that stopped before
    /// the request began to wait is no evidence about its wait.
    fn keeps_pace_with(self, streak: Work) -> bool {
        self.others.as_nanos() * streak.wall.as_nanos() * 2
            >= streak.others.as_nanos() * self.wall.as_nanos()
    }
}

impl Mark {
    fn work_since(self, now: Instant, cpu: Cpu, inside: Duration) -> Option<Work> {
        let wall = now.checked_duration_since(self.at)?;
        let (worked, process) = cpu?;
        let worked = worked.checked_sub(self.worked)?;
        let process = process.checked_sub(self.process)?;
        Some(Work {
            wall,
            worked,
            others: process.checked_sub(worked)?,
            inside: inside.saturating_sub(self.inside),
        })
    }

    fn queue_uncertain(
        self,
        own: Work,
        deadline: Instant,
        now: Instant,
        queue_process: Option<Duration>,
        cpu: Cpu,
        target: Duration,
    ) -> bool {
        if self.number == 0 || own.worked <= target || own.blocked() {
            return false;
        }
        let bounds = || {
            let queued = deadline.checked_sub(target)?;
            let prequeue = queued.checked_duration_since(self.at)?;
            now.checked_duration_since(queued)?;
            let queue_process = queue_process?;
            queue_process.checked_sub(self.process)?;
            let (_, process) = cpu?;
            // Process is read before ending loop CPU. Subtracting the full
            // loop interval gives a conservative post-queue other-CPU bound.
            let others = process
                .checked_sub(queue_process)?
                .checked_sub(own.worked)?;
            Some((prequeue, others))
        };
        bounds().is_some_and(|(prequeue, others)| {
            own.worked.saturating_sub(prequeue) <= target
                && others >= target
                && others >= own.worked * 2
                // The full handler interval is an upper bound: work before
                // queueing cannot hide a subsequently blocking handler.
                && others * 4 >= own.inside
        })
    }
}

impl Delay {
    pub(crate) fn enable(&self, timeout: Duration) {
        let target = (timeout / 10).clamp(Duration::from_micros(100), Duration::from_millis(5));
        let _ = self.target.set(target);
    }

    pub(crate) fn target(&self) -> Option<Duration> {
        self.target.get().copied()
    }

    pub(crate) fn productive(&self) -> bool {
        self.productive.load(Ordering::Acquire)
    }

    /// Precompute the sample's target deadline once when it is queued.
    /// Comparing instants avoids two timespec subtractions on healthy requests.
    /// Without a target, which is a fixed cap, no request is ever late: there
    /// is no delay signal, and a late request costs two CPU clock readings.
    #[inline]
    pub(crate) fn deadline(&self, started: Instant) -> Instant {
        started + self.target.get().copied().unwrap_or(NEVER)
    }

    /// What a request being queued keeps to be judged against later: which
    /// batch the loop had last finished. One atomic load, on any thread.
    #[inline]
    pub(crate) fn stamp(&self) -> u32 {
        // A submitter samples process CPU after acquiring this publication.
        // The selected mark's clock readings must precede that queue sample.
        self.newest.load(Ordering::Acquire)
    }

    /// The loop's thread ran a request handler for this long by the clock,
    /// whatever it was doing there. Called on that thread only.
    #[inline]
    pub(crate) fn ran_handler(&self, spent: Duration) {
        let spent = u64::try_from(spent.as_nanos()).unwrap_or(u64::MAX);
        // One writer, so a load and a store do.
        let total = self.inside.load(Ordering::Relaxed).saturating_add(spent);
        self.inside.store(total, Ordering::Relaxed);
    }

    /// Whether late requests are being judged, which is when a handler's
    /// time is worth reading a clock for outside a batch.
    #[inline]
    pub(crate) fn tracking(&self) -> bool {
        self.tracking.load(Ordering::Relaxed)
    }

    fn inside(&self) -> Duration {
        Duration::from_nanos(self.inside.load(Ordering::Relaxed))
    }

    /// Queue observation reuses invocation timestamps. Measure queueing
    /// immediately before a job starts, not its handler's completion time.
    /// `cpu` is only called for a late sample, on the loop's thread.
    /// `first_cold` is local to the batch being started.
    #[inline]
    pub(crate) fn observe(
        &self,
        first_cold: &mut Option<Mark>,
        deadline: Instant,
        now: Instant,
        stamp: u32,
        queue_process: Option<Duration>,
        cpu: impl FnOnce() -> Cpu,
    ) {
        if now <= deadline {
            // Not while the loop is inside a batch that has counted a late
            // request: see the module's notes.
            if self.tracking.load(Ordering::Relaxed) && !self.held.load(Ordering::Relaxed) {
                self.tracking.store(false, Ordering::Relaxed);
                self.held.store(false, Ordering::Relaxed);
                self.newest.store(0, Ordering::Relaxed);
                let mut state = self.state.lock().unwrap();
                state.above_since = None;
                state.run = None;
                state.initial = None;
            }
            return;
        }
        self.delayed(first_cold, deadline, now, stamp, queue_process, cpu());
    }

    /// The loop has finished a batch. While requests are running late its
    /// clocks are kept, so that the next late request can be judged by what
    /// happened while it waited, and a mark is given its four targets from
    /// here. Costs one atomic load otherwise.
    #[inline]
    pub(crate) fn batch_done(&self, now: Instant, cpu: impl FnOnce() -> Cpu) {
        if self.tracking.load(Ordering::Relaxed) {
            self.release(now);
            self.mark(now, cpu());
        }
    }

    /// The batch in which the loop last started a request late has ended.
    #[cold]
    fn release(&self, now: Instant) {
        if !self.held.load(Ordering::Relaxed) {
            return;
        }
        if let Some(target) = self.target.get()
            && self.congested.load(Ordering::Acquire)
        {
            let until = now + *target * RECOVERY_INTERVALS;
            let mut state = self.state.lock().unwrap();
            if state.until.is_none_or(|earlier| earlier < until) {
                state.until = Some(until);
            }
        }
        // After the time is set: a reader that finds the hold gone must
        // find the time in place.
        self.held.store(false, Ordering::Relaxed);
    }

    #[cold]
    fn mark(&self, now: Instant, cpu: Cpu) {
        let Some((worked, process)) = cpu else {
            return;
        };
        let mut state = self.state.lock().unwrap();
        // Zero means no mark, so the count skips it when it wraps.
        let number = match state.number.wrapping_add(1) {
            0 => 1,
            number => number,
        };
        state.number = number;
        state.marks[number as usize % MARKS] = Some(Mark {
            number,
            at: now,
            worked,
            process,
            inside: self.inside(),
        });
        self.newest.store(number, Ordering::Release);
    }

    #[cold]
    fn delayed(
        &self,
        first_cold: &mut Option<Mark>,
        deadline: Instant,
        now: Instant,
        stamp: u32,
        queue_process: Option<Duration>,
        cpu: Cpu,
    ) {
        let Some(target) = self.target.get().copied() else {
            return;
        };
        let mut state = self.state.lock().unwrap();
        self.tracking.store(true, Ordering::Relaxed);
        let inside = self.inside();
        let initial = cpu.map(|(worked, process)| Mark {
            number: 0,
            at: now,
            worked,
            process,
            inside,
        });
        if state.initial.is_none() {
            state.initial = initial;
        }
        let mark = state.job_mark(first_cold, initial, stamp, target);
        // Missing, regressing or inconsistent per-request readings are not
        // evidence that another thread held the loop up. Only a valid mark
        // can also consult the accumulated readings for this late streak,
        // and only for a wait the loop neither worked nor sat blocked
        // through, during which the work being added up was still going on.
        let own = mark.and_then(|mark| mark.work_since(now, cpu, inside));
        let run = state.run.and_then(|run| run.work_since(now, cpu, inside));
        let run_not_blocked = state.run.is_none() || run.is_some_and(|run| !run.blocked());
        let attributed = state.externally_delayed(own, stamp, now, cpu, inside, target)
            // A request judged against a recent reading sees a short
            // window, which one burst of work elsewhere can fill. It does
            // not end a run through which the loop sat blocked.
            && run_not_blocked;
        let attribution = if attributed {
            Attribution::External
        } else if run_not_blocked
            && mark.zip(own).is_some_and(|(mark, own)| {
                mark.queue_uncertain(own, deadline, now, queue_process, cpu, target)
            })
        {
            Attribution::Uncertain
        } else {
            Attribution::Counted
        };
        match attribution {
            Attribution::External => {
                state.above_since = None;
                state.run = None;
                state.initial = initial;
                self.held.store(false, Ordering::Relaxed);
                return;
            }
            Attribution::Uncertain => {
                if self.congested.load(Ordering::Acquire) {
                    // This new batch has not established recovery.
                    self.held.store(true, Ordering::Relaxed);
                } else {
                    state.above_since = None;
                    state.run = None;
                    state.initial = initial;
                }
                return;
            }
            Attribution::Counted => {}
        }
        // Counted. Until this batch ends the loop is busy with what made it
        // late, and a mark must not lapse under it.
        self.held.store(true, Ordering::Relaxed);
        if state
            .initial
            .is_some_and(|mark| now.saturating_duration_since(mark.at) >= target * 20)
        {
            // Incidental background CPU must not accumulate indefinitely
            // into an exemption. Use the same window as the persistence
            // decision, without resetting the run of delayed requests.
            state.initial = initial;
        }
        if state.above_since.is_none() {
            state.run = initial;
        }
        let since = *state.above_since.get_or_insert(now);
        // A single scheduling pause or a healthy burst is not persistent
        // congestion. Require delay above target over twenty target periods.
        if now.saturating_duration_since(since) > target * 20 {
            self.productive.store(
                run.is_some_and(|run| run.productive(target)),
                Ordering::Release,
            );
            state.until = Some(now + target * RECOVERY_INTERVALS);
            self.congested.store(true, Ordering::Release);
        }
    }

    pub(crate) fn overloaded(&self) -> bool {
        if !self.congested.load(Ordering::Acquire) {
            return false;
        }
        if self.held.load(Ordering::Relaxed) {
            return true;
        }
        let mut state = self.state.lock().unwrap();
        // A late observation can latch a new batch after the fast check
        // above. Its hold is published while owning this same mutex.
        if self.held.load(Ordering::Relaxed) {
            return true;
        }
        if state.until.is_some_and(|until| Instant::now() < until) {
            return true;
        }
        state.until = None;
        state.above_since = None;
        state.run = None;
        state.initial = None;
        self.tracking.store(false, Ordering::Relaxed);
        self.newest.store(0, Ordering::Relaxed);
        self.productive.store(false, Ordering::Release);
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
        delay.observe(
            &mut None,
            delay.deadline(queued),
            Instant::now(),
            0,
            None,
            none,
        );
        assert!(!delay.overloaded());
        tokio::time::advance(Duration::from_millis(101)).await;
        delay.observe(
            &mut None,
            delay.deadline(queued),
            Instant::now(),
            0,
            None,
            none,
        );
        assert!(delay.overloaded());
        // The four targets of a mark run from the end of the batch.
        delay.batch_done(Instant::now(), none);
        tokio::time::advance(Duration::from_millis(20)).await;
        assert!(!delay.overloaded());
        let queued = Instant::now();
        tokio::time::advance(Duration::from_millis(1)).await;
        delay.observe(
            &mut None,
            delay.deadline(queued),
            Instant::now(),
            0,
            None,
            none,
        );
        assert!(!delay.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn target_deadline_boundary_is_healthy_and_clears_tracking() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        let started = Instant::now();
        let deadline = delay.deadline(started);
        assert_eq!(deadline - started, Duration::from_millis(5));
        delay.observe(
            &mut None,
            deadline,
            deadline + Duration::from_nanos(1),
            0,
            None,
            none,
        );
        assert!(delay.tracking.load(Ordering::Relaxed));
        delay.batch_done(deadline, || Some((MS, MS)));
        assert_ne!(delay.stamp(), 0);
        delay.observe(&mut None, deadline, deadline, 0, None, none);
        assert!(!delay.tracking.load(Ordering::Relaxed));
        assert_eq!(delay.stamp(), 0);
        assert!(delay.state.lock().unwrap().above_since.is_none());
        delay.observe(&mut None, deadline, started, 0, None, none);
        assert!(!delay.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn fixed_caps_never_enable_delay_shedding() {
        let delay = Delay::default();
        let started = Instant::now();
        tokio::time::advance(Duration::from_secs(60)).await;
        delay.observe(
            &mut None,
            delay.deadline(started),
            Instant::now(),
            0,
            None,
            none,
        );
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
            delay.observe(&mut None, delay.deadline(queued), now, stamp, None, || {
                Some((worked, total))
            });
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
    async fn cpu_work_in_other_threads_is_not_loop_congestion_on_a_busy_host() {
        // The same GIL hand-off can span much more wall time on a shared or
        // throttled host. Requiring the process to have half a core wrongly
        // turns that scheduling delay into loop overload.
        for gap in [20 * MS, 200 * MS] {
            let delay = Delay::default();
            delay.enable(Duration::from_secs(30));
            run(&delay, 10, gap, MS / 5, 9 * MS).await;
            assert!(!delay.overloaded(), "gap={gap:?}");
        }
    }

    #[tokio::test(start_paused = true)]
    async fn short_cpu_slices_are_attributed_across_multiple_valid_marks() {
        // The process runs for two milliseconds out of every seven. Each
        // individual mark sees less than the five-millisecond target of
        // other-thread CPU, but several together prove the loop was waiting
        // for those threads rather than running its own work.
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run(&delay, 100, 7 * MS, MS / 10, 2 * MS).await;
        assert!(!delay.overloaded());
        assert_ne!(delay.stamp(), 0);
    }

    #[tokio::test(start_paused = true)]
    async fn a_cold_batch_can_attribute_continued_waits_without_a_queue_mark() {
        // All requests were queued before the first late observation, so
        // all carry zero until the batch ends. Dispatching sync work can
        // take more than 100 ms when its threads keep acquiring the GIL.
        for (gap, work, process) in [
            (20 * MS, MS / 5, 9 * MS),
            (200 * MS, MS / 5, 9 * MS),
            (7 * MS, MS / 10, 2 * MS),
        ] {
            let delay = Delay::default();
            delay.enable(Duration::from_secs(30));
            let mut first_cold = None;
            let queued = Instant::now();
            for job in 1..=24 {
                tokio::time::advance(gap).await;
                delay.observe(
                    &mut first_cold,
                    delay.deadline(queued),
                    Instant::now(),
                    0,
                    None,
                    || Some((work * job, process * job)),
                );
            }
            assert!(!delay.overloaded(), "gap={gap:?}");
            assert_eq!(delay.stamp(), 0, "no batch has finished yet");
        }
    }

    #[tokio::test(start_paused = true)]
    async fn a_cold_batch_keeps_counting_its_own_cpu_after_attribution() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        let mut first_cold = None;
        let queued = Instant::now();
        for job in 1..=64 {
            tokio::time::advance(16 * MS).await;
            delay.observe(
                &mut first_cold,
                delay.deadline(queued),
                Instant::now(),
                0,
                None,
                || Some((4 * MS * job, 12 * MS * job)),
            );
            if job == 2 {
                assert!(delay.state.lock().unwrap().above_since.is_none());
            }
        }
        // The first interval can be attributed to other threads. Later
        // requests each wait behind more than 5 ms of actual loop work.
        assert!(delay.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn a_new_cold_batch_waits_for_a_cpu_interval_of_its_own() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        let queued = Instant::now();
        tokio::time::advance(20 * MS).await;
        let first = Instant::now();
        delay.observe(&mut None, delay.deadline(queued), first, 0, None, || {
            Some((MS, MS))
        });
        delay.batch_done(first, || Some((MS, MS)));

        // These jobs were queued before the preceding batch recorded a
        // mark. Its aggregate reading cannot excuse the first cold job.
        let mut first_cold = None;
        tokio::time::advance(20 * MS).await;
        delay.observe(
            &mut first_cold,
            delay.deadline(queued),
            Instant::now(),
            0,
            None,
            || Some((MS, 20 * MS)),
        );
        assert_eq!(delay.state.lock().unwrap().above_since, Some(first));

        tokio::time::advance(20 * MS).await;
        delay.observe(
            &mut first_cold,
            delay.deadline(queued),
            Instant::now(),
            0,
            None,
            || Some((MS, 40 * MS)),
        );
        assert!(delay.state.lock().unwrap().above_since.is_none());
    }

    #[tokio::test(start_paused = true)]
    async fn a_cold_batch_still_sheds_for_its_own_work_or_an_idle_blocked_loop() {
        for (work, process) in [(8 * MS, 20 * MS), (MS / 10, MS / 10)] {
            let delay = Delay::default();
            delay.enable(Duration::from_secs(30));
            let mut first_cold = None;
            let queued = Instant::now();
            for job in 1..=10 {
                tokio::time::advance(20 * MS).await;
                delay.observe(
                    &mut first_cold,
                    delay.deadline(queued),
                    Instant::now(),
                    0,
                    None,
                    || Some((work * job, process * job)),
                );
            }
            assert!(delay.overloaded(), "work={work:?}, process={process:?}");
        }
    }

    #[tokio::test(start_paused = true)]
    async fn excusing_a_wait_requires_substantive_work_elsewhere() {
        // A little incidental work elsewhere must not hide an idle blocked
        // loop. Its total across the persistence window remains below a
        // target, not merely the CPU time between two adjacent marks.
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run(&delay, 10, 20 * MS, MS / 5, 3 * MS / 5).await;
        assert!(delay.overloaded());

        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run(&delay, 10, 20 * MS, MS / 5, 5 * MS + MS / 5).await;
        assert!(!delay.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn incidental_cpu_does_not_accumulate_beyond_the_persistence_window() {
        // Without a bounded window this idle blocked loop would eventually
        // be excused after accumulating five milliseconds of background CPU.
        for batch_marks in [false, true] {
            let delay = Delay::default();
            delay.enable(Duration::from_secs(30));
            let first = Instant::now() + 20 * MS;
            let mut first_cold = None;
            for batch in 1..=100 {
                let queued = Instant::now();
                let stamp = delay.stamp();
                tokio::time::advance(20 * MS).await;
                let now = Instant::now();
                let cpu = Some((MS / 10 * batch, MS / 2 * batch));
                delay.observe(
                    &mut first_cold,
                    delay.deadline(queued),
                    now,
                    stamp,
                    None,
                    || cpu,
                );
                if batch_marks {
                    delay.batch_done(now, || cpu);
                    first_cold = None;
                }
                assert_eq!(
                    delay.state.lock().unwrap().above_since,
                    Some(first),
                    "batch={batch}, batch_marks={batch_marks}"
                );
            }
            assert!(delay.overloaded(), "batch_marks={batch_marks}");
        }
    }

    #[tokio::test(start_paused = true)]
    async fn aggregate_cpu_evidence_expires_before_a_late_sample_can_use_it() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        let first = Instant::now();
        delay.observe(&mut None, first - MS, first, 0, None, || {
            Some((Duration::ZERO, Duration::ZERO))
        });
        delay.batch_done(first, || Some((Duration::ZERO, Duration::ZERO)));
        let stamp = delay.stamp();
        tokio::time::advance(80 * MS).await;
        let now = Instant::now();
        let cpu = Some((MS / 10, 49 * MS / 10));
        // The first 80 ms have 4.8 ms of other-thread CPU: not enough yet.
        delay.observe(&mut None, delay.deadline(first), now, stamp, None, || cpu);
        delay.batch_done(now, || cpu);
        let stamp = delay.stamp();
        tokio::time::advance(40 * MS).await;
        // This request's valid mark sees only another 0.4 ms. Adding it to
        // the initial reading would cross the CPU floor, but that reading
        // is now outside the 100 ms persistence window.
        delay.observe(
            &mut None,
            delay.deadline(now),
            Instant::now(),
            stamp,
            None,
            || Some((MS / 5, 54 * MS / 10)),
        );
        assert_eq!(delay.state.lock().unwrap().above_since, Some(first));
        assert!(delay.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn invalid_or_missing_marks_do_not_use_the_aggregate_cpu_window() {
        for bad_mark in [0, 1, 2] {
            let delay = Delay::default();
            delay.enable(Duration::from_secs(30));
            let queued = Instant::now();
            tokio::time::advance(20 * MS).await;
            let first = Instant::now();
            delay.observe(&mut None, delay.deadline(queued), first, 0, None, || {
                Some((Duration::ZERO, Duration::ZERO))
            });
            let stamp = match bad_mark {
                0 => u32::MAX, // No such mark remains in the ring.
                1 => {
                    // The per-request loop clock will have regressed.
                    delay.mark(first, Some((5 * MS, 100 * MS)));
                    delay.stamp()
                }
                _ => {
                    delay.mark(first + Duration::from_secs(1), Some((MS, 100 * MS)));
                    delay.stamp()
                }
            };
            tokio::time::advance(20 * MS).await;
            // These readings would qualify against the initial snapshot,
            // but not against the missing, regressing or future job mark.
            delay.observe(
                &mut None,
                delay.deadline(queued),
                Instant::now(),
                stamp,
                None,
                || Some((4 * MS, 120 * MS)),
            );
            assert_eq!(delay.state.lock().unwrap().above_since, Some(first));
        }
    }

    #[tokio::test(start_paused = true)]
    async fn a_busy_job_mark_is_not_excused_by_a_later_idle_window() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        let queued = Instant::now();
        delay.mark(queued, Some((Duration::ZERO, Duration::ZERO)));
        let stamp = delay.stamp();
        tokio::time::advance(20 * MS).await;
        let first = Instant::now();
        delay.observe(&mut None, delay.deadline(queued), first, 0, None, || {
            Some((8 * MS, 100 * MS))
        });
        tokio::time::advance(20 * MS).await;
        // The current streak alone would qualify: two milliseconds of loop
        // CPU and much more elsewhere. But this job waited behind ten
        // milliseconds of actual loop work, so it must still count as late.
        delay.observe(
            &mut None,
            delay.deadline(queued),
            Instant::now(),
            stamp,
            None,
            || Some((10 * MS, 200 * MS)),
        );
        assert_eq!(delay.state.lock().unwrap().above_since, Some(first));
    }

    #[tokio::test(start_paused = true)]
    async fn other_thread_work_must_dominate_the_loops_work() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run(&delay, 10, 20 * MS, 3 * MS, 8 * MS).await;
        assert!(delay.overloaded());

        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run(&delay, 10, 20 * MS, 5 * MS / 2, 15 * MS / 2).await;
        assert!(!delay.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn regressing_or_inconsistent_cpu_clocks_do_not_excuse_a_wait() {
        for sample in [
            // The loop clock regressed while the process clock advanced.
            (4 * MS, 120 * MS),
            // The process clock regressed while the loop clock advanced.
            (6 * MS, 90 * MS),
            // The process used less CPU than the loop it contains.
            (9 * MS, 102 * MS),
        ] {
            let delay = Delay::default();
            delay.enable(Duration::from_secs(30));
            for _ in 0..10 {
                let queued = Instant::now();
                delay.mark(queued, Some((5 * MS, 100 * MS)));
                let stamp = delay.stamp();
                tokio::time::advance(20 * MS).await;
                delay.observe(
                    &mut None,
                    delay.deadline(queued),
                    Instant::now(),
                    stamp,
                    None,
                    || Some(sample),
                );
            }
            assert!(delay.overloaded(), "sample={sample:?}");
        }
    }

    #[tokio::test(start_paused = true)]
    async fn a_mark_from_the_future_does_not_excuse_a_wait() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        for _ in 0..10 {
            let queued = Instant::now();
            delay.mark(queued + Duration::from_secs(1), Some((MS, 100 * MS)));
            let stamp = delay.stamp();
            tokio::time::advance(20 * MS).await;
            delay.observe(
                &mut None,
                delay.deadline(queued),
                Instant::now(),
                stamp,
                None,
                || Some((2 * MS, 120 * MS)),
            );
        }
        assert!(delay.overloaded());
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
            delay.observe(&mut None, delay.deadline(queued), now, stamp, None, none);
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
        delay.observe(&mut None, now - 20 * MS, now, old, None, || {
            Some((Duration::from_secs(1), Duration::from_secs(9)))
        });
        assert_eq!(delay.state.lock().unwrap().above_since, Some(now));
        // So does one queued before any mark was kept.
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        let now = Instant::now();
        delay.observe(&mut None, now - 20 * MS, now, 0, None, || {
            Some((MS, 20 * MS))
        });
        assert_eq!(delay.state.lock().unwrap().above_since, Some(now));
    }

    #[tokio::test(start_paused = true)]
    async fn an_on_time_request_drops_the_marks_and_the_count_skips_zero() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run(&delay, 2, 20 * MS, MS / 5, 20 * MS).await;
        assert_ne!(delay.stamp(), 0);
        let now = Instant::now();
        delay.observe(&mut None, now + MS, now, delay.stamp(), None, none);
        assert_eq!(delay.stamp(), 0);
        // Nothing is marked while nothing is late.
        delay.batch_done(now, || Some((MS, MS)));
        assert_eq!(delay.stamp(), 0);
        delay.tracking.store(true, Ordering::Relaxed);
        delay.state.lock().unwrap().number = u32::MAX;
        delay.batch_done(now, || Some((MS, MS)));
        assert_eq!(delay.stamp(), 1);
    }

    /// As `run`, with the loop's thread spending `inside` of every gap
    /// running handlers, by the clock.
    async fn run_inside(
        delay: &Delay,
        batches: u32,
        gap: Duration,
        work: Duration,
        process: Duration,
        inside: Duration,
    ) {
        let mut worked = Duration::ZERO;
        let mut total = Duration::ZERO;
        for _ in 0..batches {
            let stamp = delay.stamp();
            let queued = Instant::now();
            tokio::time::advance(gap).await;
            worked += work;
            total += process;
            delay.ran_handler(inside);
            let now = Instant::now();
            delay.observe(&mut None, delay.deadline(queued), now, stamp, None, || {
                Some((worked, total))
            });
            delay.batch_done(now, || Some((worked, total)));
        }
    }

    #[tokio::test(start_paused = true)]
    async fn a_loop_blocked_in_its_handlers_is_not_excused_by_work_elsewhere() {
        // The loop sleeps in its handlers for the whole of every 40 ms gap
        // while another thread uses 8 ms of CPU, a fifth of a core. Those
        // readings alone would excuse the wait, and do when the loop spent
        // the time outside its handlers, as it does waiting for the GIL
        // before it can hand sync work over.
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run_inside(&delay, 10, 40 * MS, MS / 5, 8 * MS + MS / 5, 40 * MS).await;
        assert!(delay.overloaded());

        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run_inside(&delay, 10, 40 * MS, MS / 5, 8 * MS + MS / 5, Duration::ZERO).await;
        assert!(!delay.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn handler_time_is_excused_when_other_threads_ran_for_a_quarter_of_it() {
        // A handler that lets go of the GIL waits for it inside the handler
        // while the threads that took it run, for as long as the host lets
        // them. A quarter is the line: ten milliseconds of their CPU for
        // forty in the handler.
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run_inside(&delay, 10, 40 * MS, MS / 5, 10 * MS + MS / 5, 40 * MS).await;
        assert!(!delay.overloaded());

        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run_inside(&delay, 10, 40 * MS, MS / 5, 10 * MS, 40 * MS).await;
        assert!(delay.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn a_long_block_is_not_excused_by_a_target_of_work_elsewhere() {
        for (inside, counted) in [(Duration::from_secs(2), true), (Duration::ZERO, false)] {
            let delay = Delay::default();
            delay.enable(Duration::from_secs(30));
            let queued = Instant::now();
            delay.mark(queued, Some((Duration::ZERO, Duration::ZERO)));
            let stamp = delay.stamp();
            tokio::time::advance(Duration::from_secs(2)).await;
            delay.ran_handler(inside);
            // Six milliseconds of work by other threads in two seconds.
            delay.observe(
                &mut None,
                delay.deadline(queued),
                Instant::now(),
                stamp,
                None,
                || Some((MS, 7 * MS)),
            );
            assert_eq!(
                delay.state.lock().unwrap().above_since.is_some(),
                counted,
                "inside={inside:?}"
            );
        }
    }

    #[tokio::test(start_paused = true)]
    async fn a_cold_batch_blocked_in_its_handlers_is_not_excused() {
        // Queued before anything was late, so judged against the batch's
        // first late reading: four milliseconds asleep in each handler
        // while another thread uses a fifth of a core.
        for (inside, overloaded) in [(4 * MS, true), (Duration::ZERO, false)] {
            let delay = Delay::default();
            delay.enable(Duration::from_secs(30));
            let mut first_cold = None;
            let queued = Instant::now();
            for job in 1..=64u32 {
                tokio::time::advance(4 * MS).await;
                delay.ran_handler(inside);
                delay.observe(
                    &mut first_cold,
                    delay.deadline(queued),
                    Instant::now(),
                    0,
                    None,
                    || Some((MS / 20 * job, (MS / 20 + 4 * MS / 5) * job)),
                );
            }
            assert_eq!(delay.overloaded(), overloaded, "inside={inside:?}");
        }
    }

    /// Batches of three requests queued before anything was late, the loop
    /// asleep 4 ms in each handler, and another thread that works `burst`
    /// in one piece every 40 ms. Each batch judges its requests against its
    /// own first late reading, a few milliseconds old.
    async fn blocked_beside_bursts(burst: Duration) -> Delay {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        let began = Instant::now();
        for _ in 0..20 {
            let mut first_cold = None;
            for _ in 0..3 {
                tokio::time::advance(4 * MS).await;
                delay.ran_handler(4 * MS);
                let now = Instant::now();
                let elapsed = now.saturating_duration_since(began);
                let worked = elapsed / 80;
                let bursts = u32::try_from((elapsed + 10 * MS).as_millis() / 40).unwrap();
                // All of them queued at the start, so each waited through
                // every handler before it.
                delay.observe(&mut first_cold, delay.deadline(began), now, 0, None, || {
                    Some((worked, worked + burst * bursts))
                });
            }
        }
        delay
    }

    #[tokio::test(start_paused = true)]
    async fn a_burst_in_a_short_window_does_not_end_a_run_of_blocked_waits() {
        // Six milliseconds every forty is 15% of a core, and fills the
        // window of a request judged against a reading 4 ms old. Judged so
        // alone, every burst ended the run and nothing was ever refused: a
        // client in the server's own process did this to a test on macOS.
        assert!(blocked_beside_bursts(6 * MS).await.overloaded());
        // Twelve every forty is 30%, over the line for the run as well.
        assert!(!blocked_beside_bursts(12 * MS).await.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn work_added_up_must_still_be_going_on_while_the_request_waited() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        let first = Instant::now();
        delay.observe(&mut None, first - MS, first, 0, None, || {
            Some((Duration::ZERO, Duration::ZERO))
        });
        delay.batch_done(first, || Some((Duration::ZERO, Duration::ZERO)));
        tokio::time::advance(10 * MS).await;
        let second = Instant::now();
        // Other threads did six milliseconds of work in the first ten.
        delay.batch_done(second, || Some((MS / 10, 61 * MS / 10)));
        let stamp = delay.stamp();
        tokio::time::advance(85 * MS).await;
        // Then nothing ran for 85 ms. The request queued at their start
        // sees 0.1 ms of work elsewhere in its own wait. The six from
        // before it was queued are inside the window being added up, and
        // are not evidence about what kept it.
        delay.observe(
            &mut None,
            delay.deadline(second),
            Instant::now(),
            stamp,
            None,
            || Some((MS / 5, 63 * MS / 10)),
        );
        assert_eq!(delay.state.lock().unwrap().above_since, Some(first));
    }

    /// Six requests queued together and started 25 ms apart, which is how
    /// long a handler that sleeps 4 ms takes on a macOS CI runner: 125 ms of
    /// late requests, and the loop marked.
    async fn marked_by_slow_handlers() -> Delay {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        let queued = Instant::now();
        let mut first_cold = None;
        for _ in 0..6 {
            tokio::time::advance(25 * MS).await;
            let now = Instant::now();
            delay.observe(&mut first_cold, delay.deadline(queued), now, 0, None, none);
        }
        assert!(delay.overloaded());
        delay
    }

    #[tokio::test(start_paused = true)]
    async fn a_mark_holds_while_the_loop_is_inside_the_batch_that_was_late() {
        // The sixth handler is still running 24 ms after it was started,
        // which is past the four targets a mark lasts. Counted from the
        // request, the mark lapsed under every handler longer than 20 ms,
        // and bursts to such handlers were answered in full on macOS.
        let delay = marked_by_slow_handlers().await;
        tokio::time::advance(24 * MS).await;
        assert!(delay.overloaded());
        tokio::time::advance(Duration::from_secs(5)).await;
        assert!(delay.overloaded());
        // The batch ends, and the four targets run from here.
        delay.batch_done(Instant::now(), none);
        tokio::time::advance(19 * MS).await;
        assert!(delay.overloaded());
        tokio::time::advance(2 * MS).await;
        assert!(!delay.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn an_on_time_request_after_a_late_one_in_its_batch_ends_nothing() {
        let delay = marked_by_slow_handlers().await;
        let now = Instant::now();
        // It came just before the batch. The loop is still in the batch that
        // made it late, and the mark holds.
        delay.observe(&mut None, now + MS, now, 0, None, none);
        tokio::time::advance(24 * MS).await;
        assert!(delay.overloaded());
        delay.batch_done(Instant::now(), none);
        tokio::time::advance(21 * MS).await;
        assert!(!delay.overloaded());
        // One first in a batch of its own does end the run.
        let queued = Instant::now();
        tokio::time::advance(50 * MS).await;
        let now = Instant::now();
        delay.observe(&mut None, delay.deadline(queued), now, 0, None, none);
        delay.batch_done(now, none);
        delay.observe(&mut None, now + MS, now, 0, None, none);
        assert!(delay.state.lock().unwrap().above_since.is_none());
    }

    #[tokio::test(start_paused = true)]
    async fn a_run_survives_batches_that_end_with_requests_on_time() {
        // Handlers that block after they have suspended once, as a loop of
        // 32 closed-loop clients fell into in CI: a batch of two late
        // requests, their handlers 8 ms, then a batch of twelve late ones
        // and eighteen that came in its last 5 ms, their handlers 120 ms.
        // While each on-time request ended the run, it never lasted 100 ms
        // and the loop, blocked all but a few milliseconds of every 128,
        // was never marked.
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        let mut marked = false;
        for _ in 0..8 {
            let now = Instant::now();
            let mut first_cold = None;
            for _ in 0..2 {
                delay.observe(
                    &mut first_cold,
                    delay.deadline(now - 60 * MS),
                    now,
                    0,
                    None,
                    none,
                );
            }
            marked |= delay.overloaded();
            delay.batch_done(now, none);
            tokio::time::advance(8 * MS).await;
            let now = Instant::now();
            let mut first_cold = None;
            for _ in 0..12 {
                delay.observe(
                    &mut first_cold,
                    delay.deadline(now - 60 * MS),
                    now,
                    0,
                    None,
                    none,
                );
            }
            for _ in 0..18 {
                delay.observe(&mut first_cold, delay.deadline(now), now, 0, None, none);
            }
            marked |= delay.overloaded();
            delay.batch_done(now, none);
            tokio::time::advance(120 * MS).await;
        }
        assert!(marked);
    }

    #[tokio::test(start_paused = true)]
    async fn a_fixed_cap_never_reads_the_cpu_clocks() {
        // No target was set, as with an explicit `max_in_flight`. From
        // 1.0.0rc6 such a request was late the moment it was queued, and
        // every one of them read both clocks before being ignored.
        let delay = Delay::default();
        let queued = Instant::now();
        tokio::time::advance(Duration::from_secs(60)).await;
        delay.observe(
            &mut None,
            delay.deadline(queued),
            Instant::now(),
            delay.stamp(),
            None,
            || panic!("a fixed cap has no use for the CPU clocks"),
        );
        assert!(!delay.tracking());
        assert!(!delay.overloaded());
    }

    #[test]
    fn handler_time_adds_up_and_does_not_wrap() {
        let delay = Delay::default();
        delay.ran_handler(3 * MS);
        delay.ran_handler(4 * MS);
        assert_eq!(delay.inside(), 7 * MS);
        delay.ran_handler(Duration::MAX);
        delay.ran_handler(MS);
        assert_eq!(delay.inside(), Duration::from_nanos(u64::MAX));
    }

    #[tokio::test(start_paused = true)]
    async fn healthy_resets_do_not_reuse_an_old_requests_mark_number() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run(&delay, 1, 20 * MS, MS / 5, 20 * MS).await;
        let old = delay.stamp();
        let now = Instant::now();
        delay.observe(&mut None, now + MS, now, old, None, none);
        assert_eq!(delay.stamp(), 0);
        delay.observe(&mut None, now - MS, now, 0, None, || Some((MS, 40 * MS)));
        delay.batch_done(now, || Some((MS, 40 * MS)));
        assert_ne!(delay.stamp(), old);
    }

    #[tokio::test(start_paused = true)]
    async fn cpu_before_a_request_queues_does_not_establish_loop_congestion() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        let mut worked = Duration::ZERO;
        let mut process = Duration::ZERO;
        let first = Instant::now();
        delay.observe(&mut None, first - MS, first, 0, None, || {
            Some((worked, process))
        });
        delay.batch_done(first, || Some((worked, process)));
        let stamp = delay.stamp();
        tokio::time::advance(10 * MS).await;
        process += 10 * MS;
        let now = Instant::now();
        delay.observe(&mut None, delay.deadline(first), now, stamp, None, || {
            Some((worked, process))
        });
        delay.batch_done(now, || Some((worked, process)));
        assert!(delay.state.lock().unwrap().above_since.is_none());

        // This is the separately proven prequeue regression: the old mark
        // charges six milliseconds before each queue edge to that request.
        for request in 1..=2 {
            tokio::time::advance(6 * MS).await;
            worked += 6 * MS;
            process += 6 * MS;
            let queued = Instant::now();
            let queue_process = process;
            let stamp = delay.stamp();
            tokio::time::advance(100 * MS).await;
            process += 100 * MS;
            let now = Instant::now();
            delay.observe(
                &mut None,
                delay.deadline(queued),
                now,
                stamp,
                Some(queue_process),
                || Some((worked, process)),
            );
            assert!(
                delay.state.lock().unwrap().above_since.is_none(),
                "request={request}"
            );
            delay.batch_done(now, || Some((worked, process)));
            assert!(!delay.overloaded(), "request={request}");
        }
    }

    /// One old-mark interval with explicit CPU on either side of the queue.
    /// Each tuple is (wall time, loop CPU, other-thread CPU).
    async fn boundary_sample(
        before: (Duration, Duration, Duration),
        after: (Duration, Duration, Duration),
        inside: Duration,
    ) -> Delay {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        delay.mark(Instant::now(), Some((Duration::ZERO, Duration::ZERO)));
        let stamp = delay.stamp();
        tokio::time::advance(before.0).await;
        let queued = Instant::now();
        let queue_process = before.1 + before.2;
        tokio::time::advance(after.0).await;
        delay.ran_handler(inside);
        delay.observe(
            &mut None,
            delay.deadline(queued),
            Instant::now(),
            stamp,
            Some(queue_process),
            || Some((before.1 + after.1, queue_process + after.1 + after.2)),
        );
        delay
    }

    #[tokio::test(start_paused = true)]
    async fn known_postqueue_loop_cpu_still_counts() {
        let delay = boundary_sample(
            (MS, MS, Duration::ZERO),
            (100 * MS, 6 * MS, 100 * MS),
            Duration::ZERO,
        )
        .await;
        assert!(delay.state.lock().unwrap().above_since.is_some());
    }

    #[tokio::test(start_paused = true)]
    async fn ambiguous_postqueue_loop_cpu_is_neutral_not_proven_external() {
        // The six milliseconds really follow queueing in this construction,
        // but the retained readings cannot distinguish them from prequeue
        // CPU. The supported policy is neutrality, not exact attribution.
        let delay = boundary_sample(
            (6 * MS, Duration::ZERO, Duration::ZERO),
            (100 * MS, 6 * MS, 100 * MS),
            Duration::ZERO,
        )
        .await;
        let state = delay.state.lock().unwrap();
        assert!(state.above_since.is_none());
        assert!(state.run.is_none());
        assert_eq!(state.initial.unwrap().worked, 6 * MS);
    }

    #[tokio::test(start_paused = true)]
    async fn the_postqueue_loop_cpu_lower_bound_keeps_the_same_target() {
        for (prequeue, counted) in [(MS, false), (MS - Duration::from_micros(1), true)] {
            let delay = boundary_sample(
                (prequeue, Duration::ZERO, Duration::ZERO),
                (100 * MS, 6 * MS, 100 * MS),
                Duration::ZERO,
            )
            .await;
            assert_eq!(delay.state.lock().unwrap().above_since.is_some(), counted);
        }
    }

    #[tokio::test(start_paused = true)]
    async fn queue_uncertainty_needs_substantive_dominating_cpu_after_queueing() {
        for other in [Duration::ZERO, 5 * MS, 6 * MS, 10 * MS, 17 * MS, 18 * MS] {
            let delay = boundary_sample(
                (6 * MS, 6 * MS, 1_000 * MS),
                (100 * MS, Duration::ZERO, other),
                Duration::ZERO,
            )
            .await;
            // The queue lower bound subtracts all six milliseconds of loop
            // CPU, even though they preceded this request. It must still
            // prove at least twelve milliseconds elsewhere after queueing.
            assert_eq!(
                delay.state.lock().unwrap().above_since.is_none(),
                other == 18 * MS,
                "other={other:?}"
            );
        }
    }

    #[tokio::test(start_paused = true)]
    async fn prequeue_cpu_elsewhere_cannot_hide_a_blocking_handler() {
        for (inside, counted) in [(48 * MS, false), (48 * MS + Duration::from_nanos(1), true)] {
            let delay = boundary_sample(
                (6 * MS, 6 * MS, 1_000 * MS),
                (100 * MS, Duration::ZERO, 18 * MS),
                inside,
            )
            .await;
            // The old mark includes a second of unrelated CPU before this
            // request. Only the conservative queue lower bound may account
            // for the full handler-time upper bound.
            assert_eq!(delay.state.lock().unwrap().above_since.is_some(), counted);
        }
    }

    #[tokio::test(start_paused = true)]
    async fn queue_uncertainty_does_not_end_a_blocked_run() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        delay.ran_handler(500 * MS);
        let began = Instant::now();
        delay.mark(began, Some((Duration::ZERO, Duration::ZERO)));
        let stamp = delay.stamp();
        {
            let mut state = delay.state.lock().unwrap();
            let mark = state.marks[stamp as usize % MARKS].unwrap();
            state.run = Some(Mark {
                inside: Duration::ZERO,
                ..mark
            });
            state.above_since = Some(began);
        }
        tokio::time::advance(6 * MS).await;
        let queued = Instant::now();
        tokio::time::advance(100 * MS).await;
        delay.observe(
            &mut None,
            delay.deadline(queued),
            Instant::now(),
            stamp,
            Some(6 * MS),
            || Some((6 * MS, 106 * MS)),
        );
        assert_eq!(delay.state.lock().unwrap().above_since, Some(began));
        assert!(delay.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn invalid_queue_process_readings_keep_counting_the_wait() {
        for queue_process in [
            None,
            Some(99 * MS),
            Some(201 * MS),
            Some(206 * MS),
            Some(207 * MS),
        ] {
            let delay = Delay::default();
            delay.enable(Duration::from_secs(30));
            delay.mark(Instant::now(), Some((10 * MS, 100 * MS)));
            let stamp = delay.stamp();
            tokio::time::advance(6 * MS).await;
            let queued = Instant::now();
            tokio::time::advance(100 * MS).await;
            delay.observe(
                &mut None,
                delay.deadline(queued),
                Instant::now(),
                stamp,
                queue_process,
                || Some((16 * MS, 206 * MS)),
            );
            assert!(
                delay.state.lock().unwrap().above_since.is_some(),
                "queue_process={queue_process:?}"
            );
        }
    }

    #[tokio::test(start_paused = true)]
    async fn invalid_ending_clocks_keep_counting_with_a_queue_process_reading() {
        for cpu in [
            None,
            Some((9 * MS, 206 * MS)),
            Some((16 * MS, 99 * MS)),
            Some((16 * MS, 105 * MS)),
        ] {
            let delay = Delay::default();
            delay.enable(Duration::from_secs(30));
            delay.mark(Instant::now(), Some((10 * MS, 100 * MS)));
            let stamp = delay.stamp();
            tokio::time::advance(6 * MS).await;
            let queued = Instant::now();
            tokio::time::advance(100 * MS).await;
            delay.observe(
                &mut None,
                delay.deadline(queued),
                Instant::now(),
                stamp,
                Some(106 * MS),
                || cpu,
            );
            assert!(
                delay.state.lock().unwrap().above_since.is_some(),
                "cpu={cpu:?}"
            );
        }
    }

    #[tokio::test(start_paused = true)]
    async fn a_queue_process_reading_cannot_replace_an_invalid_job_mark() {
        for bad_mark in 0..4 {
            let delay = Delay::default();
            delay.enable(Duration::from_secs(30));
            let began = Instant::now();
            delay.mark(began, Some((Duration::ZERO, Duration::ZERO)));
            let old = delay.stamp();
            let queued = began + 6 * MS;
            let stamp = match bad_mark {
                0 => 0,
                1 => {
                    for _ in 0..MARKS {
                        delay.mark(began, Some((Duration::ZERO, Duration::ZERO)));
                    }
                    old // Evicted, even though its slot has a new reading.
                }
                2 => {
                    delay.mark(queued + MS, Some((Duration::ZERO, Duration::ZERO)));
                    delay.stamp()
                }
                _ => {
                    delay.mark(
                        queued + Duration::from_secs(1),
                        Some((Duration::ZERO, Duration::ZERO)),
                    );
                    delay.stamp()
                }
            };
            tokio::time::advance(106 * MS).await;
            delay.observe(
                &mut None,
                delay.deadline(queued),
                Instant::now(),
                stamp,
                Some(6 * MS),
                || Some((6 * MS, 106 * MS)),
            );
            assert!(
                delay.state.lock().unwrap().above_since.is_some(),
                "bad_mark={bad_mark}"
            );
        }
    }

    #[tokio::test(start_paused = true)]
    async fn neutral_queue_samples_keep_an_expired_congestion_hold_through_the_new_batch() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        let began = Instant::now();
        delay.mark(began, Some((Duration::ZERO, Duration::ZERO)));
        let stamp = delay.stamp();
        let above = began - 200 * MS;
        let until = began + 20 * MS;
        {
            let mut state = delay.state.lock().unwrap();
            let mark = state.marks[stamp as usize % MARKS].unwrap();
            state.above_since = Some(above);
            state.run = Some(mark);
            state.initial = Some(mark);
            state.until = Some(until);
        }
        delay.congested.store(true, Ordering::Release);
        tokio::time::advance(6 * MS).await;
        let queued = Instant::now();
        tokio::time::advance(100 * MS).await;
        delay.observe(
            &mut None,
            delay.deadline(queued),
            Instant::now(),
            stamp,
            Some(6 * MS),
            || Some((6 * MS, 106 * MS)),
        );
        {
            let state = delay.state.lock().unwrap();
            assert_eq!(state.above_since, Some(above));
            assert_eq!(state.until, Some(until));
            assert_eq!(state.run.unwrap().number, stamp);
            assert_eq!(state.initial.unwrap().number, stamp);
        }
        assert!(delay.held.load(Ordering::Relaxed));
        assert!(delay.overloaded());
        tokio::time::advance(24 * MS).await;
        assert!(delay.overloaded());
        delay.batch_done(Instant::now(), || Some((6 * MS, 106 * MS)));
        tokio::time::advance(19 * MS).await;
        assert!(delay.overloaded());
        tokio::time::advance(2 * MS).await;
        assert!(!delay.overloaded());
    }
}
