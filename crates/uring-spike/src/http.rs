//! The request handling shared by all three backends, so that the only thing
//! that differs between them is how bytes reach and leave the socket.

use std::cell::{Cell, RefCell};
use std::time::{SystemTime, UNIX_EPOCH};

const BODY: &[u8] = br#"{"ok":true,"count":42}"#;
const NOT_FOUND: &[u8] = b"HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\ndate: ";

/// The `date` header value, refreshed at most once a second (as hyper does).
struct Date {
    secs: u64,
    text: [u8; 29],
}

thread_local! {
    /// Requests answered by this thread; reported by GET /stats so a
    /// benchmark can count requests over exactly its CPU sampling window.
    static SERVED: Cell<u64> = const { Cell::new(0) };

    static DATE: RefCell<Date> = const { RefCell::new(Date { secs: 0, text: [0; 29] }) };
}

fn with_date<R>(f: impl FnOnce(&[u8; 29]) -> R) -> R {
    DATE.with(|cell| {
        let mut date = cell.borrow_mut();
        let now = SystemTime::now();
        let secs = now.duration_since(UNIX_EPOCH).map_or(0, |d| d.as_secs());
        if secs != date.secs {
            date.secs = secs;
            date.text
                .copy_from_slice(httpdate::fmt_http_date(now).as_bytes());
        }
        f(&date.text)
    })
}

fn respond(found: bool, out: &mut Vec<u8>) {
    with_date(|date| {
        if found {
            out.extend_from_slice(
                b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: 22\r\ndate: ",
            );
            out.extend_from_slice(date);
            out.extend_from_slice(b"\r\n\r\n");
            out.extend_from_slice(BODY);
        } else {
            out.extend_from_slice(NOT_FOUND);
            out.extend_from_slice(date);
            out.extend_from_slice(b"\r\n\r\n");
        }
    });
}

fn stats(out: &mut Vec<u8>) {
    let body = SERVED.get().to_string();
    out.extend_from_slice(b"HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\ncontent-length: ");
    out.extend_from_slice(body.len().to_string().as_bytes());
    out.extend_from_slice(b"\r\n\r\n");
    out.extend_from_slice(body.as_bytes());
}

pub enum Served {
    /// `consumed` bytes of input were answered; any rest is a partial request.
    Ok { consumed: usize, close: bool },
    /// Malformed request: drop the connection.
    Bad,
}

/// Answer every complete request in `input` (pipelining included), appending
/// the responses to `out`. Only bodiless requests are supported: GET /json
/// gets the JSON body, GET /stats the number of requests answered so far,
/// anything else a 404.
pub fn serve(input: &[u8], out: &mut Vec<u8>) -> Served {
    let mut pos = 0;
    let mut close = false;
    while pos < input.len() && !close {
        let mut headers = [httparse::EMPTY_HEADER; 32];
        let mut req = httparse::Request::new(&mut headers);
        match req.parse(&input[pos..]) {
            Ok(httparse::Status::Complete(n)) => {
                close = req.version == Some(0)
                    || req.headers.iter().any(|h| {
                        h.name.eq_ignore_ascii_case("connection")
                            && h.value.eq_ignore_ascii_case(b"close")
                    });
                if req.method == Some("GET") && req.path == Some("/stats") {
                    stats(out);
                } else {
                    respond(req.method == Some("GET") && req.path == Some("/json"), out);
                }
                SERVED.set(SERVED.get() + 1);
                pos += n;
            }
            Ok(httparse::Status::Partial) => break,
            Err(_) => return Served::Bad,
        }
    }
    Served::Ok {
        consumed: pos,
        close,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn answers_pipelined_requests_and_keeps_partial_tail() {
        let mut out = Vec::new();
        let input = b"GET /json HTTP/1.1\r\nhost: x\r\n\r\nGET /nope HTTP/1.1\r\n\r\nGET /js";
        let Served::Ok { consumed, close } = serve(input, &mut out) else {
            panic!()
        };
        assert_eq!(consumed, input.len() - 7);
        assert!(!close);
        let text = String::from_utf8(out).unwrap();
        assert!(text.starts_with("HTTP/1.1 200 OK\r\n"));
        assert!(text.contains("\r\n\r\n{\"ok\":true,\"count\":42}HTTP/1.1 404"));
    }

    #[test]
    fn honours_connection_close() {
        let mut out = Vec::new();
        let input = b"GET /json HTTP/1.1\r\nConnection: close\r\n\r\nGET /json HTTP/1.1\r\n\r\n";
        let Served::Ok { consumed, close } = serve(input, &mut out) else {
            panic!()
        };
        assert!(close);
        assert_eq!(consumed, 41);
    }

    #[test]
    fn counts_requests() {
        let mut out = Vec::new();
        serve(
            b"GET /json HTTP/1.1\r\n\r\nGET /json HTTP/1.1\r\n\r\n",
            &mut out,
        );
        out.clear();
        serve(b"GET /stats HTTP/1.1\r\n\r\n", &mut out);
        assert!(
            out.ends_with(b"\r\n\r\n2"),
            "{}",
            String::from_utf8_lossy(&out)
        );
    }

    #[test]
    fn rejects_garbage() {
        assert!(matches!(
            serve(b"\x00\x01 nonsense\r\n\r\n", &mut Vec::new()),
            Served::Bad
        ));
    }
}
