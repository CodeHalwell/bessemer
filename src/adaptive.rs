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
//! waited, or the rest of the process was idle (a loop blocked in a call).
//! It is excused when the loop thread used at most a target of CPU time,
//! other threads used at least a target and at least twice the loop's CPU.
//! Several short scheduling slices can supply that evidence together; each
//! slice need not have a full target of CPU time to give the process.
//! Use the work the process did, not its share of wall time: contention for
//! a processor can stretch a GIL wait without making the loop congested.
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
    /// First CPU reading in this run of late observations. Requests queued
    /// before tracking began carry zero; their continued wait can still be
    /// attributed once there is positive evidence from subsequent readings.
    initial: Option<Mark>,
    /// Unlike `newest`, this does not reset on a healthy observation: a
    /// request retaining an old mark must not match a different batch's mark.
    number: u32,
    marks: [Option<Mark>; MARKS],
}

/// A CPU reading, at the end of one batch or at the first late observation.
#[derive(Clone, Copy)]
struct Mark {
    number: u32,
    at: Instant,
    worked: Duration,
    process: Duration,
}

impl Mark {
    fn work_since(self, now: Instant, cpu: Cpu) -> Option<(Duration, Duration)> {
        if now < self.at {
            return None;
        }
        let (worked, process) = cpu?;
        let worked = worked.checked_sub(self.worked)?;
        let process = process.checked_sub(self.process)?;
        Some((worked, process.checked_sub(worked)?))
    }
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

    /// Queue observation reuses invocation timestamps. Measure queueing
    /// immediately before a job starts, not its handler's completion time.
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
                let mut state = self.state.lock().unwrap();
                state.above_since = None;
                state.initial = None;
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
        let initial = cpu.map(|(worked, process)| Mark {
            number: 0,
            at: now,
            worked,
            process,
        });
        if state.initial.is_none() {
            state.initial = initial;
        }
        let mark = if stamp == 0 {
            state.initial
        } else {
            state.marks[stamp as usize % MARKS].filter(|mark| mark.number == stamp)
        };
        let external_work = |(worked, others): (Duration, Duration)| {
            worked <= *target && others >= *target && others >= worked * 2
        };
        // Missing, regressing or inconsistent per-request readings are not
        // evidence that another thread held the loop up. Only a valid mark
        // can also consult the accumulated readings for this late streak.
        let attributed = mark
            .and_then(|mark| mark.work_since(now, cpu))
            .filter(|(worked, _)| *worked <= *target)
            .is_some_and(|work| {
                external_work(work)
                    || state
                        .initial
                        .filter(|mark| now.saturating_duration_since(mark.at) <= *target * 20)
                        .and_then(|mark| mark.work_since(now, cpu))
                        .is_some_and(external_work)
            });
        if attributed {
            // The loop was kept from this request by the rest of the
            // process, not by work of its own. That is not congestion,
            // and it ends a run of samples that looked like it.
            state.above_since = None;
            state.initial = initial;
            return;
        }
        if state
            .initial
            .is_some_and(|mark| now.saturating_duration_since(mark.at) >= *target * 20)
        {
            // Incidental background CPU must not accumulate indefinitely
            // into an exemption. Use the same window as the persistence
            // decision, without resetting the run of delayed requests.
            state.initial = initial;
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
        state.initial = None;
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
            for batch in 1..=100 {
                let queued = Instant::now();
                tokio::time::advance(gap).await;
                delay.observe(delay.deadline(queued), Instant::now(), 0, || {
                    Some((work * batch, process * batch))
                });
            }
            assert!(!delay.overloaded(), "gap={gap:?}");
            assert_eq!(delay.stamp(), 0, "no batch has finished yet");
        }
    }

    #[tokio::test(start_paused = true)]
    async fn a_cold_batch_still_sheds_for_its_own_work_or_an_idle_blocked_loop() {
        for (work, process) in [(8 * MS, 20 * MS), (MS / 10, MS / 10)] {
            let delay = Delay::default();
            delay.enable(Duration::from_secs(30));
            for batch in 1..=10 {
                let queued = Instant::now();
                tokio::time::advance(20 * MS).await;
                delay.observe(delay.deadline(queued), Instant::now(), 0, || {
                    Some((work * batch, process * batch))
                });
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
            for batch in 1..=100 {
                let queued = Instant::now();
                let stamp = delay.stamp();
                tokio::time::advance(20 * MS).await;
                let now = Instant::now();
                let cpu = Some((MS / 10 * batch, MS / 2 * batch));
                delay.observe(delay.deadline(queued), now, stamp, || cpu);
                if batch_marks {
                    delay.batch_done(now, || cpu);
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
        delay.observe(first - MS, first, 0, || {
            Some((Duration::ZERO, Duration::ZERO))
        });
        delay.batch_done(first, || Some((Duration::ZERO, Duration::ZERO)));
        let stamp = delay.stamp();
        tokio::time::advance(80 * MS).await;
        let now = Instant::now();
        let cpu = Some((MS / 10, 49 * MS / 10));
        // The first 80 ms have 4.8 ms of other-thread CPU: not enough yet.
        delay.observe(delay.deadline(first), now, stamp, || cpu);
        delay.batch_done(now, || cpu);
        let stamp = delay.stamp();
        tokio::time::advance(40 * MS).await;
        // This request's valid mark sees only another 0.4 ms. Adding it to
        // the initial reading would cross the CPU floor, but that reading
        // is now outside the 100 ms persistence window.
        delay.observe(delay.deadline(now), Instant::now(), stamp, || {
            Some((MS / 5, 54 * MS / 10))
        });
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
            delay.observe(delay.deadline(queued), first, 0, || {
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
            delay.observe(delay.deadline(queued), Instant::now(), stamp, || {
                Some((4 * MS, 120 * MS))
            });
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
        delay.observe(delay.deadline(queued), first, 0, || {
            Some((8 * MS, 100 * MS))
        });
        tokio::time::advance(20 * MS).await;
        // The current streak alone would qualify: two milliseconds of loop
        // CPU and much more elsewhere. But this job waited behind ten
        // milliseconds of actual loop work, so it must still count as late.
        delay.observe(delay.deadline(queued), Instant::now(), stamp, || {
            Some((10 * MS, 200 * MS))
        });
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
                delay.observe(delay.deadline(queued), Instant::now(), stamp, || {
                    Some(sample)
                });
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
            delay.observe(delay.deadline(queued), Instant::now(), stamp, || {
                Some((2 * MS, 120 * MS))
            });
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
        delay.state.lock().unwrap().number = u32::MAX;
        delay.batch_done(now, || Some((MS, MS)));
        assert_eq!(delay.stamp(), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn healthy_resets_do_not_reuse_an_old_requests_mark_number() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        run(&delay, 1, 20 * MS, MS / 5, 20 * MS).await;
        let old = delay.stamp();
        let now = Instant::now();
        delay.observe(now + MS, now, old, none);
        assert_eq!(delay.stamp(), 0);
        delay.observe(now - MS, now, 0, || Some((MS, 40 * MS)));
        delay.batch_done(now, || Some((MS, 40 * MS)));
        assert_ne!(delay.stamp(), old);
    }
}
