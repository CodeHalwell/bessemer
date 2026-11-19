use std::io;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use tokio::io::unix::AsyncFd;

/// Deadlines are rounded up to a multiple of this, so the waits for one
/// timer, each computed from a slightly later clock reading, usually ask for
/// the same deadline and leave the timer as it is. A wait ends at most this
/// much after its deadline, never before it.
#[cfg(target_os = "linux")]
const GRAIN_NS: u64 = 50_000;

pub(crate) struct Timer {
    fd: AsyncFd<TimerFd>,
    /// The deadline the timer is set to, in CLOCK_MONOTONIC nanoseconds (0:
    /// not set), where the platform's timer takes one (Linux).
    armed: AtomicU64,
}

impl Timer {
    pub(crate) fn new() -> io::Result<Self> {
        let fd = TimerFd::new()?;
        // SAFETY: TimerFd owns the descriptor until AsyncFd deregisters it.
        let fd = unsafe { AsyncFd::register_with_interest(fd, tokio::io::Interest::READABLE) }
            .map_err(|error| error.into_parts().1)?;
        Ok(Self {
            fd,
            armed: AtomicU64::new(0),
        })
    }

    /// Wait for `duration`, or a little longer (see `GRAIN_NS`).
    ///
    /// On Linux the timer is set to an absolute deadline and left set when
    /// the wait is dropped (I/O ended it first): the next wait, usually for
    /// the same timer of the loop, then finds it set already and makes no
    /// system call, where arming on entry and disarming on drop made two on
    /// every wake-up. A deadline left set that nobody waits for any more
    /// fires once, unread; setting the timer again discards that expiry.
    pub(crate) async fn wait(&self, duration: Duration) -> io::Result<()> {
        let mut armed = self.arm(duration)?;
        loop {
            let mut ready = self.fd.readable().await?;
            match ready.try_io(|fd| fd.get_ref().consume()) {
                Ok(result) => {
                    if result.is_ok() {
                        ready.clear_ready();
                        // Expired: it is no longer set.
                        self.armed.store(0, Ordering::Relaxed);
                        armed.consumed();
                    }
                    return result;
                }
                Err(_) => continue,
            }
        }
    }

    #[cfg(target_os = "linux")]
    fn arm(&self, duration: Duration) -> io::Result<Armed> {
        let wanted = now_ns()
            .saturating_add(duration.as_nanos().min(u64::MAX as u128) as u64)
            .max(1)
            .div_ceil(GRAIN_NS)
            .saturating_mul(GRAIN_NS);
        self.set_deadline(wanted)?;
        Ok(Armed)
    }

    /// Set the timer to `deadline` unless it is set to it already; whether
    /// it had to be set.
    #[cfg(target_os = "linux")]
    fn set_deadline(&self, deadline: u64) -> io::Result<bool> {
        if self.armed.load(Ordering::Relaxed) == deadline {
            return Ok(false);
        }
        // Forget the old deadline first: should setting fail, the timer is in
        // an unknown state and is set again next time.
        self.armed.store(0, Ordering::Relaxed);
        self.fd.get_ref().arm_at(deadline)?;
        self.armed.store(deadline, Ordering::Relaxed);
        Ok(true)
    }

    #[cfg(target_os = "macos")]
    fn arm(&self, duration: Duration) -> io::Result<Armed<'_>> {
        self.fd.get_ref().arm(duration)?;
        Ok(Armed {
            fd: &self.fd,
            consumed: false,
        })
    }

    #[cfg(test)]
    pub(crate) fn is_disarmed(&self) -> bool {
        self.fd.get_ref().is_disarmed()
    }
}

/// On Linux the timer stays set when a wait is dropped.
#[cfg(target_os = "linux")]
struct Armed;

#[cfg(target_os = "linux")]
impl Armed {
    fn consumed(&mut self) {}
}

/// CLOCK_MONOTONIC in nanoseconds, the clock the timer is set on.
#[cfg(target_os = "linux")]
fn now_ns() -> u64 {
    let mut ts = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts) };
    ts.tv_sec as u64 * 1_000_000_000 + ts.tv_nsec as u64
}

/// On macOS a relative one-shot timer, disarmed when the wait is dropped.
#[cfg(target_os = "macos")]
struct Armed<'a> {
    fd: &'a AsyncFd<TimerFd>,
    consumed: bool,
}

#[cfg(target_os = "macos")]
impl Armed<'_> {
    fn consumed(&mut self) {
        self.consumed = true;
    }
}

#[cfg(target_os = "macos")]
impl Drop for Armed<'_> {
    fn drop(&mut self) {
        if !self.consumed {
            let _ = self.fd.get_ref().disarm();
        }
    }
}

struct TimerFd(OwnedFd);

impl AsRawFd for TimerFd {
    fn as_raw_fd(&self) -> RawFd {
        self.0.as_raw_fd()
    }
}

#[cfg(target_os = "linux")]
impl TimerFd {
    fn new() -> io::Result<Self> {
        let fd = unsafe {
            libc::timerfd_create(
                libc::CLOCK_MONOTONIC,
                libc::TFD_NONBLOCK | libc::TFD_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: timerfd_create returned a new, exclusively owned descriptor.
        Ok(Self(unsafe { OwnedFd::from_raw_fd(fd) }))
    }

    /// Set the timer to expire at `deadline` (CLOCK_MONOTONIC nanoseconds,
    /// not 0). Setting it discards an expiry not yet read.
    fn arm_at(&self, deadline: u64) -> io::Result<()> {
        let value = libc::itimerspec {
            it_interval: libc::timespec {
                tv_sec: 0,
                tv_nsec: 0,
            },
            it_value: libc::timespec {
                tv_sec: (deadline / 1_000_000_000) as libc::time_t,
                tv_nsec: (deadline % 1_000_000_000) as libc::c_long,
            },
        };
        let rc = unsafe {
            libc::timerfd_settime(
                self.as_raw_fd(),
                libc::TFD_TIMER_ABSTIME,
                &value,
                std::ptr::null_mut(),
            )
        };
        if rc < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    fn consume(&self) -> io::Result<()> {
        let mut expirations = 0_u64;
        loop {
            let result = unsafe {
                libc::read(
                    self.as_raw_fd(),
                    (&mut expirations as *mut u64).cast(),
                    std::mem::size_of::<u64>(),
                )
            };
            if result >= 0 {
                return Ok(());
            }
            let error = io::Error::last_os_error();
            if error.kind() != io::ErrorKind::Interrupted {
                return Err(error);
            }
        }
    }

    #[cfg(test)]
    fn is_disarmed(&self) -> bool {
        let mut value = std::mem::MaybeUninit::<libc::itimerspec>::uninit();
        let result = unsafe { libc::timerfd_gettime(self.as_raw_fd(), value.as_mut_ptr()) };
        assert_eq!(result, 0);
        // SAFETY: timerfd_gettime succeeded and initialized value.
        let value = unsafe { value.assume_init() };
        value.it_value.tv_sec == 0 && value.it_value.tv_nsec == 0
    }
}

#[cfg(target_os = "macos")]
impl TimerFd {
    fn new() -> io::Result<Self> {
        let fd = crate::poller::create()?;
        // SAFETY: create returned a new, exclusively owned kqueue descriptor.
        Ok(Self(unsafe { OwnedFd::from_raw_fd(fd) }))
    }

    fn change(&self, flags: u16, duration: Duration) -> io::Result<()> {
        let event = libc::kevent {
            ident: 1,
            filter: libc::EVFILT_TIMER,
            flags: flags | libc::EV_RECEIPT,
            fflags: libc::NOTE_NSECONDS | libc::NOTE_CRITICAL,
            data: duration.as_nanos().clamp(1, libc::intptr_t::MAX as u128) as libc::intptr_t,
            udata: std::ptr::null_mut(),
        };
        let mut receipt = crate::poller::EMPTY;
        let result = unsafe {
            libc::kevent(
                self.as_raw_fd(),
                &event,
                1,
                &mut receipt,
                1,
                std::ptr::null(),
            )
        };
        if result < 0 {
            return Err(io::Error::last_os_error());
        }
        if receipt.flags & libc::EV_ERROR != 0 && receipt.data != 0 {
            return Err(io::Error::from_raw_os_error(receipt.data as i32));
        }
        Ok(())
    }

    fn arm(&self, duration: Duration) -> io::Result<()> {
        self.change(libc::EV_ADD | libc::EV_ONESHOT, duration)
    }

    fn disarm(&self) -> io::Result<()> {
        match self.change(libc::EV_DELETE, Duration::ZERO) {
            Err(error) if error.raw_os_error() == Some(libc::ENOENT) => Ok(()),
            result => result,
        }
    }

    fn consume(&self) -> io::Result<()> {
        let mut event = crate::poller::EMPTY;
        let timeout = libc::timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        loop {
            let result = unsafe {
                libc::kevent(
                    self.as_raw_fd(),
                    std::ptr::null(),
                    0,
                    &mut event,
                    1,
                    &timeout,
                )
            };
            if result > 0 {
                return Ok(());
            }
            if result == 0 {
                return Err(io::ErrorKind::WouldBlock.into());
            }
            let error = io::Error::last_os_error();
            if error.kind() != io::ErrorKind::Interrupted {
                return Err(error);
            }
        }
    }

    #[cfg(test)]
    fn is_disarmed(&self) -> bool {
        self.change(libc::EV_DELETE, Duration::ZERO)
            .is_err_and(|error| error.raw_os_error() == Some(libc::ENOENT))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::future::Future;
    use std::task::Poll;
    use std::time::Instant;

    fn runtime() -> tokio::runtime::Runtime {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
    }

    /// Poll `wait` once, then drop it, as a wait that I/O ended is.
    #[cfg(target_os = "linux")]
    async fn start_and_drop(wait: impl Future<Output = io::Result<()>>) {
        tokio::pin!(wait);
        std::future::poll_fn(|cx| {
            assert!(wait.as_mut().poll(cx).is_pending());
            Poll::Ready(())
        })
        .await;
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn cancellation_disarms_and_rearming_preserves_the_new_deadline() {
        runtime().block_on(async {
            let timer = Timer::new().expect("native timer is available");
            {
                let wait = timer.wait(Duration::from_secs(60));
                tokio::pin!(wait);
                std::future::poll_fn(|cx| {
                    assert!(wait.as_mut().poll(cx).is_pending());
                    Poll::Ready(())
                })
                .await;
                timer.fd.get_ref().arm(Duration::from_nanos(1)).unwrap();
                drop(timer.fd.readable().await.unwrap());
            }
            assert!(timer.fd.get_ref().is_disarmed());
            assert_eq!(
                timer.fd.get_ref().consume().unwrap_err().kind(),
                io::ErrorKind::WouldBlock
            );
            let delay = Duration::from_millis(3);
            let start = Instant::now();
            timer.wait(delay).await.unwrap();
            assert!(start.elapsed() >= delay);
        });
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn a_deadline_already_set_is_not_set_again() {
        runtime().block_on(async {
            let timer = Timer::new().expect("native timer is available");
            let deadline = now_ns() + 60_000_000_000;
            assert!(timer.set_deadline(deadline).unwrap());
            assert!(!timer.set_deadline(deadline).unwrap());
            assert!(!timer.is_disarmed());
            assert!(timer.set_deadline(deadline + GRAIN_NS).unwrap());
        });
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn a_dropped_wait_leaves_the_timer_set_and_later_waits_keep_their_own_deadline() {
        runtime().block_on(async {
            let timer = Timer::new().expect("native timer is available");
            // A distant deadline left set by a dropped wait.
            start_and_drop(timer.wait(Duration::from_secs(60))).await;
            assert!(!timer.is_disarmed());
            // A nearer one replaces it.
            let delay = Duration::from_millis(3);
            let start = Instant::now();
            timer.wait(delay).await.unwrap();
            let waited = start.elapsed();
            assert!(waited >= delay, "{waited:?}");
            assert!(waited < Duration::from_secs(30), "{waited:?}");
            assert!(timer.is_disarmed());
            // A near deadline left set expires unread; a later, longer wait
            // must not end at that expiry.
            start_and_drop(timer.wait(Duration::from_millis(1))).await;
            std::thread::sleep(Duration::from_millis(5));
            let delay = Duration::from_millis(20);
            let start = Instant::now();
            timer.wait(delay).await.unwrap();
            assert!(start.elapsed() >= delay, "{:?}", start.elapsed());
        });
    }
}
