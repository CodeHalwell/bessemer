//! An embedded Tokio runtime driven by the loop's own thread.
//!
//! With a driver, the loop waits for its poller (epoll or kqueue) descriptor
//! inside a Tokio current-thread runtime instead of blocking in the poller,
//! so futures spawned on that runtime (the HTTP server's connections) run on
//! the loop's thread between its callbacks, without another thread or a
//! hand-off.

use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::pin::Pin;
use std::sync::Mutex;
use std::time::Duration;

use tokio::io::unix::AsyncFd;
use tokio::runtime::{Builder, Handle, Runtime};
use tokio::time::{Instant, Sleep};

use crate::timer::Timer;

const NATIVE_TIMER_WINDOW: Duration = Duration::from_millis(10);

/// Tokio's timer wheel works in milliseconds.
const TIMER_RESOLUTION: Duration = Duration::from_millis(1);

/// A duplicate of the loop's poller descriptor, owned by the driver, so it
/// stays open while registered with Tokio even after the loop closes its own.
struct PollerFd(OwnedFd);

impl AsRawFd for PollerFd {
    fn as_raw_fd(&self) -> RawFd {
        self.0.as_raw_fd()
    }
}

pub(crate) struct Driver {
    /// Dropped before the runtime, whose reactor it is registered with.
    poller: Option<AsyncFd<PollerFd>>,
    timer: Option<Timer>,
    /// Ends a wait early for work queued on the loop without a descriptor.
    notify: tokio::sync::Notify,
    /// The one timer for the coarse part of a distant wait, moved in place
    /// instead of a new timer registered and removed on every wait.
    coarse: Mutex<Option<Pin<Box<Sleep>>>>,
    /// Always set until the driver is dropped.
    runtime: Option<Runtime>,
}

impl Drop for Driver {
    fn drop(&mut self) {
        drop(self.poller.take());
        drop(self.timer.take());
        if let Some(runtime) = self.runtime.take() {
            // Drop the spawned tasks without waiting for blocking work, which
            // may itself be waiting on the loop.
            runtime.shutdown_background();
        }
    }
}

impl Driver {
    pub(crate) fn new(epfd: RawFd) -> std::io::Result<Self> {
        let runtime = Builder::new_current_thread().enable_all().build()?;
        let duplicate = unsafe { libc::fcntl(epfd, libc::F_DUPFD_CLOEXEC, 0) };
        if duplicate < 0 {
            return Err(std::io::Error::last_os_error());
        }
        // SAFETY: `duplicate` is a new descriptor that nothing else owns.
        let owned = unsafe { OwnedFd::from_raw_fd(duplicate) };
        let poller = {
            let _context = runtime.enter();
            // SAFETY: the `PollerFd` owns the descriptor for the AsyncFd's life.
            unsafe {
                AsyncFd::register_with_interest(PollerFd(owned), tokio::io::Interest::READABLE)
            }
            .map_err(|error| error.into_parts().1)?
        };
        let timer = {
            let _context = runtime.enter();
            Timer::new().ok()
        };
        Ok(Self {
            poller: Some(poller),
            timer,
            notify: tokio::sync::Notify::new(),
            coarse: Mutex::new(None),
            runtime: Some(runtime),
        })
    }

    fn runtime(&self) -> &Runtime {
        self.runtime
            .as_ref()
            .expect("the runtime lives as long as the driver")
    }

    fn poller_fd(&self) -> &AsyncFd<PollerFd> {
        self.poller
            .as_ref()
            .expect("the descriptor lives as long as the driver")
    }

    pub(crate) fn handle(&self) -> Handle {
        self.runtime().handle().clone()
    }

    /// Run ready work, then wait for readiness or the optional timeout.
    pub(crate) fn wait(&self, timeout: Option<Duration>) {
        self.runtime().block_on(self.wait_async(timeout));
    }

    /// End the current or next wait.
    pub(crate) fn notify(&self) {
        self.notify.notify_one();
    }

    /// Run `future` on the runtime from the calling thread.
    pub(crate) fn block_on<F: std::future::Future>(&self, future: F) -> F::Output {
        self.runtime().block_on(future)
    }

    /// As `wait`, from inside `block_on`.
    pub(crate) async fn wait_async(&self, timeout: Option<Duration>) -> bool {
        if timeout == Some(Duration::ZERO) {
            // Let spawned tasks that are ready run once, without the
            // scheduler's extra driver poll that `yield_now` asks for.
            let mut yielded = false;
            std::future::poll_fn(|cx| {
                if yielded {
                    return std::task::Poll::Ready(());
                }
                yielded = true;
                cx.waker().wake_by_ref();
                std::task::Poll::Pending
            })
            .await;
        } else {
            let wait = || async {
                tokio::select! {
                    biased;
                    _ = self.notify.notified() => {}
                    _ = self.poller_fd().readable() => {}
                }
            };
            if let Some(timeout) = timeout {
                if let Some(timer) = self
                    .timer
                    .as_ref()
                    .filter(|_| timeout <= NATIVE_TIMER_WINDOW)
                {
                    tokio::select! {
                        biased;
                        _ = wait() => {}
                        result = timer.wait(timeout) => {
                            if result.is_err() {
                                // The timer failed to arm or wait: fall back
                                // to Tokio's for the same span. No clock
                                // reading is taken up front for this rare
                                // path, which a wait ended by I/O never needs.
                                let _ = tokio::time::timeout(timeout, wait()).await;
                            }
                        }
                    }
                } else {
                    // Recompute the deadline in the core before its final
                    // native wait; distant timers need no syscalls on I/O wakes.
                    let coarse = if self.timer.is_some() {
                        timeout.saturating_sub(NATIVE_TIMER_WINDOW)
                    } else {
                        timeout
                    };
                    self.wait_coarse(coarse, wait()).await;
                }
            } else {
                wait().await;
            }
        }
        self.take_readable()
    }

    /// Run `work` for at most `limit`, on the driver's reusable timer.
    ///
    /// The timer is moved only when its deadline is a timer resolution or
    /// more from the new one, which is what Tokio's own timers round to, so
    /// back-to-back waits on a steady distant deadline cost no timer-wheel
    /// work. The wait then ends within that resolution of `limit`.
    async fn wait_coarse(&self, limit: Duration, work: impl std::future::Future<Output = ()>) {
        // Taken out for the wait, so no lock is held across it; a wait that is
        // dropped before it ends leaves a fresh timer to be made next time.
        let held = self.coarse.lock().ok().and_then(|mut slot| slot.take());
        let target = Instant::now() + limit;
        let mut sleep = held.unwrap_or_else(|| Box::pin(tokio::time::sleep_until(target)));
        let current = sleep.deadline();
        let gap = if target >= current {
            target - current
        } else {
            current - target
        };
        if sleep.is_elapsed() || gap >= TIMER_RESOLUTION {
            sleep.as_mut().reset(target);
        }
        tokio::select! {
            biased;
            () = work => {}
            () = sleep.as_mut() => {}
        }
        if let Ok(mut slot) = self.coarse.lock() {
            *slot = Some(sleep);
        }
    }

    /// Whether the poller descriptor has reported events since the last call;
    /// the caller then drains it. A later event makes it readable again.
    fn take_readable(&self) -> bool {
        let waker = std::task::Waker::noop();
        let mut cx = std::task::Context::from_waker(waker);
        match self.poller_fd().poll_read_ready(&mut cx) {
            std::task::Poll::Ready(Ok(mut guard)) => {
                guard.clear_ready();
                true
            }
            std::task::Poll::Ready(Err(_)) => true,
            std::task::Poll::Pending => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn driver() -> Driver {
        let fd = crate::poller::create().unwrap();
        // SAFETY: create returned a new, exclusively owned descriptor.
        let fd = unsafe { OwnedFd::from_raw_fd(fd) };
        Driver::new(fd.as_raw_fd()).unwrap()
    }

    #[test]
    fn high_resolution_wait_does_not_use_tokio_clock() {
        let driver = driver();
        driver.timer.as_ref().expect("native timer is available");
        driver.block_on(async {
            tokio::time::pause();
            let clock = tokio::time::Instant::now();
            let started = std::time::Instant::now();
            let delay = Duration::from_micros(250);
            assert!(!driver.wait_async(Some(delay)).await);
            assert!(started.elapsed() >= delay);
            assert_eq!(tokio::time::Instant::now(), clock);
        });
    }

    #[test]
    fn unavailable_native_timer_falls_back_to_tokio() {
        let mut driver = driver();
        driver.timer.take();
        driver.block_on(async {
            tokio::time::pause();
            let clock = tokio::time::Instant::now();
            let delay = Duration::from_micros(250);
            assert!(!driver.wait_async(Some(delay)).await);
            assert!(tokio::time::Instant::now() - clock >= delay);
        });
    }

    #[test]
    fn distant_wait_defers_native_timer_until_the_final_window() {
        use std::future::Future;
        use std::task::Poll;

        let driver = driver();
        let timer = driver.timer.as_ref().expect("native timer is available");
        driver.block_on(async {
            tokio::time::pause();
            let clock = tokio::time::Instant::now();
            let delay = Duration::from_millis(30);
            let wait = driver.wait_async(Some(delay));
            tokio::pin!(wait);
            std::future::poll_fn(|cx| {
                assert!(wait.as_mut().poll(cx).is_pending());
                assert!(timer.is_disarmed());
                Poll::Ready(())
            })
            .await;
            assert!(!wait.await);
            let coarse = tokio::time::Instant::now() - clock;
            assert!(coarse >= delay - NATIVE_TIMER_WINDOW);
            assert!(coarse < delay);
            assert!(timer.is_disarmed());

            let clock = tokio::time::Instant::now();
            let started = std::time::Instant::now();
            let remaining = Duration::from_micros(250);
            assert!(!driver.wait_async(Some(remaining)).await);
            assert!(started.elapsed() >= remaining);
            assert_eq!(tokio::time::Instant::now(), clock);
        });
    }

    #[test]
    fn the_coarse_timer_follows_a_nearer_deadline_after_a_distant_one() {
        let driver = driver();
        driver.block_on(async {
            tokio::time::pause();
            // A distant wait, ended early by a notification, leaves its timer
            // armed far away; the next wait must still end at its own limit.
            driver.notify();
            assert!(!driver.wait_async(Some(Duration::from_secs(30))).await);
            let clock = tokio::time::Instant::now();
            let delay = Duration::from_millis(40);
            assert!(!driver.wait_async(Some(delay)).await);
            let waited = tokio::time::Instant::now() - clock;
            assert!(waited >= delay - NATIVE_TIMER_WINDOW, "{waited:?}");
            assert!(waited < delay, "{waited:?}");
            // And back out to a distant one, then a steady repeat of it.
            driver.notify();
            assert!(!driver.wait_async(Some(Duration::from_secs(30))).await);
            let clock = tokio::time::Instant::now();
            assert!(!driver.wait_async(Some(delay)).await);
            assert!(tokio::time::Instant::now() - clock >= delay - NATIVE_TIMER_WINDOW);
        });
    }
}
