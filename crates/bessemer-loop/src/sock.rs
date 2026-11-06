//! `sock_recv` and `sock_sendall` with the syscalls made from Rust.
//!
//! The Python wrappers in `bessemer_loop` stay coroutines (so
//! `create_task(loop.sock_recv(...))` still works) and call
//! `_bessemer_sock_recv` / `_bessemer_sock_sendall`, which try the operation
//! at once and only fall back to a future and a reader or writer when the
//! socket would block.

use std::cell::RefCell;
use std::os::fd::RawFd;

use pyo3::buffer::PyBuffer;
use pyo3::exceptions::{PyOSError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyTuple};
use pyo3::{PyTraverseError, PyVisit, intern};

use crate::core::LoopCore;

const STACK_BUFFER: usize = 256 * 1024;

thread_local! {
    static RECV_BUFFER: RefCell<Vec<u8>> = RefCell::new(vec![0; STACK_BUFFER]);
}

static SSL_SOCKET: PyOnceLock<Option<Py<PyAny>>> = PyOnceLock::new();

enum Io<T> {
    Done(T),
    WouldBlock,
}

enum SendData {
    Immutable(Py<PyBytes>),
    Snapshot(Vec<u8>),
}

impl SendData {
    fn from_python(py: Python<'_>, data: &Bound<'_, PyAny>) -> PyResult<Self> {
        if data.is_exact_instance_of::<PyBytes>() {
            return Ok(Self::Immutable(data.cast::<PyBytes>()?.clone().unbind()));
        }
        let buffer = PyBuffer::<u8>::get(data)?;
        if !buffer.is_c_contiguous() {
            return Err(PyValueError::new_err(
                "sock_sendall needs a contiguous buffer",
            ));
        }
        Ok(Self::Snapshot(buffer.to_vec(py)?))
    }

    fn bytes<'py>(&'py self, py: Python<'py>) -> &'py [u8] {
        match self {
            Self::Immutable(data) => data.bind(py).as_bytes(),
            Self::Snapshot(data) => data,
        }
    }
}

fn os_error(err: std::io::Error) -> PyErr {
    // OSError(errno, strerror) picks the matching subclass, as socket does.
    match err.raw_os_error() {
        Some(code) => {
            let message = std::io::Error::from_raw_os_error(code).to_string();
            let message = message.split(" (os error").next().unwrap_or("").to_string();
            PyOSError::new_err((code, message))
        }
        None => PyOSError::new_err(err.to_string()),
    }
}

fn recv_once(py: Python<'_>, fd: RawFd, n: usize) -> PyResult<Io<Py<PyBytes>>> {
    let read = |buf: &mut [u8]| -> PyResult<Io<Py<PyBytes>>> {
        loop {
            let rc = unsafe { libc::recv(fd, buf.as_mut_ptr().cast(), n, 0) };
            if rc >= 0 {
                return Ok(Io::Done(PyBytes::new(py, &buf[..rc as usize]).unbind()));
            }
            let err = std::io::Error::last_os_error();
            match err.raw_os_error() {
                Some(libc::EINTR) => {
                    // As CPython's socket module: run signal handlers, retry.
                    py.check_signals()?;
                }
                Some(libc::EAGAIN) => return Ok(Io::WouldBlock),
                _ => return Err(os_error(err)),
            }
        }
    };
    if n <= STACK_BUFFER {
        RECV_BUFFER.with(|cell| match cell.try_borrow_mut() {
            Ok(mut buf) => read(&mut buf[..n]),
            // Re-entered from a signal handler mid-read: use a fresh buffer.
            Err(_) => read(&mut vec![0; n]),
        })
    } else {
        read(&mut vec![0; n])
    }
}

fn send_once(py: Python<'_>, fd: RawFd, data: &[u8]) -> PyResult<Io<usize>> {
    loop {
        let rc = unsafe { libc::send(fd, data.as_ptr().cast(), data.len(), 0) };
        if rc >= 0 {
            return Ok(Io::Done(rc as usize));
        }
        let err = std::io::Error::last_os_error();
        match err.raw_os_error() {
            Some(libc::EINTR) => py.check_signals()?,
            Some(libc::EAGAIN) => return Ok(Io::WouldBlock),
            _ => return Err(os_error(err)),
        }
    }
}

fn check_socket(slf: &Bound<'_, LoopCore>, sock: &Bound<'_, PyAny>) -> PyResult<RawFd> {
    let py = slf.py();
    let ssl_socket = SSL_SOCKET.get_or_init(py, || {
        py.import("ssl")
            .and_then(|m| m.getattr("SSLSocket"))
            .ok()
            .map(Bound::unbind)
    });
    if let Some(ssl_socket) = ssl_socket
        && sock.is_instance(ssl_socket.bind(py))?
    {
        return Err(PyTypeError::new_err("Socket cannot be of type SSLSocket"));
    }
    if slf.get().get_debug_flag() {
        let timeout = sock.call_method0(intern!(py, "gettimeout"))?;
        if timeout.is_none() || timeout.extract::<f64>()? != 0.0 {
            return Err(PyValueError::new_err("the socket must be non-blocking"));
        }
    }
    let fd: i64 = sock.call_method0(intern!(py, "fileno"))?.extract()?;
    if fd < 0 {
        // A closed socket: let the socket module raise its usual error.
        sock.call_method1(intern!(py, "recv"), (0,))?;
        return Err(PyOSError::new_err((libc::EBADF, "Bad file descriptor")));
    }
    Ok(fd as RawFd)
}

/// Pending recv: the reader callback.
#[pyclass(frozen, module = "bessemer._native")]
pub struct SockRecv {
    future: Py<PyAny>,
    fd: RawFd,
    n: usize,
}

#[pymethods]
impl SockRecv {
    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        let future = self.future.bind(py);
        if future.call_method0(intern!(py, "done"))?.is_truthy()? {
            return Ok(());
        }
        match recv_once(py, self.fd, self.n) {
            Ok(Io::Done(data)) => {
                future.call_method1(intern!(py, "set_result"), (data,))?;
            }
            Ok(Io::WouldBlock) => {}
            Err(err) => {
                future.call_method1(intern!(py, "set_exception"), (err.value(py),))?;
            }
        }
        Ok(())
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.future)
    }
}

/// Pending sendall: the writer callback. Keeps the buffer it sends from.
#[pyclass(module = "bessemer._native")]
pub struct SockSendall {
    future: Py<PyAny>,
    data: Option<SendData>,
    fd: RawFd,
    pos: usize,
}

#[pymethods]
impl SockSendall {
    fn __call__(&mut self, py: Python<'_>) -> PyResult<()> {
        let future = self.future.bind(py).clone();
        if future.call_method0(intern!(py, "done"))?.is_truthy()? {
            return Ok(());
        }
        let Some(data) = self.data.as_ref() else {
            return Ok(());
        };
        let bytes = data.bytes(py);
        match send_once(py, self.fd, &bytes[self.pos..]) {
            Ok(Io::Done(sent)) => {
                self.pos += sent;
                if self.pos == bytes.len() {
                    self.data = None;
                    future.call_method1(intern!(py, "set_result"), (py.None(),))?;
                }
            }
            Ok(Io::WouldBlock) => {}
            Err(err) => {
                self.data = None;
                future.call_method1(intern!(py, "set_exception"), (err.value(py),))?;
            }
        }
        Ok(())
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.future)?;
        if let Some(SendData::Immutable(data)) = &self.data {
            visit.call(data)?;
        }
        Ok(())
    }
}

/// Done callback: unregister the reader or writer once the future finishes,
/// unless the handle was already cancelled (replaced or removed).
#[pyclass(frozen, module = "bessemer._native")]
pub struct SockDone {
    owner: Py<LoopCore>,
    handle: Py<crate::handle::Handle>,
    fd: RawFd,
    write: bool,
}

#[pymethods]
impl SockDone {
    fn __call__(&self, py: Python<'_>, _future: Bound<'_, PyAny>) -> PyResult<()> {
        if self.handle.get().is_cancelled() {
            return Ok(());
        }
        LoopCore::remove_io(self.owner.bind(py), self.fd, self.write)?;
        Ok(())
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.owner)?;
        visit.call(&self.handle)
    }
}

/// Bytes when the data was there, or a pending future.
pub(crate) fn sock_recv(
    slf: &Bound<'_, LoopCore>,
    sock: Bound<'_, PyAny>,
    n: isize,
) -> PyResult<Py<PyAny>> {
    let py = slf.py();
    let fd = check_socket(slf, &sock)?;
    if n < 0 {
        return Err(PyValueError::new_err("negative buffersize in recv"));
    }
    let n = n as usize;
    if n == 0 {
        // socket.recv(0) returns b"" at once; the syscall would say EAGAIN.
        return Ok(PyBytes::new(py, b"").into_any().unbind());
    }
    if let Io::Done(data) = recv_once(py, fd, n)? {
        return Ok(data.into_any());
    }
    let future = slf.call_method0(intern!(py, "create_future"))?;
    slf.call_method1(intern!(py, "_ensure_fd_no_transport"), (fd,))?;
    let callback = Bound::new(
        py,
        SockRecv {
            future: future.clone().unbind(),
            fd,
            n,
        },
    )?;
    let handle = LoopCore::add_io(slf, fd, false, callback.into_any(), PyTuple::empty(py))?;
    let done = Bound::new(
        py,
        SockDone {
            owner: slf.clone().unbind(),
            handle,
            fd,
            write: false,
        },
    )?;
    future.call_method1(intern!(py, "add_done_callback"), (done,))?;
    Ok(future.unbind())
}

/// None when everything was sent at once, or a pending future.
pub(crate) fn sock_sendall(
    slf: &Bound<'_, LoopCore>,
    sock: Bound<'_, PyAny>,
    data: Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let py = slf.py();
    let fd = check_socket(slf, &sock)?;
    let data = SendData::from_python(py, &data)?;
    let bytes = data.bytes(py);
    let total = bytes.len();
    let sent = match send_once(py, fd, bytes)? {
        Io::Done(sent) => sent,
        Io::WouldBlock => 0,
    };
    if sent == total {
        return Ok(py.None());
    }
    let future = slf.call_method0(intern!(py, "create_future"))?;
    slf.call_method1(intern!(py, "_ensure_fd_no_transport"), (fd,))?;
    let callback = Bound::new(
        py,
        SockSendall {
            future: future.clone().unbind(),
            data: Some(data),
            fd,
            pos: sent,
        },
    )?;
    let handle = LoopCore::add_io(slf, fd, true, callback.into_any(), PyTuple::empty(py))?;
    let done = Bound::new(
        py,
        SockDone {
            owner: slf.clone().unbind(),
            handle,
            fd,
            write: true,
        },
    )?;
    future.call_method1(intern!(py, "add_done_callback"), (done,))?;
    Ok(future.unbind())
}
