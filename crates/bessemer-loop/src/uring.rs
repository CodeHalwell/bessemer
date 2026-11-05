//! io_uring socket I/O for the HTTP server on the loop's embedded runtime.
//!
//! An opt-in replacement for Tokio's epoll-driven `TcpListener` and
//! `TcpStream` on the single-thread worker (`io_backend="io_uring"`): one ring
//! per loop thread, multishot `accept` on the listener, multishot `recv` into a
//! provided-buffer ring, and the sends queued during a pass of the runtime's
//! tasks submitted together in one `io_uring_enter`.
//!
//! The ring is built with `SINGLE_ISSUER | DEFER_TASKRUN` (Linux 6.1+):
//! completions are run in batches when this thread asks for them instead of
//! as task work interrupting it. The Phase 5 spike measured that without
//! those flags io_uring is slower than epoll at light load, so a kernel
//! without them is treated as having no io_uring at all.
//!
//! A pump task on the runtime owns the submit/complete cycle. Streams queue
//! work on the ring and wake the pump; the pump submits it and runs
//! completions with one `io_uring_enter`, then sleeps on an eventfd that the
//! kernel signals when new completion work arrives. (With `DEFER_TASKRUN` the
//! ring descriptor itself is not reliably reported readable to epoll, so the
//! eventfd is what the runtime's reactor watches.) The eventfd is disabled
//! during the pump's own `io_uring_enter`, and `IORING_SQ_TASKRUN` is checked
//! after re-enabling it, so its own completions do not wake it again and no
//! completion arriving in between is missed.
//!
//! Everything runs on the loop's thread. The ring lives in a thread local;
//! [`Stream`] and [`Listener`] only hold a slot index and generation, so they
//! are `Send` as Tokio's spawn requires, and report an error if used from
//! another thread.
//!
//! [`Listener::new`] fails when io_uring is unavailable: blocked by a seccomp
//! profile (Docker's and containerd's defaults return `EPERM`), disabled by
//! `kernel.io_uring_disabled`, or a kernel without `DEFER_TASKRUN`, multishot
//! accept/recv or buffer rings. The caller then keeps serving on epoll.

use std::cell::RefCell;
use std::io;
use std::mem::ManuallyDrop;
use std::net::SocketAddr;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU16, Ordering};
use std::task::{Context, Poll, Waker};

use io_uring::{IoUring, cqueue, opcode, squeue, types};
use tokio::io::unix::AsyncFd;
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};

/// Provided-buffer group id.
const BGID: u16 = 0;
/// Provided buffers (a power of two). Each is returned to the ring as soon as
/// its bytes are copied out, so this only has to cover one batch of
/// completions.
const BUF_COUNT: u16 = 4096;
const BUF_SIZE: usize = 2048;
const SQ_ENTRIES: u32 = 4096;
const CQ_ENTRIES: u32 = 16384;
/// Unread bytes buffered for a connection before its recv is paused until the
/// reader catches up.
const MAX_INPUT: usize = 64 * 1024;
/// Unsent bytes buffered for a connection before writes wait.
const MAX_OUTPUT: usize = 256 * 1024;
/// Accepted connections queued for the server before multishot accept is
/// stopped until it takes some, so a burst cannot run the process out of
/// descriptors ahead of the server's connection limit.
const MAX_ACCEPTED: usize = 256;
/// How long a dropped stream keeps sending what was written to it before the
/// rest is discarded and the connection closed.
const LINGER: std::time::Duration = std::time::Duration::from_secs(30);
/// Submit/complete rounds per pump turn before yielding to other tasks.
const MAX_ROUNDS: usize = 8;

const OP_ACCEPT: u64 = 1;
const OP_RECV: u64 = 2;
const OP_SEND: u64 = 3;
const OP_CANCEL: u64 = 4;
const GEN_MASK: u32 = 0xff_ffff;
/// `io_uring_enter` flag: run completion work and post completions.
const IORING_ENTER_GETEVENTS: u32 = 1;

/// user_data = op (8 bits) | generation (24 bits) | slot (32 bits). The
/// generation drops stale completions for a slot that has been reused.
fn tag(op: u64, generation: u32, slot: u32) -> u64 {
    (op << 56) | (((generation & GEN_MASK) as u64) << 32) | slot as u64
}

fn untag(user_data: u64) -> (u64, u32, u32) {
    (
        user_data >> 56,
        ((user_data >> 32) as u32) & GEN_MASK,
        user_data as u32,
    )
}

thread_local! {
    static RING: RefCell<Option<Box<Ring>>> = const { RefCell::new(None) };
}

/// Run `f` on this thread's ring.
fn with<R>(f: impl FnOnce(&mut Ring) -> R) -> io::Result<R> {
    RING.with(|cell| {
        let mut slot = cell.borrow_mut();
        match slot.as_deref_mut() {
            Some(ring) => Ok(f(ring)),
            None => Err(io::Error::other(
                "io_uring stream used off its event loop's thread",
            )),
        }
    })
}

/// The provided-buffer ring: the kernel picks a buffer per recv completion
/// and the pump hands it back once the bytes are copied out.
struct BufRing {
    entries: *mut types::BufRingEntry,
    entries_len: usize,
    /// BUF_COUNT * BUF_SIZE bytes the kernel writes into. Never freed while
    /// the ring may still select from it (see `Ring::drop`).
    bufs: *mut u8,
    tail: u16,
}

impl BufRing {
    fn new(ring: &IoUring) -> io::Result<Self> {
        let entries_len = BUF_COUNT as usize * size_of::<types::BufRingEntry>();
        // SAFETY: an anonymous mapping, page aligned as the kernel requires.
        let entries = unsafe {
            let p = libc::mmap(
                std::ptr::null_mut(),
                entries_len,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_PRIVATE | libc::MAP_ANONYMOUS | libc::MAP_POPULATE,
                -1,
                0,
            );
            if p == libc::MAP_FAILED {
                return Err(io::Error::last_os_error());
            }
            p.cast::<types::BufRingEntry>()
        };
        let bufs =
            Box::into_raw(vec![0u8; BUF_COUNT as usize * BUF_SIZE].into_boxed_slice()).cast::<u8>();
        let mut this = BufRing {
            entries,
            entries_len,
            bufs,
            tail: 0,
        };
        for bid in 0..BUF_COUNT {
            this.push(bid);
        }
        this.publish();
        // SAFETY: the entries and buffers outlive the registration: the
        // buffer ring is unregistered before they are released.
        if let Err(error) = unsafe {
            ring.submitter()
                .register_buf_ring_with_flags(entries as u64, BUF_COUNT, BGID, 0)
        } {
            this.release();
            return Err(error);
        }
        Ok(this)
    }

    /// The first `len` bytes of buffer `bid`, filled by a completed recv.
    fn get(&self, bid: u16, len: usize) -> &[u8] {
        let len = len.min(BUF_SIZE);
        // SAFETY: bid < BUF_COUNT and the kernel has finished writing it.
        unsafe { std::slice::from_raw_parts(self.bufs.add(bid as usize * BUF_SIZE), len) }
    }

    /// Hand buffer `bid` back to the kernel (visible after `publish`).
    fn push(&mut self, bid: u16) {
        let idx = (self.tail & (BUF_COUNT - 1)) as usize;
        // SAFETY: idx < BUF_COUNT, inside the mapping.
        let entry = unsafe { &mut *self.entries.add(idx) };
        entry.set_addr(self.bufs as u64 + bid as u64 * BUF_SIZE as u64);
        entry.set_len(BUF_SIZE as u32);
        entry.set_bid(bid);
        self.tail = self.tail.wrapping_add(1);
    }

    fn publish(&self) {
        // SAFETY: the tail field overlays the first entry's reserved u16.
        let tail = unsafe { &*(types::BufRingEntry::tail(self.entries) as *const AtomicU16) };
        tail.store(self.tail, Ordering::Release);
    }

    /// Free the memory. Only once the kernel can no longer write to it.
    fn release(&mut self) {
        unsafe {
            libc::munmap(self.entries.cast(), self.entries_len);
            drop(Box::from_raw(std::ptr::slice_from_raw_parts_mut(
                self.bufs,
                BUF_COUNT as usize * BUF_SIZE,
            )));
        }
    }
}

#[derive(Default)]
struct Conn {
    generation: u32,
    fd: RawFd,
    open: bool,
    /// Queued for `flush_dirty`.
    dirty: bool,
    /// The stream was dropped: send what it wrote (up to `LINGER`), stop
    /// receiving, then close.
    dropped: bool,
    /// Lingering ended: discard unsent output and close.
    abandon: bool,

    /// Received bytes not yet read; `start` is the read offset.
    input: Vec<u8>,
    start: usize,
    recv_armed: bool,
    recv_cancelled: bool,
    /// Too much unread input: the recv was stopped until the reader drains it.
    paused: bool,
    eof: bool,
    read_error: Option<i32>,
    read_waker: Option<Waker>,

    /// The buffer of the send in flight (or about to be), and bytes sent.
    out: Vec<u8>,
    sent: usize,
    /// Bytes written while `out` is in flight.
    queued: Vec<u8>,
    sending: bool,
    send_cancelled: bool,
    write_error: Option<i32>,
    write_waker: Option<Waker>,
}

impl Conn {
    fn unread(&self) -> usize {
        self.input.len() - self.start
    }

    fn unsent(&self) -> usize {
        self.out.len() - self.sent + self.queued.len()
    }
}

#[derive(Default)]
struct ListenerState {
    generation: u32,
    fd: RawFd,
    open: bool,
    armed: bool,
    cancelled: bool,
    accepted: std::collections::VecDeque<RawFd>,
    error: Option<i32>,
    waker: Option<Waker>,
}

struct Ring {
    ring: IoUring,
    bufs: BufRing,
    /// Registered with the ring; the pump waits on a duplicate.
    eventfd: OwnedFd,
    conns: Vec<Conn>,
    free: Vec<u32>,
    dirty: Vec<u32>,
    listener: ListenerState,
    /// Dropped streams still sending: slot, generation and deadline.
    lingering: Vec<(u32, u32, tokio::time::Instant)>,
    /// Wakers collected while the ring is borrowed, woken after.
    wake: Vec<Waker>,
    /// Work is queued for the pump.
    kicked: bool,
    pump_waker: Option<Waker>,
    /// Cleared when the pump task ends (or is dropped with its runtime).
    pump_alive: Arc<AtomicBool>,
}

impl Ring {
    fn new() -> io::Result<Box<Self>> {
        let ring = IoUring::<squeue::Entry, cqueue::Entry>::builder()
            .setup_single_issuer()
            .setup_defer_taskrun()
            .setup_taskrun_flag()
            .setup_cqsize(CQ_ENTRIES)
            .build(SQ_ENTRIES)
            .map_err(|error| context(error, "io_uring_setup with SINGLE_ISSUER | DEFER_TASKRUN"))?;
        let mut probe = io_uring::Probe::new();
        ring.submitter()
            .register_probe(&mut probe)
            .map_err(|error| context(error, "io_uring_register"))?;
        for (name, code) in [
            ("multishot accept", opcode::AcceptMulti::CODE),
            ("multishot recv", opcode::RecvMulti::CODE),
            ("send", opcode::Send::CODE),
            ("cancel", opcode::AsyncCancel::CODE),
        ] {
            if !probe.is_supported(code) {
                return Err(io::Error::new(
                    io::ErrorKind::Unsupported,
                    format!("the kernel's io_uring has no {name}"),
                ));
            }
        }
        // Multishot recv (6.0) and DEFER_TASKRUN (6.1) imply buffer rings
        // (5.19), but a failure here is still a reason to fall back.
        let bufs = BufRing::new(&ring).map_err(|error| context(error, "provided-buffer ring"))?;
        let eventfd = unsafe { libc::eventfd(0, libc::EFD_NONBLOCK | libc::EFD_CLOEXEC) };
        if eventfd < 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: a new descriptor nothing else owns.
        let eventfd = unsafe { OwnedFd::from_raw_fd(eventfd) };
        ring.submitter()
            .register_eventfd(eventfd.as_raw_fd())
            .map_err(|error| context(error, "io_uring eventfd"))?;
        Ok(Box::new(Ring {
            ring,
            bufs,
            eventfd,
            conns: Vec::new(),
            free: Vec::new(),
            dirty: Vec::new(),
            lingering: Vec::new(),
            listener: ListenerState {
                fd: -1,
                ..Default::default()
            },
            wake: Vec::new(),
            kicked: false,
            pump_waker: None,
            pump_alive: Arc::new(AtomicBool::new(false)),
        }))
    }

    fn push(&mut self, entry: squeue::Entry) {
        loop {
            // SAFETY: every buffer an entry refers to outlives its operation:
            // send buffers stay in their Conn until the send completes, and a
            // slot is only reused once nothing is in flight on it.
            if unsafe { self.ring.submission().push(&entry) }.is_ok() {
                return;
            }
            // Full: submit what is there without waiting.
            let _ = self.ring.submit();
        }
    }

    fn kick(&mut self) {
        if !self.kicked {
            self.kicked = true;
            if let Some(waker) = self.pump_waker.take() {
                waker.wake();
            }
        }
    }

    fn mark_dirty(&mut self, slot: u32) {
        let conn = &mut self.conns[slot as usize];
        if !conn.dirty {
            conn.dirty = true;
            self.dirty.push(slot);
        }
        self.kick();
    }

    fn conn(&mut self, slot: u32, generation: u32) -> io::Result<&mut Conn> {
        match self.conns.get_mut(slot as usize) {
            Some(conn) if conn.open && conn.generation == generation => Ok(conn),
            _ => Err(io::Error::from(io::ErrorKind::NotConnected)),
        }
    }

    fn open_conn(&mut self, fd: RawFd) -> (u32, u32) {
        let slot = match self.free.pop() {
            Some(slot) => slot,
            None => {
                self.conns.push(Conn::default());
                (self.conns.len() - 1) as u32
            }
        };
        let conn = &mut self.conns[slot as usize];
        let generation = conn.generation;
        let input = std::mem::take(&mut conn.input);
        let out = std::mem::take(&mut conn.out);
        let queued = std::mem::take(&mut conn.queued);
        *conn = Conn {
            generation,
            fd,
            open: true,
            input,
            out,
            queued,
            ..Default::default()
        };
        self.mark_dirty(slot);
        (slot, generation)
    }

    /// Close a dropped connection once nothing is in flight on it.
    fn try_finalize(&mut self, slot: u32) {
        let conn = &mut self.conns[slot as usize];
        if !conn.open || !conn.dropped || conn.recv_armed || conn.sending {
            return;
        }
        if conn.unsent() > 0 && conn.write_error.is_none() && !conn.abandon {
            // Still sending what the stream wrote before it was dropped.
            return;
        }
        unsafe { libc::close(conn.fd) };
        conn.open = false;
        conn.fd = -1;
        conn.generation = conn.generation.wrapping_add(1) & GEN_MASK;
        conn.input.clear();
        conn.start = 0;
        conn.out.clear();
        conn.queued.clear();
        conn.read_waker = None;
        conn.write_waker = None;
        // Keep the allocations of ordinary-sized buffers for the next user.
        if conn.input.capacity() > 2 * MAX_INPUT {
            conn.input = Vec::new();
        }
        if conn.out.capacity() > MAX_OUTPUT || conn.queued.capacity() > MAX_OUTPUT {
            conn.out = Vec::new();
            conn.queued = Vec::new();
        }
        self.free.push(slot);
    }

    /// Queue the recvs and sends that connections are waiting for.
    fn flush_dirty(&mut self) {
        let dirty = std::mem::take(&mut self.dirty);
        for &slot in &dirty {
            let conn = &mut self.conns[slot as usize];
            conn.dirty = false;
            if !conn.open || conn.abandon {
                continue;
            }
            let fd = conn.fd;
            let generation = conn.generation;
            let arm = !conn.dropped
                && !conn.recv_armed
                && !conn.paused
                && !conn.eof
                && conn.read_error.is_none();
            if arm {
                conn.recv_armed = true;
                conn.recv_cancelled = false;
            }
            let send = !conn.sending && conn.sent < conn.out.len() && conn.write_error.is_none();
            let (ptr, len) = (conn.out[conn.sent..].as_ptr(), conn.out.len() - conn.sent);
            if send {
                conn.sending = true;
                conn.send_cancelled = false;
            }
            if arm {
                self.push(
                    opcode::RecvMulti::new(types::Fd(fd), BGID)
                        .build()
                        .user_data(tag(OP_RECV, generation, slot)),
                );
            }
            if send {
                self.push(
                    opcode::Send::new(types::Fd(fd), ptr, len as u32)
                        .flags(libc::MSG_NOSIGNAL)
                        .build()
                        .user_data(tag(OP_SEND, generation, slot)),
                );
            }
        }
        // Give the Vec's allocation back for the next turn.
        let mut dirty = dirty;
        dirty.clear();
        if self.dirty.is_empty() {
            self.dirty = dirty;
        }
    }

    fn arm_accept(&mut self) {
        let listener = &mut self.listener;
        if !listener.open || listener.armed || listener.accepted.len() >= MAX_ACCEPTED {
            return;
        }
        listener.armed = true;
        listener.cancelled = false;
        let entry = opcode::AcceptMulti::new(types::Fd(listener.fd))
            .flags(libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC)
            .build()
            .user_data(tag(OP_ACCEPT, listener.generation, 0));
        self.push(entry);
        self.kick();
    }

    fn cancel(&mut self, target: u64) {
        self.push(
            opcode::AsyncCancel::new(target)
                .build()
                .user_data(tag(OP_CANCEL, 0, 0)),
        );
        self.kick();
    }

    /// One pump turn: submit queued work and run completions until no more is
    /// pending, up to `MAX_ROUNDS`. Returns whether work remains.
    fn turn(&mut self) -> io::Result<bool> {
        self.kicked = false;
        for _ in 0..MAX_ROUNDS {
            self.flush_dirty();
            self.bufs.publish();
            let to_submit = self.ring.submission().len();
            // The pump's own completions must not wake it again.
            self.ring.completion().disable_eventfd();
            // SAFETY: no argument struct is passed.
            let entered = unsafe {
                self.ring.submitter().enter::<libc::sigset_t>(
                    to_submit as u32,
                    0,
                    IORING_ENTER_GETEVENTS,
                    None,
                )
            };
            self.ring.completion().enable_eventfd();
            if let Err(error) = entered {
                match error.raw_os_error() {
                    // Interrupted, or completions must be reaped before more
                    // can be submitted: reap, then go round again.
                    Some(libc::EINTR | libc::EBUSY | libc::EAGAIN) => {}
                    _ => return Err(error),
                }
            }
            self.reap();
            // Completion work that arrived while the eventfd was disabled.
            let pending = self.ring.submission().taskrun();
            if self.dirty.is_empty() && self.ring.submission().is_empty() && !pending {
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn reap(&mut self) {
        loop {
            let mut batch: [std::mem::MaybeUninit<cqueue::Entry>; 64] =
                [const { std::mem::MaybeUninit::uninit() }; 64];
            let n = self.ring.completion().fill(&mut batch).len();
            if n == 0 {
                break;
            }
            for entry in &batch[..n] {
                // SAFETY: `fill` initialised the first `n` entries.
                let entry = unsafe { entry.assume_init_ref() };
                self.complete(entry.user_data(), entry.result(), entry.flags());
            }
            if n < batch.len() {
                break;
            }
        }
        self.bufs.publish();
    }

    fn complete(&mut self, user_data: u64, result: i32, flags: u32) {
        let (op, generation, slot) = untag(user_data);
        match op {
            OP_ACCEPT => self.on_accept(generation, result, flags),
            OP_RECV => self.on_recv(slot, generation, result, flags),
            OP_SEND => self.on_send(slot, generation, result),
            _ => {}
        }
    }

    fn on_accept(&mut self, generation: u32, result: i32, flags: u32) {
        let listener = &mut self.listener;
        let current = listener.open && listener.generation == generation;
        if !current {
            if result >= 0 {
                unsafe { libc::close(result) };
            }
            return;
        }
        if !cqueue::more(flags) {
            listener.armed = false;
        }
        if result >= 0 {
            listener.accepted.push_back(result);
        } else if result != -libc::ECANCELED {
            listener.error = Some(-result);
        }
        let pause =
            listener.accepted.len() >= MAX_ACCEPTED && listener.armed && !listener.cancelled;
        if pause {
            // The server is behind: stop accepting until it catches up.
            listener.cancelled = true;
        }
        if let Some(waker) = listener.waker.take() {
            self.wake.push(waker);
        }
        if pause {
            self.cancel(tag(OP_ACCEPT, generation, 0));
        }
    }

    fn on_recv(&mut self, slot: u32, generation: u32, result: i32, flags: u32) {
        let more = cqueue::more(flags);
        let buffer = cqueue::buffer_select(flags);
        let current = matches!(
            self.conns.get(slot as usize),
            Some(conn) if conn.open && conn.generation == generation
        );
        if !current {
            if let Some(bid) = buffer {
                self.bufs.push(bid);
            }
            return;
        }
        let conn = &mut self.conns[slot as usize];
        if !more {
            conn.recv_armed = false;
        }
        let mut rearm = false;
        if result > 0 {
            if let Some(bid) = buffer {
                if !conn.dropped {
                    if conn.start == conn.input.len() {
                        conn.input.clear();
                        conn.start = 0;
                    } else if conn.start >= conn.input.len() / 2 {
                        // Drop the consumed prefix, so a reader that stays a
                        // little behind does not grow the buffer for ever.
                        conn.input.drain(..conn.start);
                        conn.start = 0;
                    }
                    conn.input
                        .extend_from_slice(self.bufs.get(bid, result as usize));
                }
                self.bufs.push(bid);
            }
            if conn.unread() > MAX_INPUT && !conn.paused {
                conn.paused = true;
                if conn.recv_armed && !conn.recv_cancelled {
                    conn.recv_cancelled = true;
                    let target = tag(OP_RECV, generation, slot);
                    self.cancel(target);
                }
            } else if !more {
                rearm = true;
            }
        } else if result == 0 {
            conn.eof = true;
        } else if result == -libc::ENOBUFS {
            // The provided buffers ran out for a moment; they are refilled
            // before the next submit.
            rearm = !more;
        } else if result != -libc::ECANCELED {
            conn.read_error = Some(-result);
        }
        let conn = &mut self.conns[slot as usize];
        if let Some(waker) = conn.read_waker.take() {
            self.wake.push(waker);
        }
        if conn.dropped {
            self.try_finalize(slot);
        } else if rearm {
            self.mark_dirty(slot);
        }
    }

    fn on_send(&mut self, slot: u32, generation: u32, result: i32) {
        let current = matches!(
            self.conns.get(slot as usize),
            Some(conn) if conn.open && conn.generation == generation
        );
        if !current {
            return;
        }
        let conn = &mut self.conns[slot as usize];
        conn.sending = false;
        if conn.abandon {
            conn.out.clear();
            conn.sent = 0;
            conn.queued.clear();
            self.try_finalize(slot);
            return;
        }
        if result < 0 {
            conn.write_error = Some(if result == -libc::ECANCELED {
                libc::EPIPE
            } else {
                -result
            });
            conn.out.clear();
            conn.sent = 0;
            conn.queued.clear();
        } else {
            conn.sent += result as usize;
            if conn.sent >= conn.out.len() {
                conn.out.clear();
                conn.sent = 0;
                std::mem::swap(&mut conn.out, &mut conn.queued);
            }
            if conn.sent < conn.out.len() {
                // Not all sent, or more was written meanwhile.
                self.mark_dirty(slot);
            }
        }
        let conn = &mut self.conns[slot as usize];
        if let Some(waker) = conn.write_waker.take() {
            self.wake.push(waker);
        }
        if conn.dropped {
            self.try_finalize(slot);
        }
    }

    /// Close dropped streams that are still sending past their deadline, and
    /// forget finished ones. Returns the next deadline.
    fn expire_lingering(&mut self) -> Option<tokio::time::Instant> {
        if self.lingering.is_empty() {
            return None;
        }
        let now = tokio::time::Instant::now();
        let mut next: Option<tokio::time::Instant> = None;
        let mut lingering = std::mem::take(&mut self.lingering);
        lingering.retain(|&(slot, generation, deadline)| {
            let conn = &mut self.conns[slot as usize];
            if !conn.open || conn.generation != generation {
                return false;
            }
            if deadline > now {
                next = Some(next.map_or(deadline, |next| next.min(deadline)));
                return true;
            }
            conn.abandon = true;
            if conn.sending && !conn.send_cancelled {
                conn.send_cancelled = true;
                self.cancel(tag(OP_SEND, generation, slot));
            }
            self.try_finalize(slot);
            false
        });
        self.lingering = lingering;
        next
    }
}

impl Drop for Ring {
    fn drop(&mut self) {
        let _ = self.ring.submitter().unregister_buf_ring(BGID);
        for conn in &mut self.conns {
            if conn.open {
                unsafe { libc::close(conn.fd) };
            }
            if conn.sending {
                // The kernel may still read a send's buffer while the ring is
                // torn down asynchronously, so it is left allocated too.
                std::mem::forget(std::mem::take(&mut conn.out));
            }
        }
        for &fd in &self.listener.accepted {
            unsafe { libc::close(fd) };
        }
        // The kernel tears the ring down asynchronously after its descriptor
        // closes and could still complete a recv that already selected a
        // buffer, so the buffer memory is left allocated. This only happens
        // when the loop's thread exits.
    }
}

fn context(error: io::Error, what: &str) -> io::Error {
    io::Error::new(error.kind(), format!("{what}: {error}"))
}

/// Make sure this thread has a ring and the current runtime has a pump task.
/// Must be called inside the loop's runtime, on its thread.
fn ensure_ring() -> io::Result<()> {
    RING.with(|cell| {
        let mut slot = cell.borrow_mut();
        if slot.is_none() {
            *slot = Some(Ring::new()?);
        }
        let ring = slot.as_deref_mut().expect("ring");
        if !ring.pump_alive.load(Ordering::Acquire) {
            let duplicate =
                unsafe { libc::fcntl(ring.eventfd.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 0) };
            if duplicate < 0 {
                return Err(io::Error::last_os_error());
            }
            // SAFETY: a new descriptor nothing else owns.
            let owned = unsafe { OwnedFd::from_raw_fd(duplicate) };
            // SAFETY: the AsyncFd owns the descriptor for its whole life.
            let eventfd =
                unsafe { AsyncFd::register_with_interest(owned, tokio::io::Interest::READABLE) }
                    .map_err(|error| error.into_parts().1)?;
            let alive = Arc::new(AtomicBool::new(true));
            ring.pump_alive = alive.clone();
            ring.pump_waker = None;
            ring.kicked = false;
            tokio::spawn(pump(eventfd, alive));
        }
        Ok(())
    })
}

/// Clears the ring's `pump_alive` when the pump task ends or is dropped.
struct PumpGuard(Arc<AtomicBool>);

impl Drop for PumpGuard {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

async fn pump(eventfd: AsyncFd<OwnedFd>, alive: Arc<AtomicBool>) {
    let _guard = PumpGuard(alive);
    let mut wake: Vec<Waker> = Vec::new();
    loop {
        let turned = with(|ring| {
            let turned = ring.turn();
            std::mem::swap(&mut wake, &mut ring.wake);
            turned
        });
        for waker in wake.drain(..) {
            waker.wake();
        }
        match turned {
            Ok(Ok(false)) => {}
            // More work than one turn takes: let the woken tasks run first.
            Ok(Ok(true)) => {
                tokio::task::yield_now().await;
                continue;
            }
            Ok(Err(error)) => {
                // The ring itself failed: fail every user so connections end
                // instead of hanging.
                let _ = with(|ring| {
                    fail_all(ring, &error);
                    std::mem::swap(&mut wake, &mut ring.wake);
                });
                for waker in wake.drain(..) {
                    waker.wake();
                }
                return;
            }
            Err(_) => return,
        }
        let deadline = with(|ring| ring.expire_lingering()).ok().flatten();
        let mut linger = deadline.map(|deadline| Box::pin(tokio::time::sleep_until(deadline)));
        std::future::poll_fn(|cx| {
            if let Some(sleep) = linger.as_mut()
                && sleep.as_mut().poll(cx).is_ready()
            {
                return Poll::Ready(());
            }
            let kicked = with(|ring| {
                if ring.kicked {
                    true
                } else {
                    if !ring
                        .pump_waker
                        .as_ref()
                        .is_some_and(|waker| waker.will_wake(cx.waker()))
                    {
                        ring.pump_waker = Some(cx.waker().clone());
                    }
                    false
                }
            })
            .unwrap_or(true);
            if kicked {
                return Poll::Ready(());
            }
            match eventfd.poll_read_ready(cx) {
                Poll::Ready(Ok(mut guard)) => {
                    // The eventfd is watched edge-triggered, and every signal
                    // is a new edge, so its counter is never read.
                    guard.clear_ready();
                    Poll::Ready(())
                }
                Poll::Ready(Err(_)) => Poll::Ready(()),
                Poll::Pending => Poll::Pending,
            }
        })
        .await;
    }
}

fn fail_all(ring: &mut Ring, error: &io::Error) {
    let code = error.raw_os_error().unwrap_or(libc::EIO);
    for conn in ring.conns.iter_mut().filter(|conn| conn.open) {
        conn.read_error.get_or_insert(code);
        conn.write_error.get_or_insert(code);
        ring.wake.extend(conn.read_waker.take());
        ring.wake.extend(conn.write_waker.take());
    }
    ring.listener.error = Some(code);
    ring.wake.extend(ring.listener.waker.take());
}

/// Borrow `fd` as a std `TcpStream` for socket calls, without owning it.
fn borrowed(fd: RawFd) -> ManuallyDrop<std::net::TcpStream> {
    // SAFETY: the descriptor stays open for the borrow, and ManuallyDrop never
    // closes it.
    ManuallyDrop::new(unsafe { std::net::TcpStream::from_raw_fd(fd) })
}

/// A TCP listener whose connections are accepted, read and written through
/// this thread's io_uring.
pub struct Listener {
    listener: std::net::TcpListener,
    generation: u32,
}

impl Listener {
    /// Serve `listener` through io_uring. On failure (io_uring blocked or
    /// unsupported) the listener is handed back with the reason.
    pub fn new(
        listener: std::net::TcpListener,
    ) -> Result<Self, (std::net::TcpListener, io::Error)> {
        if let Err(error) = ensure_ring() {
            return Err((listener, error));
        }
        let fd = listener.as_raw_fd();
        let generation = with(|ring| {
            let state = &mut ring.listener;
            if state.open {
                return Err(io::Error::other(
                    "this thread already has an io_uring listener",
                ));
            }
            state.generation = state.generation.wrapping_add(1) & GEN_MASK;
            state.fd = fd;
            state.open = true;
            state.armed = false;
            state.error = None;
            let generation = state.generation;
            ring.arm_accept();
            Ok(generation)
        });
        match generation {
            Ok(Ok(generation)) => Ok(Self {
                listener,
                generation,
            }),
            Ok(Err(error)) | Err(error) => Err((listener, error)),
        }
    }

    pub fn local_addr(&self) -> io::Result<SocketAddr> {
        self.listener.local_addr()
    }

    pub fn poll_accept(&self, cx: &mut Context<'_>) -> Poll<io::Result<(Stream, SocketAddr)>> {
        let accepted = with(|ring| {
            if ring.listener.generation != self.generation || !ring.listener.open {
                return Poll::Ready(Err(io::Error::from(io::ErrorKind::NotConnected)));
            }
            if let Some(fd) = ring.listener.accepted.pop_front() {
                ring.arm_accept();
                return Poll::Ready(Ok(fd));
            }
            if let Some(code) = ring.listener.error.take() {
                ring.arm_accept();
                return Poll::Ready(Err(io::Error::from_raw_os_error(code)));
            }
            ring.arm_accept();
            ring.listener.waker = Some(cx.waker().clone());
            Poll::Pending
        });
        let fd = match accepted {
            Ok(Poll::Ready(Ok(fd))) => fd,
            Ok(Poll::Ready(Err(error))) | Err(error) => return Poll::Ready(Err(error)),
            Ok(Poll::Pending) => return Poll::Pending,
        };
        let peer = match borrowed(fd).peer_addr() {
            Ok(peer) => peer,
            Err(_) => {
                // Reset before it was served.
                unsafe { libc::close(fd) };
                return Poll::Ready(Err(io::ErrorKind::ConnectionAborted.into()));
            }
        };
        match with(|ring| ring.open_conn(fd)) {
            Ok((slot, generation)) => Poll::Ready(Ok((
                Stream {
                    slot,
                    generation,
                    fd,
                },
                peer,
            ))),
            Err(error) => {
                unsafe { libc::close(fd) };
                Poll::Ready(Err(error))
            }
        }
    }

    pub async fn accept(&self) -> io::Result<(Stream, SocketAddr)> {
        std::future::poll_fn(|cx| self.poll_accept(cx)).await
    }
}

impl Drop for Listener {
    fn drop(&mut self) {
        let generation = self.generation;
        let _ = with(|ring| {
            let state = &mut ring.listener;
            if state.generation != generation || !state.open {
                return;
            }
            state.open = false;
            state.waker = None;
            for fd in state.accepted.drain(..) {
                unsafe { libc::close(fd) };
            }
            let armed = std::mem::replace(&mut state.armed, false);
            // Later completions of the old accept are closed as stale.
            state.generation = state.generation.wrapping_add(1) & GEN_MASK;
            if armed {
                ring.cancel(tag(OP_ACCEPT, generation, 0));
            }
        });
        // The listening descriptor closes with `self.listener`; the kernel
        // keeps the socket until the cancelled accept completes.
    }
}

/// A connection read and written through this thread's io_uring.
pub struct Stream {
    slot: u32,
    generation: u32,
    fd: RawFd,
}

impl Stream {
    pub fn set_nodelay(&self, nodelay: bool) -> io::Result<()> {
        borrowed(self.fd).set_nodelay(nodelay)
    }

    pub fn peer_addr(&self) -> io::Result<SocketAddr> {
        borrowed(self.fd).peer_addr()
    }

    pub fn local_addr(&self) -> io::Result<SocketAddr> {
        borrowed(self.fd).local_addr()
    }

    /// Write directly to the socket without waiting, as Tokio's `try_write`.
    /// Only for a stream nothing else has been written to.
    pub fn try_write(&self, buf: &[u8]) -> io::Result<usize> {
        let sent = unsafe {
            libc::send(
                self.fd,
                buf.as_ptr().cast(),
                buf.len(),
                libc::MSG_NOSIGNAL | libc::MSG_DONTWAIT,
            )
        };
        if sent < 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(sent as usize)
        }
    }

    fn write_bufs(&self, cx: &mut Context<'_>, bufs: &[&[u8]]) -> Poll<io::Result<usize>> {
        let (slot, generation) = (self.slot, self.generation);
        let total: usize = bufs.iter().map(|buf| buf.len()).sum();
        let polled = with(|ring| {
            let conn = ring.conn(slot, generation)?;
            if let Some(code) = conn.write_error {
                return Err(io::Error::from_raw_os_error(code));
            }
            if total == 0 {
                return Ok(Some(0));
            }
            if conn.unsent() >= MAX_OUTPUT {
                conn.write_waker = Some(cx.waker().clone());
                return Ok(None);
            }
            // Take no more than the room left, so a large write waits (and
            // its caller keeps the rest, and any budget it holds) instead of
            // being copied here in full.
            let room = MAX_OUTPUT - conn.unsent();
            let target = if conn.sending {
                &mut conn.queued
            } else {
                &mut conn.out
            };
            let mut taken = 0;
            for buf in bufs {
                let n = buf.len().min(room - taken);
                target.extend_from_slice(&buf[..n]);
                taken += n;
                if taken == room {
                    break;
                }
            }
            if !conn.sending {
                ring.mark_dirty(slot);
            }
            Ok(Some(taken))
        });
        match polled {
            Ok(Ok(Some(n))) => Poll::Ready(Ok(n)),
            Ok(Ok(None)) => Poll::Pending,
            Ok(Err(error)) | Err(error) => Poll::Ready(Err(error)),
        }
    }
}

impl AsyncRead for Stream {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let (slot, generation) = (self.slot, self.generation);
        let polled = with(|ring| {
            let conn = ring.conn(slot, generation)?;
            if conn.unread() > 0 {
                let n = conn.unread().min(buf.remaining());
                buf.put_slice(&conn.input[conn.start..conn.start + n]);
                conn.start += n;
                if conn.start == conn.input.len() {
                    conn.input.clear();
                    conn.start = 0;
                }
                if conn.paused && conn.unread() <= MAX_INPUT / 2 {
                    conn.paused = false;
                    if !conn.recv_armed {
                        ring.mark_dirty(slot);
                    }
                }
                return Ok(true);
            }
            if let Some(code) = conn.read_error {
                return Err(io::Error::from_raw_os_error(code));
            }
            if conn.eof {
                return Ok(true);
            }
            if !conn
                .read_waker
                .as_ref()
                .is_some_and(|waker| waker.will_wake(cx.waker()))
            {
                conn.read_waker = Some(cx.waker().clone());
            }
            let rearm = !conn.recv_armed;
            conn.paused = false;
            if rearm {
                ring.mark_dirty(slot);
            }
            Ok(false)
        });
        match polled {
            Ok(Ok(true)) => Poll::Ready(Ok(())),
            Ok(Ok(false)) => Poll::Pending,
            Ok(Err(error)) | Err(error) => Poll::Ready(Err(error)),
        }
    }
}

impl AsyncWrite for Stream {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        self.write_bufs(cx, &[buf])
    }

    fn poll_write_vectored(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bufs: &[io::IoSlice<'_>],
    ) -> Poll<io::Result<usize>> {
        let slices: smallvec_like::Slices<'_> = smallvec_like::Slices::new(bufs);
        self.write_bufs(cx, slices.as_slice())
    }

    fn is_write_vectored(&self) -> bool {
        true
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let (slot, generation) = (self.slot, self.generation);
        let polled = with(|ring| {
            let conn = ring.conn(slot, generation)?;
            if let Some(code) = conn.write_error {
                return Err(io::Error::from_raw_os_error(code));
            }
            if !conn.sending && conn.unsent() == 0 {
                return Ok(true);
            }
            conn.write_waker = Some(cx.waker().clone());
            Ok(false)
        });
        match polled {
            Ok(Ok(true)) => Poll::Ready(Ok(())),
            Ok(Ok(false)) => Poll::Pending,
            Ok(Err(error)) | Err(error) => Poll::Ready(Err(error)),
        }
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        match self.as_mut().poll_flush(cx) {
            Poll::Ready(Ok(())) => {}
            other => return other,
        }
        if unsafe { libc::shutdown(self.fd, libc::SHUT_WR) } < 0 {
            let error = io::Error::last_os_error();
            if error.raw_os_error() != Some(libc::ENOTCONN) {
                return Poll::Ready(Err(error));
            }
        }
        Poll::Ready(Ok(()))
    }
}

impl Drop for Stream {
    fn drop(&mut self) {
        let (slot, generation) = (self.slot, self.generation);
        let closed = with(|ring| {
            let Ok(conn) = ring.conn(slot, generation) else {
                return;
            };
            conn.dropped = true;
            conn.read_waker = None;
            conn.write_waker = None;
            // What was written was accepted as a Tokio stream accepts it into
            // the socket buffer: keep sending it after the drop, as the kernel
            // would, but only for up to LINGER.
            let draining = conn.write_error.is_none() && conn.unsent() > 0;
            let idle = !conn.sending;
            let cancel_recv = conn.recv_armed && !conn.recv_cancelled;
            conn.recv_cancelled |= cancel_recv;
            if cancel_recv {
                ring.cancel(tag(OP_RECV, generation, slot));
            }
            if draining {
                let deadline = tokio::time::Instant::now() + LINGER;
                ring.lingering.push((slot, generation, deadline));
                if idle {
                    ring.mark_dirty(slot);
                }
                ring.kick();
            }
            ring.try_finalize(slot);
        });
        if closed.is_err() {
            // Off the ring's thread (or after it is gone): nothing can be
            // cancelled, so only the descriptor is closed.
            unsafe { libc::close(self.fd) };
        }
    }
}

mod smallvec_like {
    /// The slices of an `IoSlice` list, on the stack for the usual few.
    pub(super) struct Slices<'a> {
        inline: [&'a [u8]; 8],
        heap: Vec<&'a [u8]>,
        len: usize,
    }

    impl<'a> Slices<'a> {
        pub(super) fn new(bufs: &'a [std::io::IoSlice<'a>]) -> Self {
            let mut this = Slices {
                inline: [&[]; 8],
                heap: Vec::new(),
                len: bufs.len(),
            };
            if bufs.len() <= 8 {
                for (slot, buf) in this.inline.iter_mut().zip(bufs) {
                    *slot = buf;
                }
            } else {
                this.heap = bufs.iter().map(|buf| &**buf).collect();
            }
            this
        }

        pub(super) fn as_slice(&self) -> &[&'a [u8]] {
            if self.len <= 8 {
                &self.inline[..self.len]
            } else {
                &self.heap
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    /// Run `serve` on a current-thread runtime with an io_uring listener,
    /// or return None where io_uring is unavailable (a seccomp filter).
    fn with_server<F, Fut>(serve: F, client: impl FnOnce(SocketAddr) + Send + 'static) -> bool
    where
        F: FnOnce(Listener) -> Fut,
        Fut: std::future::Future<Output = ()>,
    {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let std_listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        std_listener.set_nonblocking(true).unwrap();
        let addr = std_listener.local_addr().unwrap();
        runtime.block_on(async move {
            let listener = match Listener::new(std_listener) {
                Ok(listener) => listener,
                Err((_, error)) => {
                    eprintln!("io_uring unavailable, skipping: {error}");
                    return false;
                }
            };
            let client = std::thread::spawn(move || client(addr));
            serve(listener).await;
            client.join().unwrap();
            true
        })
    }

    async fn echo(mut stream: Stream) {
        let mut buf = vec![0u8; 4096];
        loop {
            let n = stream.read(&mut buf).await.unwrap();
            if n == 0 {
                break;
            }
            stream.write_all(&buf[..n]).await.unwrap();
        }
        stream.shutdown().await.unwrap();
    }

    #[test]
    fn echoes_many_connections() {
        const CONNECTIONS: usize = 64;
        with_server(
            |listener| async move {
                let mut tasks = tokio::task::JoinSet::new();
                for _ in 0..CONNECTIONS {
                    let (stream, peer) = listener.accept().await.unwrap();
                    assert!(peer.ip().is_loopback());
                    stream.set_nodelay(true).unwrap();
                    tasks.spawn(echo(stream));
                }
                while let Some(result) = tasks.join_next().await {
                    result.unwrap();
                }
            },
            |addr| {
                let clients: Vec<_> = (0..CONNECTIONS)
                    .map(|i| {
                        std::thread::spawn(move || {
                            let mut stream = std::net::TcpStream::connect(addr).unwrap();
                            for round in 0..50 {
                                let message = format!("connection {i} round {round}\n");
                                stream.write_all(message.as_bytes()).unwrap();
                                let mut back = vec![0u8; message.len()];
                                stream.read_exact(&mut back).unwrap();
                                assert_eq!(back, message.as_bytes());
                            }
                            stream.shutdown(std::net::Shutdown::Write).unwrap();
                            let mut rest = Vec::new();
                            stream.read_to_end(&mut rest).unwrap();
                            assert!(rest.is_empty());
                        })
                    })
                    .collect();
                for client in clients {
                    client.join().unwrap();
                }
            },
        );
    }

    #[test]
    fn a_large_write_is_taken_only_up_to_the_output_limit() {
        const SIZE: usize = 1024 * 1024;
        with_server(
            |listener| async move {
                let (mut stream, _) = listener.accept().await.unwrap();
                let body = vec![7u8; SIZE];
                let first = stream.write(&body).await.unwrap();
                assert_eq!(first, MAX_OUTPUT);
                stream.write_all(&body[first..]).await.unwrap();
                stream.shutdown().await.unwrap();
            },
            |addr| {
                let mut stream = std::net::TcpStream::connect(addr).unwrap();
                let mut got = Vec::new();
                stream.read_to_end(&mut got).unwrap();
                assert_eq!(got.len(), SIZE);
                assert!(got.iter().all(|&byte| byte == 7));
            },
        );
    }

    #[test]
    fn large_transfers_in_both_directions() {
        // Larger than the input pause and output limits, so recv pauses and
        // resumes and sends complete partially.
        const SIZE: usize = 4 * 1024 * 1024;
        let payload: Arc<Vec<u8>> = Arc::new((0..SIZE).map(|i| (i * 31 % 251) as u8).collect());
        let expected = payload.clone();
        with_server(
            |listener| async move {
                let (stream, _) = listener.accept().await.unwrap();
                echo(stream).await;
            },
            move |addr| {
                let stream = std::net::TcpStream::connect(addr).unwrap();
                let mut reader = stream.try_clone().unwrap();
                let writer = std::thread::spawn(move || {
                    let mut stream = stream;
                    // Slowly, in pieces, to exercise paused reads.
                    for chunk in payload.chunks(100_000) {
                        stream.write_all(chunk).unwrap();
                    }
                    stream.shutdown(std::net::Shutdown::Write).unwrap();
                });
                let mut back = Vec::new();
                std::thread::sleep(std::time::Duration::from_millis(200));
                reader.read_to_end(&mut back).unwrap();
                writer.join().unwrap();
                assert_eq!(back.len(), expected.len());
                assert!(back == *expected);
            },
        );
    }

    #[test]
    fn dropping_a_stream_closes_the_connection() {
        with_server(
            |listener| async move {
                for _ in 0..3 {
                    let (mut stream, _) = listener.accept().await.unwrap();
                    stream.write_all(b"bye").await.unwrap();
                    stream.flush().await.unwrap();
                    drop(stream);
                }
                // Let the pump submit the cancellations and close.
                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            },
            |addr| {
                for _ in 0..3 {
                    let mut stream = std::net::TcpStream::connect(addr).unwrap();
                    stream
                        .set_read_timeout(Some(std::time::Duration::from_secs(5)))
                        .unwrap();
                    let mut got = Vec::new();
                    stream.read_to_end(&mut got).unwrap();
                    assert_eq!(got, b"bye");
                }
            },
        );
    }

    #[test]
    fn unflushed_writes_are_sent_when_the_stream_is_dropped() {
        with_server(
            |listener| async move {
                let (mut stream, _) = listener.accept().await.unwrap();
                stream.write_all(b"unflushed").await.unwrap();
                drop(stream);
                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            },
            |addr| {
                let mut stream = std::net::TcpStream::connect(addr).unwrap();
                stream
                    .set_read_timeout(Some(std::time::Duration::from_secs(5)))
                    .unwrap();
                let mut got = Vec::new();
                stream.read_to_end(&mut got).unwrap();
                assert_eq!(got, b"unflushed");
            },
        );
    }

    #[test]
    fn a_dropped_stream_finishes_sending_to_a_slow_reader() {
        // More than a socket buffer, written and dropped without a flush.
        const SIZE: usize = 200 * 1024;
        with_server(
            |listener| async move {
                let (mut stream, _) = listener.accept().await.unwrap();
                stream.write_all(&vec![3u8; SIZE]).await.unwrap();
                drop(stream);
                // Keep the runtime (and the pump) alive while the client
                // reads slowly.
                tokio::time::sleep(std::time::Duration::from_millis(800)).await;
            },
            |addr| {
                let mut stream = std::net::TcpStream::connect(addr).unwrap();
                std::thread::sleep(std::time::Duration::from_millis(300));
                stream
                    .set_read_timeout(Some(std::time::Duration::from_secs(5)))
                    .unwrap();
                let mut got = Vec::new();
                stream.read_to_end(&mut got).unwrap();
                assert_eq!(got.len(), SIZE);
            },
        );
    }

    #[test]
    fn accepted_connections_queue_only_up_to_the_limit() {
        const CONNECTIONS: usize = MAX_ACCEPTED + 100;
        let (connected, ready) = std::sync::mpsc::channel();
        let (release, done) = std::sync::mpsc::channel::<()>();
        with_server(
            |listener| async move {
                // Wait without blocking the runtime, so the pump keeps
                // accepting while the client connects.
                while ready.try_recv().is_err() {
                    tokio::time::sleep(std::time::Duration::from_millis(10)).await;
                }
                // Let the pump accept as much as it will before serving any.
                tokio::time::sleep(std::time::Duration::from_millis(200)).await;
                let queued = with(|ring| ring.listener.accepted.len()).unwrap();
                // A few more can complete while the cancellation is in flight.
                assert!(queued <= MAX_ACCEPTED + 64, "{queued} queued");
                for _ in 0..CONNECTIONS {
                    let (stream, _) = listener.accept().await.unwrap();
                    drop(stream);
                }
                release.send(()).unwrap();
            },
            move |addr| {
                let streams: Vec<_> = (0..CONNECTIONS)
                    .map(|_| std::net::TcpStream::connect(addr).unwrap())
                    .collect();
                connected.send(()).unwrap();
                done.recv().unwrap();
                drop(streams);
            },
        );
    }

    #[test]
    fn listener_can_be_replaced_on_the_same_thread() {
        for _ in 0..2 {
            with_server(
                |listener| async move {
                    let (stream, _) = listener.accept().await.unwrap();
                    echo(stream).await;
                },
                |addr| {
                    let mut stream = std::net::TcpStream::connect(addr).unwrap();
                    stream.write_all(b"ping").unwrap();
                    stream.shutdown(std::net::Shutdown::Write).unwrap();
                    let mut back = Vec::new();
                    stream.read_to_end(&mut back).unwrap();
                    assert_eq!(back, b"ping");
                },
            );
        }
    }

    /// What a container's default seccomp profile does: io_uring_setup
    /// fails with EPERM. The listener is handed back for the epoll path.
    #[test]
    fn blocked_io_uring_hands_the_listener_back() {
        std::thread::spawn(|| {
            // A filter for this thread only: EPERM for io_uring_setup.
            let mut filter = [
                libc::sock_filter {
                    code: (libc::BPF_LD | libc::BPF_W | libc::BPF_ABS) as u16,
                    jt: 0,
                    jf: 0,
                    k: 0,
                },
                libc::sock_filter {
                    code: (libc::BPF_JMP | libc::BPF_JEQ | libc::BPF_K) as u16,
                    jt: 0,
                    jf: 1,
                    k: libc::SYS_io_uring_setup as u32,
                },
                libc::sock_filter {
                    code: (libc::BPF_RET | libc::BPF_K) as u16,
                    jt: 0,
                    jf: 0,
                    k: libc::SECCOMP_RET_ERRNO | libc::EPERM as u32,
                },
                libc::sock_filter {
                    code: (libc::BPF_RET | libc::BPF_K) as u16,
                    jt: 0,
                    jf: 0,
                    k: libc::SECCOMP_RET_ALLOW,
                },
            ];
            let program = libc::sock_fprog {
                len: filter.len() as u16,
                filter: filter.as_mut_ptr(),
            };
            unsafe {
                assert_eq!(libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), 0);
                assert_eq!(
                    libc::prctl(libc::PR_SET_SECCOMP, libc::SECCOMP_MODE_FILTER, &program),
                    0
                );
            }
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap();
            runtime.block_on(async {
                let std_listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
                let addr = std_listener.local_addr().unwrap();
                let (listener, error) = match Listener::new(std_listener) {
                    Ok(_) => panic!("io_uring_setup should have been refused"),
                    Err(returned) => returned,
                };
                assert_eq!(error.kind(), io::ErrorKind::PermissionDenied, "{error}");
                assert!(error.to_string().contains("io_uring_setup"), "{error}");
                assert_eq!(listener.local_addr().unwrap(), addr);
            });
        })
        .join()
        .unwrap();
    }

    #[test]
    fn use_off_the_ring_thread_is_an_error() {
        let error = with(|_| ()).unwrap_err();
        assert!(error.to_string().contains("off its event loop's thread"));
    }
}
