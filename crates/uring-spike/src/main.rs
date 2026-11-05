//! Phase 5 spike: the same keep-alive HTTP/1 responder for GET /json on three
//! I/O backends, so their CPU cost per request can be compared directly.
//!
//!   uring-spike tokio|mio|uring PORT
//!
//! `tokio` is a current_thread runtime with a task per connection (the shape of
//! the server today), `mio` is a hand-rolled edge-triggered epoll loop, and
//! `uring` uses io_uring with multishot accept into the fixed file table,
//! multishot recv from a provided-buffer ring, and one io_uring_enter per loop
//! iteration for all sends. Set SPIKE_URING_PLAIN=1 to build the ring without
//! SINGLE_ISSUER/DEFER_TASKRUN, to see what those flags are worth.
//!
//! Everything runs on one thread; pin it with taskset.
//!
//!   uring-spike probe
//!
//! reports whether this process may use io_uring (seccomp, sysctl), and exits 0
//! if it can.

mod http;
mod mio_backend;
mod tokio_backend;
mod uring_backend;

use std::net::TcpListener;
use std::os::fd::FromRawFd;

/// A non-blocking listener on 127.0.0.1 with TCP_NODELAY (inherited by
/// accepted sockets on Linux) and a backlog large enough for 1024 clients
/// connecting at once.
fn listener(port: u16) -> TcpListener {
    unsafe {
        let fd = libc::socket(
            libc::AF_INET,
            libc::SOCK_STREAM | libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC,
            0,
        );
        assert!(fd >= 0, "socket: {}", std::io::Error::last_os_error());
        let one: libc::c_int = 1;
        for (level, opt) in [
            (libc::SOL_SOCKET, libc::SO_REUSEADDR),
            (libc::IPPROTO_TCP, libc::TCP_NODELAY),
        ] {
            libc::setsockopt(
                fd,
                level,
                opt,
                (&raw const one).cast(),
                size_of::<libc::c_int>() as u32,
            );
        }
        let addr = libc::sockaddr_in {
            sin_family: libc::AF_INET as u16,
            sin_port: port.to_be(),
            sin_addr: libc::in_addr {
                s_addr: u32::from_be_bytes([127, 0, 0, 1]).to_be(),
            },
            sin_zero: [0; 8],
        };
        let rc = libc::bind(
            fd,
            (&raw const addr).cast(),
            size_of::<libc::sockaddr_in>() as u32,
        );
        assert!(rc == 0, "bind: {}", std::io::Error::last_os_error());
        assert!(
            libc::listen(fd, 4096) == 0,
            "listen: {}",
            std::io::Error::last_os_error()
        );
        TcpListener::from_raw_fd(fd)
    }
}

/// Raise RLIMIT_NOFILE to the hard limit; registering a large fixed file table
/// is checked against it.
fn raise_nofile() -> u64 {
    unsafe {
        let mut lim: libc::rlimit = std::mem::zeroed();
        libc::getrlimit(libc::RLIMIT_NOFILE, &mut lim);
        lim.rlim_cur = lim.rlim_max;
        libc::setrlimit(libc::RLIMIT_NOFILE, &lim);
        lim.rlim_cur
    }
}

fn main() {
    let mut args = std::env::args().skip(1);
    let usage = "usage: uring-spike tokio|mio|uring PORT | probe";
    let mode = args.next().expect(usage);
    if mode == "probe" {
        std::process::exit(uring_backend::probe());
    }
    let port: u16 = args.next().expect(usage).parse().expect(usage);
    let nofile = raise_nofile();
    let listener = listener(port);
    eprintln!("uring-spike: {mode} on 127.0.0.1:{port}");
    let result = match mode.as_str() {
        "tokio" => tokio_backend::run(listener),
        "mio" => mio_backend::run(listener),
        "uring" => uring_backend::run(listener, nofile),
        _ => panic!("{usage}"),
    };
    if let Err(err) = result {
        eprintln!("uring-spike: {err}");
        std::process::exit(1);
    }
}
