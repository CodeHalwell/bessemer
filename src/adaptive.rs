//! Delay-based admission. Suspended handlers consume memory, not loop time:
//! a healthy sleeping workload may use the safety ceiling without being shed.
use std::sync::{
    Mutex, OnceLock,
    atomic::{AtomicBool, Ordering},
};
use std::time::Duration;
use tokio::time::Instant;

pub(crate) const CEILING: usize = 4096;

#[derive(Default)]
pub(crate) struct Delay {
    target: OnceLock<Duration>,
    congested: AtomicBool,
    tracking: AtomicBool,
    state: Mutex<State>,
}

#[derive(Default)]
struct State {
    above_since: Option<Instant>,
    until: Option<Instant>,
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

    /// Queue observation reuses invocation timestamps; batch observation adds
    /// one completion sample. Measure loop work and queueing, not handler RTT.
    #[inline]
    pub(crate) fn observe(&self, deadline: Instant, now: Instant) {
        if now <= deadline {
            if self.tracking.load(Ordering::Relaxed) {
                self.tracking.store(false, Ordering::Relaxed);
                self.state.lock().unwrap().above_since = None;
            }
            return;
        }
        self.delayed(now);
    }

    #[cold]
    fn delayed(&self, now: Instant) {
        let Some(target) = self.target.get() else {
            return;
        };
        let mut state = self.state.lock().unwrap();
        self.tracking.store(true, Ordering::Relaxed);
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
        self.congested.store(false, Ordering::Release);
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test(start_paused = true)]
    async fn queue_delay_sheds_then_recovers_without_releasing_permits() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        let queued = Instant::now();
        tokio::time::advance(Duration::from_millis(101)).await;
        delay.observe(delay.deadline(queued), Instant::now());
        assert!(!delay.overloaded());
        tokio::time::advance(Duration::from_millis(101)).await;
        delay.observe(delay.deadline(queued), Instant::now());
        assert!(delay.overloaded());
        tokio::time::advance(Duration::from_millis(20)).await;
        assert!(!delay.overloaded());
        let queued = Instant::now();
        tokio::time::advance(Duration::from_millis(1)).await;
        delay.observe(delay.deadline(queued), Instant::now());
        assert!(!delay.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn target_deadline_boundary_is_healthy_and_clears_tracking() {
        let delay = Delay::default();
        delay.enable(Duration::from_secs(30));
        let started = Instant::now();
        let deadline = delay.deadline(started);
        assert_eq!(deadline - started, Duration::from_millis(5));
        delay.observe(deadline, deadline + Duration::from_nanos(1));
        assert!(delay.tracking.load(Ordering::Relaxed));
        delay.observe(deadline, deadline);
        assert!(!delay.tracking.load(Ordering::Relaxed));
        assert!(delay.state.lock().unwrap().above_since.is_none());
        delay.observe(deadline, started);
        assert!(!delay.overloaded());
    }

    #[tokio::test(start_paused = true)]
    async fn fixed_caps_never_enable_delay_shedding() {
        let delay = Delay::default();
        let started = Instant::now();
        tokio::time::advance(Duration::from_secs(60)).await;
        delay.observe(delay.deadline(started), Instant::now());
        assert!(!delay.overloaded());
    }
}
