//! The readiness reactor under the loop: epoll on Linux, kqueue on macOS.
//!
//! Both are used level-triggered, so a descriptor stays reported while it is
//! ready, as selectors' `EpollSelector` and `KqueueSelector` behave. The
//! event type is the platform's own (`epoll_event`, `kevent`) so the Linux
//! hot path reads the same fields it always has.

use std::io;
use std::os::fd::RawFd;

/// Interest in reading, in the loop's own registration mask.
pub(crate) const READ: u32 = 1;
/// Interest in writing, in the loop's own registration mask.
pub(crate) const WRITE: u32 = 2;

#[cfg(target_os = "linux")]
mod imp {
    use super::*;

    pub(crate) type Event = libc::epoll_event;

    pub(crate) const EMPTY: Event = libc::epoll_event { events: 0, u64: 0 };

    pub(crate) fn create() -> io::Result<RawFd> {
        let fd = unsafe { libc::epoll_create1(libc::EPOLL_CLOEXEC) };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(fd)
    }

    #[inline(always)]
    fn bits(mask: u32) -> u32 {
        (if mask & READ != 0 {
            libc::EPOLLIN as u32
        } else {
            0
        }) | (if mask & WRITE != 0 {
            libc::EPOLLOUT as u32
        } else {
            0
        })
    }

    fn ctl(poller: RawFd, op: i32, fd: RawFd, mask: u32) -> io::Result<()> {
        let mut event = libc::epoll_event {
            events: bits(mask),
            u64: fd as u64,
        };
        if unsafe { libc::epoll_ctl(poller, op, fd, &mut event) } < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    /// Bring the registration of `fd` from `old` to `new`. The loop's map can
    /// be stale when a descriptor was closed and reused without being
    /// unregistered, so a failed MOD retries as ADD and a failed ADD as MOD.
    pub(crate) fn update(poller: RawFd, fd: RawFd, old: u32, new: u32) -> io::Result<()> {
        if new == 0 {
            match ctl(poller, libc::EPOLL_CTL_DEL, fd, 0) {
                Err(e) if matches!(e.raw_os_error(), Some(libc::ENOENT) | Some(libc::EBADF)) => {
                    Ok(())
                }
                other => other,
            }
        } else if old == 0 {
            match ctl(poller, libc::EPOLL_CTL_ADD, fd, new) {
                Err(e) if e.raw_os_error() == Some(libc::EEXIST) => {
                    ctl(poller, libc::EPOLL_CTL_MOD, fd, new)
                }
                other => other,
            }
        } else {
            match ctl(poller, libc::EPOLL_CTL_MOD, fd, new) {
                Err(e) if e.raw_os_error() == Some(libc::ENOENT) => {
                    ctl(poller, libc::EPOLL_CTL_ADD, fd, new)
                }
                other => other,
            }
        }
    }

    /// Wait up to `timeout_ms` (-1: no limit) for events into `events`.
    /// Returns the raw result: the count, or -1 with errno set.
    #[inline(always)]
    pub(crate) unsafe fn wait(
        poller: RawFd,
        events: *mut Event,
        max: usize,
        timeout_ms: i32,
    ) -> i32 {
        unsafe { libc::epoll_wait(poller, events, max as i32, timeout_ms) }
    }

    #[inline(always)]
    pub(crate) fn fd(event: &Event) -> RawFd {
        event.u64 as RawFd
    }

    /// As `EpollSelector`: anything but EPOLLOUT counts as readable.
    #[inline(always)]
    pub(crate) fn readable(event: &Event) -> bool {
        event.events & !(libc::EPOLLOUT as u32) != 0
    }

    /// As `EpollSelector`: anything but EPOLLIN counts as writable.
    #[inline(always)]
    pub(crate) fn writable(event: &Event) -> bool {
        event.events & !(libc::EPOLLIN as u32) != 0
    }

    /// An event that reports `fd` readable, for wake-ups made in-process.
    #[inline(always)]
    pub(crate) fn read_event(fd: RawFd) -> Event {
        libc::epoll_event {
            events: libc::EPOLLIN as u32,
            u64: fd as u64,
        }
    }
}

#[cfg(target_os = "macos")]
mod imp {
    use super::*;

    pub(crate) type Event = libc::kevent;

    pub(crate) const EMPTY: Event = libc::kevent {
        ident: 0,
        filter: 0,
        flags: 0,
        fflags: 0,
        data: 0,
        udata: std::ptr::null_mut(),
    };

    pub(crate) fn create() -> io::Result<RawFd> {
        let fd = unsafe { libc::kqueue() };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        // kqueue descriptors are not inherited across fork, but mark it so
        // an exec in this process does not see it either.
        unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) };
        Ok(fd)
    }

    fn change(fd: RawFd, filter: i16, flags: u16) -> Event {
        libc::kevent {
            ident: fd as libc::uintptr_t,
            filter,
            flags: flags | libc::EV_RECEIPT,
            fflags: 0,
            data: 0,
            udata: std::ptr::null_mut(),
        }
    }

    /// Bring the registration of `fd` from `old` to `new`. Every filter in
    /// `new` is (re-)added, which is idempotent, because the loop's map can
    /// be stale: a closed descriptor's filters are gone from the kqueue even
    /// though the map still lists them. Deleting a filter the kernel no
    /// longer has is not an error.
    pub(crate) fn update(poller: RawFd, fd: RawFd, old: u32, new: u32) -> io::Result<()> {
        let mut changes = [EMPTY; 2];
        let mut n = 0;
        for (bit, filter) in [(READ, libc::EVFILT_READ), (WRITE, libc::EVFILT_WRITE)] {
            if new & bit != 0 {
                changes[n] = change(fd, filter, libc::EV_ADD);
                n += 1;
            } else if old & bit != 0 {
                changes[n] = change(fd, filter, libc::EV_DELETE);
                n += 1;
            }
        }
        if n == 0 {
            return Ok(());
        }
        let mut receipts = [EMPTY; 2];
        let rc = unsafe {
            libc::kevent(
                poller,
                changes.as_ptr(),
                n as i32,
                receipts.as_mut_ptr(),
                n as i32,
                std::ptr::null(),
            )
        };
        if rc < 0 {
            return Err(io::Error::last_os_error());
        }
        // With EV_RECEIPT every change comes back with EV_ERROR set and its
        // result (0 for success) in `data`.
        for (receipt, sent) in receipts[..rc as usize].iter().zip(&changes[..n]) {
            let errno = receipt.data as i32;
            if receipt.flags & libc::EV_ERROR == 0 || errno == 0 {
                continue;
            }
            let deleting = sent.flags & libc::EV_DELETE != 0;
            if deleting && matches!(errno, libc::ENOENT | libc::EBADF) {
                continue;
            }
            return Err(io::Error::from_raw_os_error(errno));
        }
        Ok(())
    }

    /// Wait up to `timeout_ms` (-1: no limit) for events into `events`.
    /// Returns the raw result: the count, or -1 with errno set.
    #[inline(always)]
    pub(crate) unsafe fn wait(
        poller: RawFd,
        events: *mut Event,
        max: usize,
        timeout_ms: i32,
    ) -> i32 {
        let timeout = libc::timespec {
            tv_sec: (timeout_ms / 1000) as libc::time_t,
            tv_nsec: ((timeout_ms % 1000) as libc::c_long) * 1_000_000,
        };
        let timeout = if timeout_ms < 0 {
            std::ptr::null()
        } else {
            &timeout as *const libc::timespec
        };
        unsafe { libc::kevent(poller, std::ptr::null(), 0, events, max as i32, timeout) }
    }

    #[inline(always)]
    pub(crate) fn fd(event: &Event) -> RawFd {
        event.ident as RawFd
    }

    /// As `KqueueSelector`: the read filter, including at end of file.
    #[inline(always)]
    pub(crate) fn readable(event: &Event) -> bool {
        event.filter == libc::EVFILT_READ
    }

    /// As `KqueueSelector`: the write filter.
    #[inline(always)]
    pub(crate) fn writable(event: &Event) -> bool {
        event.filter == libc::EVFILT_WRITE
    }

    /// An event that reports `fd` readable, for wake-ups made in-process.
    #[inline(always)]
    pub(crate) fn read_event(fd: RawFd) -> Event {
        libc::kevent {
            ident: fd as libc::uintptr_t,
            filter: libc::EVFILT_READ,
            ..EMPTY
        }
    }
}

pub(crate) use imp::*;
