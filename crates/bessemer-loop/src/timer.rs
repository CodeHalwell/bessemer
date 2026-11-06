use std::io;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::time::Duration;

use tokio::io::unix::AsyncFd;

pub(crate) struct Timer {
    fd: AsyncFd<TimerFd>,
}

impl Timer {
    pub(crate) fn new() -> io::Result<Self> {
        let fd = TimerFd::new()?;
        // SAFETY: TimerFd owns the descriptor until AsyncFd deregisters it.
        let fd = unsafe { AsyncFd::register_with_interest(fd, tokio::io::Interest::READABLE) }
            .map_err(|error| error.into_parts().1)?;
        Ok(Self { fd })
    }

    pub(crate) async fn wait(&self, duration: Duration) -> io::Result<()> {
        self.fd.get_ref().arm(duration)?;
        let mut armed = Armed {
            fd: &self.fd,
            consumed: false,
        };
        loop {
            let mut ready = self.fd.readable().await?;
            match ready.try_io(|fd| fd.get_ref().consume()) {
                Ok(result) => {
                    armed.consumed = result.is_ok();
                    if armed.consumed {
                        ready.clear_ready();
                    }
                    return result;
                }
                Err(_) => continue,
            }
        }
    }

    #[cfg(test)]
    pub(crate) fn is_disarmed(&self) -> bool {
        self.fd.get_ref().is_disarmed()
    }
}

struct Armed<'a> {
    fd: &'a AsyncFd<TimerFd>,
    consumed: bool,
}

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

    fn set(&self, duration: Duration) -> io::Result<()> {
        let value = libc::itimerspec {
            it_interval: libc::timespec {
                tv_sec: 0,
                tv_nsec: 0,
            },
            it_value: libc::timespec {
                tv_sec: duration.as_secs() as libc::time_t,
                tv_nsec: duration.subsec_nanos() as libc::c_long,
            },
        };
        if unsafe { libc::timerfd_settime(self.as_raw_fd(), 0, &value, std::ptr::null_mut()) } < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    fn arm(&self, duration: Duration) -> io::Result<()> {
        self.set(duration.max(Duration::from_nanos(1)))
    }

    fn disarm(&self) -> io::Result<()> {
        self.set(Duration::ZERO)
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

    #[test]
    fn cancellation_disarms_and_rearming_preserves_the_new_deadline() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(async {
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
}
