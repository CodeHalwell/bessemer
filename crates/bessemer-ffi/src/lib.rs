//! The small, audited operating-system ownership boundary for inherited handles.
#![deny(unsafe_op_in_unsafe_fn)]

use std::{io, net::TcpListener};

#[cfg(unix)]
fn duplicate(fd: i32) -> io::Result<std::os::fd::OwnedFd> {
    use std::os::fd::FromRawFd;
    if fd < 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "negative inherited descriptor",
        ));
    }
    // SAFETY: fcntl validates the integer descriptor in the kernel and creates a
    // distinct descriptor. It does not dereference a pointer or take ownership of
    // the caller's original descriptor. CLOEXEC prevents leakage on later exec.
    let cloned = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 0) };
    if cloned == -1 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: successful F_DUPFD_CLOEXEC returned a new valid descriptor owned by
    // this call. Exactly one OwnedFd is constructed, which closes it on drop.
    Ok(unsafe { std::os::fd::OwnedFd::from_raw_fd(cloned) })
}

/// Duplicate a listener inherited from the supervisor without closing its copy.
#[cfg(unix)]
pub fn inherited_listener(fd: i32) -> io::Result<TcpListener> {
    let listener = TcpListener::from(duplicate(fd)?);
    listener.local_addr()?;
    listener.set_nonblocking(true)?;
    Ok(listener)
}

#[cfg(not(unix))]
pub fn inherited_listener(_fd: i32) -> io::Result<TcpListener> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "inherited listeners currently require Unix",
    ))
}

/// Signal readiness only after application startup and native listener setup.
#[cfg(unix)]
pub fn signal_ready(fd: i32) -> io::Result<()> {
    use std::io::Write;
    std::fs::File::from(duplicate(fd)?).write_all(b"1")
}

#[cfg(not(unix))]
pub fn signal_ready(_fd: i32) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "readiness pipes currently require Unix",
    ))
}

/// Fail closed if the supervisor disappears, including while Python is blocked.
#[cfg(target_os = "linux")]
pub fn set_parent_death_signal(expected_parent: u32) -> io::Result<()> {
    if expected_parent == 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "expected parent must be positive",
        ));
    }
    // SAFETY: prctl takes integer arguments for PR_SET_PDEATHSIG and does not
    // read a pointer. SIGKILL is an OS signal number; the setting affects only
    // the calling worker process and is installed after re-exec, before app import.
    if unsafe { libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL, 0, 0, 0) } == -1 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: getppid takes no arguments and returns the current process parent.
    // Checking after prctl closes the race where the parent died just before it.
    if unsafe { libc::getppid() } as u32 != expected_parent {
        return Err(io::Error::new(
            io::ErrorKind::BrokenPipe,
            "supervisor exited before worker initialization",
        ));
    }
    Ok(())
}

#[cfg(not(target_os = "linux"))]
pub fn set_parent_death_signal(_expected_parent: u32) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "parent-death signaling currently requires Linux",
    ))
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::fd::AsRawFd;

    #[test]
    fn inherited_listener_has_independent_ownership() {
        let original = TcpListener::bind("127.0.0.1:0").unwrap();
        let duplicate = inherited_listener(original.as_raw_fd()).unwrap();
        assert_eq!(
            original.local_addr().unwrap(),
            duplicate.local_addr().unwrap()
        );
        drop(duplicate);
        assert!(original.local_addr().is_ok());
    }

    #[test]
    fn invalid_handles_are_errors() {
        assert!(inherited_listener(-1).is_err());
        assert!(inherited_listener(i32::MAX).is_err());
        let file = std::fs::File::open("/dev/null").unwrap();
        assert!(inherited_listener(file.as_raw_fd()).is_err());
    }
}
