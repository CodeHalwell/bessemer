//! The small, audited operating-system ownership boundary for inherited handles.
#![deny(unsafe_op_in_unsafe_fn)]

use std::{io, net::TcpListener};

/// Inherited handles arrive from Python as integers: file descriptors on
/// POSIX, and SOCKET or HANDLE values on Windows.
pub type RawHandle = i64;

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
fn cpu_clock(clock: libc::clockid_t) -> Option<std::time::Duration> {
    let mut value = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: clock_gettime writes to the provided timespec; failure is checked.
    if unsafe { libc::clock_gettime(clock, &mut value) } != 0 {
        return None;
    }
    let seconds = u64::try_from(value.tv_sec).ok()?;
    let nanos = u32::try_from(value.tv_nsec).ok()?;
    (nanos < 1_000_000_000).then(|| std::time::Duration::new(seconds, nanos))
}

/// Checked process CPU time for adaptive worker growth and admission.
#[cfg(any(target_os = "linux", target_os = "macos"))]
pub fn process_cpu_time() -> Option<std::time::Duration> {
    cpu_clock(libc::CLOCK_PROCESS_CPUTIME_ID)
}

/// Checked CPU time of the calling thread. Adaptive admission reads it on an
/// event loop's thread to tell work that thread did from time it waited.
#[cfg(any(target_os = "linux", target_os = "macos"))]
pub fn thread_cpu_time() -> Option<std::time::Duration> {
    cpu_clock(libc::CLOCK_THREAD_CPUTIME_ID)
}

#[cfg(unix)]
fn duplicate(fd: RawHandle) -> io::Result<std::os::fd::OwnedFd> {
    use std::os::fd::FromRawFd;
    let fd = i32::try_from(fd).map_err(|_| invalid("inherited descriptor out of range"))?;
    if fd < 0 {
        return Err(invalid("negative inherited descriptor"));
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

/// A distinct, non-inheritable duplicate of a socket the caller keeps owning.
#[cfg(windows)]
fn duplicate_socket(raw: RawHandle) -> io::Result<std::os::windows::io::OwnedSocket> {
    use std::os::windows::io::{BorrowedSocket, RawSocket};
    if raw < 0 {
        return Err(invalid("negative inherited socket"));
    }
    // SAFETY: the borrow lasts only for this call, while the caller keeps the
    // socket open, and is never INVALID_SOCKET (all ones), which is negative
    // here. WSADuplicateSocketW validates the value and fails for anything
    // that is not a socket; the original is neither closed nor taken.
    let borrowed = unsafe { BorrowedSocket::borrow_raw(raw as RawSocket) };
    borrowed.try_clone_to_owned()
}

/// Cancel every operation pending on a socket, so an accept blocked inside
/// Winsock returns as interrupted. Nothing pending is not an error.
///
/// The caller keeps the socket open across the call: a closed value is only
/// an error here, but a reused one would cancel another handle's I/O.
#[cfg(windows)]
pub fn cancel_pending_io(socket: std::os::windows::io::RawSocket) {
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn CancelIoEx(file: *mut std::ffi::c_void, overlapped: *mut std::ffi::c_void) -> i32;
    }
    // SAFETY: CancelIoEx validates the handle in the kernel and reads no
    // memory through either argument; a null OVERLAPPED means all I/O.
    unsafe {
        CancelIoEx(
            socket as usize as *mut std::ffi::c_void,
            std::ptr::null_mut(),
        );
    }
}

/// A distinct duplicate of a pipe or file handle the caller keeps owning.
#[cfg(windows)]
fn duplicate_handle(raw: RawHandle) -> io::Result<std::os::windows::io::OwnedHandle> {
    use std::os::windows::io::{BorrowedHandle, RawHandle as WindowsHandle};
    if raw <= 0 {
        return Err(invalid("invalid inherited handle"));
    }
    // SAFETY: the borrow lasts only for this call, while the caller keeps the
    // handle open. DuplicateHandle validates it and fails for a closed or
    // foreign value; the original is neither closed nor taken.
    let borrowed = unsafe { BorrowedHandle::borrow_raw(raw as isize as WindowsHandle) };
    borrowed.try_clone_to_owned()
}

/// Duplicate a listener inherited from the supervisor without closing its copy.
pub fn inherited_listener(fd: RawHandle) -> io::Result<TcpListener> {
    #[cfg(unix)]
    let listener = TcpListener::from(duplicate(fd)?);
    #[cfg(windows)]
    let listener = TcpListener::from(duplicate_socket(fd)?);
    listener.local_addr()?;
    listener.set_nonblocking(true)?;
    Ok(listener)
}

/// Close a descriptor this process inherited once its duplicate is in use.
///
/// For a supervised worker's copy of the listener, which nothing else in the
/// worker owns: Python received it as a bare integer and only passes it on.
/// On Windows the inherited SOCKET is left to process exit.
pub fn close_inherited(fd: RawHandle) -> io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::fd::FromRawFd;
        let fd = i32::try_from(fd).map_err(|_| invalid("inherited descriptor out of range"))?;
        // The standard streams are never a listener handed down by the
        // supervisor; refusing them keeps a wrong number from closing one.
        if fd <= 2 {
            return Err(invalid("not an inherited listener descriptor"));
        }
        // SAFETY: the caller states that `fd` is an open descriptor inherited
        // from the supervisor that no other object in this process owns, so
        // exactly one owner is constructed here and dropped to close it.
        drop(unsafe { std::os::fd::OwnedFd::from_raw_fd(fd) });
    }
    #[cfg(not(unix))]
    let _ = fd;
    Ok(())
}

/// A duplicate of an inherited stream socket (a Unix socket on POSIX, a
/// loopback TCP socket from `socket.socketpair` on Windows), nonblocking for
/// Tokio.
#[cfg(unix)]
pub fn inherited_stream(fd: RawHandle) -> io::Result<std::os::unix::net::UnixStream> {
    let stream = std::os::unix::net::UnixStream::from(duplicate(fd)?);
    stream.set_nonblocking(true)?;
    Ok(stream)
}

#[cfg(windows)]
pub fn inherited_stream(fd: RawHandle) -> io::Result<std::net::TcpStream> {
    let stream = std::net::TcpStream::from(duplicate_socket(fd)?);
    stream.set_nonblocking(true)?;
    Ok(stream)
}

fn notify(fd: RawHandle, message: &[u8]) -> io::Result<()> {
    use std::io::Write;
    #[cfg(unix)]
    let file = std::fs::File::from(duplicate(fd)?);
    #[cfg(windows)]
    let file = std::fs::File::from(duplicate_handle(fd)?);
    (&file).write_all(message)
}

/// Whether a failed `accept` means the listening socket itself cannot be used
/// again. Every other failure belongs to the process or the machine (no free
/// descriptors or buffers) or to one connection that ended before it was
/// accepted, so the caller should wait and accept again rather than stop
/// serving the connections it already has.
pub fn accept_failure_is_fatal(error: &io::Error) -> bool {
    let Some(code) = error.raw_os_error() else {
        // Not an operating-system failure: the listener's own driver gave up.
        return true;
    };
    #[cfg(unix)]
    {
        matches!(
            code,
            libc::EBADF | libc::EINVAL | libc::ENOTSOCK | libc::EFAULT | libc::EOPNOTSUPP
        )
    }
    #[cfg(windows)]
    {
        // WSAEBADF, WSAEFAULT, WSAEINVAL, WSAENOTSOCK, WSAEOPNOTSUPP.
        matches!(code, 10009 | 10014 | 10022 | 10038 | 10045)
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = code;
        true
    }
}

/// Signal readiness only after application startup and native listener setup.
pub fn signal_ready(fd: RawHandle) -> io::Result<()> {
    notify(fd, b"1")
}

/// Announce native drain before connection and Python cleanup can block.
pub fn signal_draining(fd: RawHandle) -> io::Result<()> {
    notify(fd, b"d")
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

    #[cfg(any(target_os = "linux", target_os = "macos"))]
    #[test]
    fn process_cpu_clock_is_available_and_monotonic() {
        let before = process_cpu_time().expect("process CPU clock unavailable");
        let after = process_cpu_time().expect("process CPU clock stopped");
        assert!(after >= before);
    }

    #[cfg(any(target_os = "linux", target_os = "macos"))]
    #[test]
    fn thread_cpu_clock_counts_this_threads_work_and_not_its_sleep() {
        let before = thread_cpu_time().expect("thread CPU clock unavailable");
        std::thread::sleep(std::time::Duration::from_millis(30));
        let slept = thread_cpu_time().unwrap() - before;
        assert!(slept < std::time::Duration::from_millis(20), "{slept:?}");
        let started = std::time::Instant::now();
        let mut total = 0u64;
        while started.elapsed() < std::time::Duration::from_millis(30) {
            total = std::hint::black_box(total.wrapping_mul(31).wrapping_add(7));
        }
        let worked = thread_cpu_time().unwrap() - before - slept;
        assert!(worked > std::time::Duration::from_millis(5), "{worked:?}");
        // Another thread's work is the process's, not this thread's.
        let mine = thread_cpu_time().unwrap();
        let process = process_cpu_time().unwrap();
        std::thread::spawn(|| {
            let started = std::time::Instant::now();
            let mut total = 0u64;
            while started.elapsed() < std::time::Duration::from_millis(30) {
                total = std::hint::black_box(total.wrapping_mul(31).wrapping_add(7));
            }
        })
        .join()
        .unwrap();
        assert!(thread_cpu_time().unwrap() - mine < std::time::Duration::from_millis(20));
        assert!(process_cpu_time().unwrap() - process > std::time::Duration::from_millis(5));
    }

    #[test]
    fn inherited_listener_has_independent_ownership() {
        let original = TcpListener::bind("127.0.0.1:0").unwrap();
        let duplicate = inherited_listener(original.as_raw_fd().into()).unwrap();
        assert_eq!(
            original.local_addr().unwrap(),
            duplicate.local_addr().unwrap()
        );
        drop(duplicate);
        assert!(original.local_addr().is_ok());
    }

    #[test]
    fn only_a_broken_listener_makes_an_accept_failure_fatal() {
        for transient in [
            libc::EMFILE,
            libc::ENFILE,
            libc::ENOBUFS,
            libc::ENOMEM,
            libc::ECONNRESET,
            libc::EPROTO,
            libc::ENETDOWN,
            libc::EHOSTUNREACH,
            libc::ETIMEDOUT,
            libc::EPERM,
        ] {
            assert!(
                !accept_failure_is_fatal(&io::Error::from_raw_os_error(transient)),
                "errno {transient} must be retried"
            );
        }
        for fatal in [libc::EBADF, libc::EINVAL, libc::ENOTSOCK, libc::EFAULT] {
            assert!(
                accept_failure_is_fatal(&io::Error::from_raw_os_error(fatal)),
                "errno {fatal} means the listener is unusable"
            );
        }
        assert!(accept_failure_is_fatal(&io::Error::other("driver stopped")));
    }

    #[test]
    fn closing_the_inherited_copy_leaves_the_duplicate_listening() {
        use std::os::fd::IntoRawFd;
        let original = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = original.local_addr().unwrap();
        // The worker holds the supervisor's listener as a bare descriptor.
        let inherited = original.into_raw_fd();
        let duplicate = inherited_listener(inherited.into()).unwrap();
        close_inherited(inherited.into()).unwrap();
        assert_eq!(duplicate.local_addr().unwrap(), address);
        assert!(std::net::TcpStream::connect(address).is_ok());
        // With the last copy gone, nothing listens any more.
        drop(duplicate);
        assert!(std::net::TcpStream::connect(address).is_err());
        assert!(close_inherited(0).is_err());
        assert!(close_inherited(-1).is_err());
        assert!(close_inherited(i64::MAX).is_err());
    }

    #[test]
    fn invalid_handles_are_errors() {
        assert!(inherited_listener(-1).is_err());
        assert!(inherited_listener(i32::MAX.into()).is_err());
        assert!(inherited_listener(i64::MAX).is_err());
        let file = std::fs::File::open("/dev/null").unwrap();
        assert!(inherited_listener(file.as_raw_fd().into()).is_err());
    }

    #[test]
    fn control_notifications_preserve_the_original_descriptor() {
        use std::io::{Read, Write};
        use std::os::unix::net::UnixStream;
        use std::time::Duration;

        let (mut reader, mut writer) = UnixStream::pair().unwrap();
        reader
            .set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap();
        signal_ready(writer.as_raw_fd().into()).unwrap();
        signal_draining(writer.as_raw_fd().into()).unwrap();
        let mut notifications = [0; 2];
        reader.read_exact(&mut notifications).unwrap();
        assert_eq!(&notifications, b"1d");
        writer.write_all(b"h").unwrap();
        reader.read_exact(&mut notifications[..1]).unwrap();
        assert_eq!(notifications[0], b'h');
        assert!(signal_draining(-1).is_err());
        assert!(signal_draining(i32::MAX.into()).is_err());
    }
}

/// Keep one CPython thread state alive for the lifetime of a native thread.
///
/// A thread that CPython did not create gets a fresh thread state, including a
/// newly mapped evaluation stack, from every outermost GIL acquisition, and
/// loses it again on release. Tokio worker threads enter Python once per
/// request, so that churn costs several percent of a request. Pinning holds one
/// PyGILState reference without holding the GIL; later acquisitions on this
/// thread reuse the state. Call `pin_python_thread_state` when a native thread
/// starts and `unpin_python_thread_state` before it exits, while the
/// interpreter that started it is still running.
pub mod python_thread {
    use pyo3_ffi as ffi;
    use std::cell::Cell;

    thread_local! {
        static PINNED: Cell<Option<(ffi::PyGILState_STATE, *mut ffi::PyThreadState)>> =
            const { Cell::new(None) };
    }

    pub fn pin_python_thread_state() {
        if PINNED.with(Cell::get).is_some() {
            return;
        }
        // SAFETY: only used by threads started while the interpreter is running
        // (inside the extension's serve call). PyGILState_Ensure creates or
        // reuses this thread's state and acquires the GIL; PyEval_SaveThread then
        // releases the GIL while leaving the PyGILState reference held.
        let pinned = unsafe {
            if ffi::Py_IsInitialized() == 0 {
                return;
            }
            let state = ffi::PyGILState_Ensure();
            (state, ffi::PyEval_SaveThread())
        };
        PINNED.with(|cell| cell.set(Some(pinned)));
    }

    pub fn unpin_python_thread_state() {
        let Some((state, thread_state)) = PINNED.with(Cell::take) else {
            return;
        };
        // SAFETY: the pair came from pin_python_thread_state on this same
        // thread and the GIL is not held here. Restoring reacquires the GIL for
        // the saved state; releasing the PyGILState reference then deletes it.
        // If the interpreter has already gone, the state is leaked instead.
        unsafe {
            if ffi::Py_IsInitialized() == 0 {
                return;
            }
            ffi::PyEval_RestoreThread(thread_state);
            ffi::PyGILState_Release(state);
        }
    }
}
