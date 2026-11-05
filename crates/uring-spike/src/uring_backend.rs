//! io_uring: multishot accept straight into the fixed file table, multishot
//! recv from a provided-buffer ring, sends batched into one io_uring_enter per
//! loop iteration, and (by default) SINGLE_ISSUER + DEFER_TASKRUN so that
//! completion work runs in this thread when it asks for events.

use crate::http::{Served, serve};
use io_uring::{IoUring, cqueue, opcode, squeue, types};
use std::io;
use std::os::fd::AsRawFd;
use std::sync::atomic::{AtomicU16, Ordering};

const BGID: u16 = 0;
const BUF_COUNT: u16 = 4096; // power of two
const BUF_SIZE: usize = 2048;

const OP_ACCEPT: u64 = 1;
const OP_RECV: u64 = 2;
const OP_SEND: u64 = 3;
const OP_CLOSE: u64 = 4;
const OP_CANCEL: u64 = 5;

/// user_data = op (8 bits) | generation (24 bits) | fixed file slot (32 bits).
/// The generation drops stale completions for a slot that has been reused.
fn tag(op: u64, gen_: u32, slot: u32) -> u64 {
    (op << 56) | ((gen_ as u64 & 0xff_ffff) << 32) | slot as u64
}

#[derive(Default)]
struct Conn {
    gen_: u32,
    open: bool,
    /// Leftover bytes of a request split across recvs.
    input: Vec<u8>,
    /// The buffer of the send in flight, and bytes of it already sent.
    out: Vec<u8>,
    sent: usize,
    /// Responses produced while a send was in flight.
    queued: Vec<u8>,
    sending: bool,
    recv_armed: bool,
    /// Close once the send in flight (if any) completes.
    closing: bool,
}

/// The provided-buffer ring: BUF_COUNT entries the kernel picks recv buffers
/// from, refilled by us as we finish with each buffer.
struct BufRing {
    entries: *mut types::BufRingEntry,
    bufs: Vec<u8>,
    tail: u16,
}

impl BufRing {
    fn new(ring: &IoUring) -> io::Result<Self> {
        let bytes = BUF_COUNT as usize * size_of::<types::BufRingEntry>();
        // SAFETY: anonymous mapping, page aligned as the kernel requires.
        let entries = unsafe {
            let p = libc::mmap(
                std::ptr::null_mut(),
                bytes,
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
        let mut this = BufRing {
            entries,
            bufs: vec![0; BUF_COUNT as usize * BUF_SIZE],
            tail: 0,
        };
        for bid in 0..BUF_COUNT {
            this.push(bid);
        }
        this.publish();
        // SAFETY: the mapping and the buffers live as long as the ring (the
        // process, in this spike).
        unsafe {
            ring.submitter()
                .register_buf_ring_with_flags(entries as u64, BUF_COUNT, BGID, 0)?;
        }
        Ok(this)
    }

    fn buf(&self, bid: u16, len: usize) -> &[u8] {
        let start = bid as usize * BUF_SIZE;
        &self.bufs[start..start + len]
    }

    /// Hand buffer `bid` back to the kernel (visible after `publish`).
    fn push(&mut self, bid: u16) {
        let idx = (self.tail & (BUF_COUNT - 1)) as usize;
        // SAFETY: idx < BUF_COUNT, inside the mapping.
        let entry = unsafe { &mut *self.entries.add(idx) };
        entry.set_addr(self.bufs.as_ptr() as u64 + bid as u64 * BUF_SIZE as u64);
        entry.set_len(BUF_SIZE as u32);
        entry.set_bid(bid);
        self.tail = self.tail.wrapping_add(1);
    }

    fn publish(&self) {
        // SAFETY: the tail field overlays the first entry's reserved u16.
        let tail = unsafe { &*(types::BufRingEntry::tail(self.entries) as *const AtomicU16) };
        tail.store(self.tail, Ordering::Release);
    }
}

struct Loop<'a> {
    submitter: io_uring::Submitter<'a>,
    sq: io_uring::SubmissionQueue<'a>,
    conns: Vec<Conn>,
    bufs: BufRing,
}

impl Loop<'_> {
    fn push(&mut self, entry: squeue::Entry) {
        // SAFETY: every buffer referenced by an entry outlives its operation:
        // send buffers stay in their Conn until the send completes, and a slot
        // is only closed once no send is in flight.
        while unsafe { self.sq.push(&entry) }.is_err() {
            self.sq.sync();
            self.submitter.submit().expect("io_uring_enter");
            self.sq.sync();
        }
    }

    fn arm_recv(&mut self, slot: u32) {
        let conn = &mut self.conns[slot as usize];
        conn.recv_armed = true;
        let ud = tag(OP_RECV, conn.gen_, slot);
        self.push(
            opcode::RecvMulti::new(types::Fixed(slot), BGID)
                .build()
                .user_data(ud),
        );
    }

    fn send(&mut self, slot: u32) {
        let conn = &mut self.conns[slot as usize];
        let ptr = conn.out[conn.sent..].as_ptr();
        let len = (conn.out.len() - conn.sent) as u32;
        let ud = tag(OP_SEND, conn.gen_, slot);
        conn.sending = true;
        self.push(
            opcode::Send::new(types::Fixed(slot), ptr, len)
                .build()
                .user_data(ud),
        );
    }

    fn close(&mut self, slot: u32) {
        let conn = &mut self.conns[slot as usize];
        if !conn.open {
            return;
        }
        conn.closing = true;
        if conn.sending {
            return; // finish when the send completes
        }
        conn.open = false;
        let (gen_, recv_armed) = (conn.gen_, conn.recv_armed);
        if recv_armed {
            let target = tag(OP_RECV, gen_, slot);
            self.push(
                opcode::AsyncCancel::new(target)
                    .build()
                    .user_data(tag(OP_CANCEL, gen_, slot)),
            );
        }
        self.push(
            opcode::Close::new(types::Fixed(slot))
                .build()
                .user_data(tag(OP_CLOSE, gen_, slot)),
        );
    }

    fn on_accept(&mut self, res: i32, flags: u32, listen_fd: i32) {
        if res >= 0 {
            let slot = res as u32;
            let conn = &mut self.conns[slot as usize];
            conn.gen_ = conn.gen_.wrapping_add(1) & 0xff_ffff;
            conn.open = true;
            conn.closing = false;
            conn.sending = false;
            conn.input.clear();
            conn.out.clear();
            conn.queued.clear();
            conn.sent = 0;
            self.arm_recv(slot);
        } else {
            eprintln!("accept: {}", io::Error::from_raw_os_error(-res));
        }
        if !cqueue::more(flags) {
            self.arm_accept(listen_fd);
        }
    }

    fn arm_accept(&mut self, listen_fd: i32) {
        let entry = opcode::AcceptMulti::new(types::Fd(listen_fd))
            .allocate_file_index(true)
            .build()
            .user_data(tag(OP_ACCEPT, 0, 0));
        self.push(entry);
    }

    fn on_recv(&mut self, slot: u32, res: i32, flags: u32) {
        let more = cqueue::more(flags);
        if !more {
            self.conns[slot as usize].recv_armed = false;
        }
        if res <= 0 {
            if res == -libc::ENOBUFS {
                // Out of provided buffers; they are refilled before the next
                // submit, so just re-arm.
                if !more {
                    self.arm_recv(slot);
                }
            } else {
                self.close(slot); // EOF or error
            }
            return;
        }
        let bid = cqueue::buffer_select(flags).expect("recv without a buffer");
        let data = self.bufs.buf(bid, res as usize);
        let conn = &mut self.conns[slot as usize];
        if conn.closing {
            self.bufs.push(bid);
            return;
        }
        // Responses go to `out` if it is free, else are queued behind it.
        let target = if conn.sending {
            &mut conn.queued
        } else {
            &mut conn.out
        };
        let served = if conn.input.is_empty() {
            let served = serve(data, target);
            if let Served::Ok { consumed, .. } = served {
                conn.input.extend_from_slice(&data[consumed..]);
            }
            served
        } else {
            conn.input.extend_from_slice(data);
            let served = serve(&conn.input, target);
            if let Served::Ok { consumed, .. } = served {
                conn.input.drain(..consumed);
            }
            served
        };
        self.bufs.push(bid);
        match served {
            Served::Bad => self.close(slot),
            Served::Ok { close, .. } => {
                let conn = &mut self.conns[slot as usize];
                if !conn.sending && !conn.out.is_empty() {
                    self.send(slot);
                }
                if close {
                    self.close(slot);
                } else if !more {
                    self.arm_recv(slot);
                }
            }
        }
    }

    fn on_send(&mut self, slot: u32, res: i32) {
        let conn = &mut self.conns[slot as usize];
        conn.sending = false;
        if res < 0 {
            self.close(slot);
            return;
        }
        conn.sent += res as usize;
        if conn.sent < conn.out.len() {
            self.send(slot);
            return;
        }
        conn.out.clear();
        conn.sent = 0;
        if !conn.queued.is_empty() {
            std::mem::swap(&mut conn.out, &mut conn.queued);
            self.send(slot);
        } else if conn.closing {
            self.close(slot);
        }
    }
}

pub fn run(listener: std::net::TcpListener, nofile: u64) -> io::Result<()> {
    let plain = std::env::var_os("SPIKE_URING_PLAIN").is_some_and(|v| v != "0");
    let mut builder = IoUring::builder();
    builder.setup_cqsize(16384);
    if !plain {
        builder.setup_single_issuer().setup_defer_taskrun();
    }
    let mut ring = builder.build(4096)?;
    let slots = nofile.min(16384) as u32;
    ring.submitter().register_files_sparse(slots)?;
    let bufs = BufRing::new(&ring)?;
    let listen_fd = listener.as_raw_fd();
    eprintln!(
        "uring-spike: {slots} fixed file slots, defer_taskrun={}",
        !plain
    );

    let (submitter, sq, mut cq) = ring.split();
    let mut lp = Loop {
        submitter,
        sq,
        conns: (0..slots).map(|_| Conn::default()).collect(),
        bufs,
    };
    lp.arm_accept(listen_fd);
    loop {
        lp.bufs.publish();
        lp.sq.sync();
        match lp.submitter.submit_and_wait(1) {
            Ok(_) => {}
            Err(err) if matches!(err.raw_os_error(), Some(libc::EINTR | libc::EBUSY)) => {}
            Err(err) => return Err(err),
        }
        cq.sync();
        for cqe in &mut cq {
            let ud = cqe.user_data();
            let (op, gen_, slot) = (ud >> 56, ((ud >> 32) & 0xff_ffff) as u32, ud as u32);
            if op == OP_ACCEPT {
                lp.on_accept(cqe.result(), cqe.flags(), listen_fd);
                continue;
            }
            let current = lp.conns[slot as usize].gen_ == gen_;
            match op {
                OP_RECV if current && lp.conns[slot as usize].open => {
                    lp.on_recv(slot, cqe.result(), cqe.flags())
                }
                // A stale recv may still carry a buffer: give it back.
                OP_RECV => {
                    if let Some(bid) = cqueue::buffer_select(cqe.flags()) {
                        lp.bufs.push(bid);
                    }
                }
                OP_SEND if current => lp.on_send(slot, cqe.result()),
                _ => {}
            }
        }
    }
}

/// Try to create a ring with the features the backend needs and say what
/// happened. EPERM or ENOSYS from io_uring_setup is what a seccomp profile
/// or the io_uring_disabled sysctl that blocks io_uring looks like.
pub fn probe() -> i32 {
    let sysctl = std::fs::read_to_string("/proc/sys/kernel/io_uring_disabled");
    let seccomp = std::fs::read_to_string("/proc/self/status")
        .ok()
        .and_then(|s| {
            s.lines()
                .find(|l| l.starts_with("Seccomp:"))
                .map(|l| l.split_whitespace().last().unwrap_or("").to_owned())
        });
    println!(
        "kernel.io_uring_disabled={} seccomp_mode={}",
        sysctl.as_deref().map_or("unknown", str::trim),
        seccomp.as_deref().unwrap_or("unknown")
    );
    let mut builder = IoUring::builder();
    builder.setup_single_issuer().setup_defer_taskrun();
    let ring = match builder.build(8) {
        Ok(ring) => ring,
        Err(err) => {
            println!("io_uring_setup: blocked ({err})");
            return 1;
        }
    };
    let mut probe = io_uring::Probe::new();
    if let Err(err) = ring.submitter().register_probe(&mut probe) {
        println!("io_uring_register: blocked ({err})");
        return 1;
    }
    let ops = [
        ("AcceptMulti", opcode::AcceptMulti::CODE),
        ("RecvMulti", opcode::RecvMulti::CODE),
        ("Send", opcode::Send::CODE),
        ("Close", opcode::Close::CODE),
    ];
    let missing: Vec<_> = ops
        .iter()
        .filter(|(_, code)| !probe.is_supported(*code))
        .map(|(n, _)| *n)
        .collect();
    if let Err(err) = BufRing::new(&ring) {
        println!("provided-buffer ring: unavailable ({err})");
        return 1;
    }
    if missing.is_empty() {
        println!("io_uring: available (setup, register, multishot accept/recv, buffer ring)");
        0
    } else {
        println!("io_uring: available but missing opcodes {missing:?}");
        1
    }
}
