//! A hand-rolled edge-triggered epoll loop on mio: the leanest readiness-based
//! design, to separate what io_uring saves from what tokio's machinery costs.

use crate::http::{Served, serve};
use mio::net::{TcpListener, TcpStream};
use mio::{Events, Interest, Poll, Token};
use std::io::{self, Read, Write};

const LISTENER: Token = Token(usize::MAX);

struct Conn {
    stream: TcpStream,
    input: Vec<u8>,
    filled: usize,
    out: Vec<u8>,
    /// Bytes of `out` already written.
    sent: usize,
    close_after_write: bool,
}

pub fn run(listener: std::net::TcpListener) -> io::Result<()> {
    let mut poll = Poll::new()?;
    let mut events = Events::with_capacity(1024);
    let mut listener = TcpListener::from_std(listener);
    poll.registry()
        .register(&mut listener, LISTENER, Interest::READABLE)?;
    let mut conns: Vec<Option<Conn>> = Vec::new();
    let mut free: Vec<usize> = Vec::new();

    loop {
        if let Err(err) = poll.poll(&mut events, None) {
            if err.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(err);
        }
        for event in events.iter() {
            if event.token() == LISTENER {
                loop {
                    match listener.accept() {
                        Ok((mut stream, _)) => {
                            let slot = free.pop().unwrap_or_else(|| {
                                conns.push(None);
                                conns.len() - 1
                            });
                            poll.registry().register(
                                &mut stream,
                                Token(slot),
                                Interest::READABLE | Interest::WRITABLE,
                            )?;
                            conns[slot] = Some(Conn {
                                stream,
                                input: vec![0; 4096],
                                filled: 0,
                                out: Vec::with_capacity(4096),
                                sent: 0,
                                close_after_write: false,
                            });
                        }
                        Err(err) if err.kind() == io::ErrorKind::WouldBlock => break,
                        Err(err) if err.kind() == io::ErrorKind::Interrupted => {}
                        Err(_) => break,
                    }
                }
                continue;
            }
            let slot = event.token().0;
            let Some(conn) = conns[slot].as_mut() else {
                continue;
            };
            let keep = (!event.is_readable() || on_readable(conn)) && flush(conn);
            if !keep {
                let mut conn = conns[slot].take().unwrap();
                let _ = poll.registry().deregister(&mut conn.stream);
                free.push(slot);
            }
        }
    }
}

/// Read until the socket would block, answering what arrived. False to close.
fn on_readable(conn: &mut Conn) -> bool {
    loop {
        // `input` is zero-filled storage (initialised once per connection,
        // grown rarely) and `filled` counts the bytes received in it.
        if conn.input.len() - conn.filled < 1024 {
            conn.input.resize(conn.input.len() + 4096, 0);
        }
        let read = conn.stream.read(&mut conn.input[conn.filled..]);
        if let Ok(n) = read {
            conn.filled += n;
        }
        match read {
            // EOF (perhaps a half-close): send what is queued, then close.
            Ok(0) => {
                conn.close_after_write = true;
                break;
            }
            Ok(_) => {}
            Err(err) if err.kind() == io::ErrorKind::WouldBlock => break,
            Err(err) if err.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) => return false,
        }
        if conn.close_after_write {
            continue;
        }
        let Served::Ok { consumed, close } = serve(&conn.input[..conn.filled], &mut conn.out)
        else {
            return false;
        };
        conn.input.copy_within(consumed..conn.filled, 0);
        conn.filled -= consumed;
        conn.close_after_write = close;
    }
    true
}

/// Write pending output until done or the socket would block. False to close.
fn flush(conn: &mut Conn) -> bool {
    while conn.sent < conn.out.len() {
        match conn.stream.write(&conn.out[conn.sent..]) {
            Ok(n) => conn.sent += n,
            Err(err) if err.kind() == io::ErrorKind::WouldBlock => return true,
            Err(err) if err.kind() == io::ErrorKind::Interrupted => {}
            Err(_) => return false,
        }
    }
    conn.out.clear();
    conn.sent = 0;
    !conn.close_after_write
}
