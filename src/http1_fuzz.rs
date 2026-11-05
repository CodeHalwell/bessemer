//! Randomised robustness checks for the HTTP/1 request parser and body
//! decoders.
//!
//! Each iteration mutates a seed request (byte flips, inserted framing
//! tokens, deletions, duplications) and checks properties that would expose
//! a parser bug or a request-smuggling ambiguity:
//!
//! - parsing never panics, whatever the input;
//! - a head is never accepted early: every prefix of the input parses as
//!   "need more bytes" or as exactly the same request as the whole input;
//! - an accepted head never carries both `Content-Length` and
//!   `Transfer-Encoding`, and one with `Transfer-Encoding` ends in `chunked`;
//! - decoding a body gives the same bytes, the same verdict and the same
//!   leftover however the input is split across reads.
//!
//! `BESSEMER_FUZZ_ITERATIONS` raises the iteration count (default 20000);
//! `BESSEMER_FUZZ_SEED` fixes the seed to replay a failure.

use super::*;

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        // xorshift64*
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    fn below(&mut self, bound: usize) -> usize {
        (self.next() % bound.max(1) as u64) as usize
    }
}

const SEEDS: &[&[u8]] = &[
    b"GET / HTTP/1.1\r\nHost: a\r\n\r\n",
    b"GET /items?id=1 HTTP/1.0\r\nConnection: keep-alive\r\n\r\n",
    b"POST /p HTTP/1.1\r\nHost: a\r\nContent-Length: 5\r\n\r\nhelloGET / HTTP/1.1\r\n\r\n",
    b"POST /p HTTP/1.1\r\nHost: a\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n",
    b"POST /p HTTP/1.1\r\nTransfer-Encoding: gzip, chunked\r\n\r\n3;ext=1\r\nabc\r\n0\r\nTrailer: x\r\n\r\n",
    b"PUT /p HTTP/1.1\r\nContent-Length: 3\r\nContent-Length: 3\r\nExpect: 100-continue\r\n\r\nabc",
    b"POST /p HTTP/1.1\r\nContent-Length: 0\r\n\r\n",
    b"GET /ws HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n",
    b"POST /p HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\na\r\n0123456789\r\n1\r\nx\r\n0\r\n\r\n",
];

const TOKENS: &[&[u8]] = &[
    b"\r\n",
    b"\n",
    b"\r",
    b" ",
    b"\t",
    b",",
    b";",
    b":",
    b"\0",
    b"\x7f",
    b"\xff",
    b"0",
    b"-1",
    b"+5",
    b"ffffffffffffffff",
    b"10000000000000000",
    b"chunked",
    b"Chunked ",
    b"identity",
    b"Transfer-Encoding: chunked\r\n",
    b"Transfer-Encoding: x\r\n",
    b"Transfer-Encoding : chunked\r\n",
    b" Transfer-Encoding: chunked\r\n",
    b"Content-Length: 4\r\n",
    b"Content-Length: 4, 5\r\n",
    b"Content-Length: 00004\r\n",
    b"Content-Length:\t4\r\n",
    b"Connection: close\r\n",
    b"Upgrade: h2c\r\n",
    b"Expect: 100-continue\r\n",
    b"HTTP/1.1",
    b"HTTP/1.0",
    b"0\r\n\r\n",
];

fn mutate(rng: &mut Rng, input: &mut Vec<u8>) {
    for _ in 0..1 + rng.below(4) {
        match rng.below(5) {
            0 if !input.is_empty() => {
                let at = rng.below(input.len());
                input[at] ^= 1 << rng.below(8);
            }
            1 => {
                let at = rng.below(input.len() + 1);
                let token = TOKENS[rng.below(TOKENS.len())];
                input.splice(at..at, token.iter().copied());
            }
            2 if !input.is_empty() => {
                let at = rng.below(input.len());
                let end = (at + 1 + rng.below(8)).min(input.len());
                input.drain(at..end);
            }
            3 if !input.is_empty() => {
                let at = rng.below(input.len());
                let end = (at + 1 + rng.below(16)).min(input.len());
                let copy = input[at..end].to_vec();
                let to = rng.below(input.len() + 1);
                input.splice(to..to, copy);
            }
            _ => {
                let at = rng.below(input.len() + 1);
                input.insert(at, rng.next() as u8);
            }
        }
    }
}

fn config() -> Config {
    Config {
        header_timeout: Duration::from_secs(1),
        max_headers: 16,
        max_header_size: 4096,
    }
}

/// What a parse produced, comparable across prefixes.
#[derive(Debug, PartialEq)]
enum Outcome {
    Partial,
    Error(StatusCode),
    Accepted {
        consumed: usize,
        method: Method,
        uri: String,
        headers: Vec<(String, Vec<u8>)>,
        framing: Framing,
        keep_alive: bool,
        upgrade: bool,
    },
}

fn parse(input: &[u8]) -> Outcome {
    let mut buffer = BytesMut::from(input);
    match parse_head(&mut buffer, &config()) {
        Ok(None) => Outcome::Partial,
        Err(status) => Outcome::Error(status),
        Ok(Some(parsed)) => Outcome::Accepted {
            consumed: input.len() - buffer.len(),
            method: parsed.request.method().clone(),
            uri: parsed.request.uri().to_string(),
            headers: parsed
                .request
                .headers()
                .iter()
                .map(|(name, value)| (name.to_string(), value.as_bytes().to_vec()))
                .collect(),
            framing: parsed.framing,
            keep_alive: parsed.keep_alive,
            upgrade: parsed.upgrade.is_some(),
        },
    }
}

/// Decode a body from `input` delivered in the given pieces. Returns the
/// body, whether it completed, whether it failed, and the leftover bytes.
fn decode(framing: Framing, input: &[u8], cuts: &[usize]) -> (Vec<u8>, bool, bool, Vec<u8>) {
    let mut decoder = match framing {
        Framing::Length(length) => Decoder::Length(length),
        Framing::Chunked => Decoder::Chunked(ChunkState::Size),
        Framing::Empty => return (Vec::new(), true, false, input.to_vec()),
    };
    let mut buffer = BytesMut::new();
    let mut body = Vec::new();
    let mut start = 0;
    let mut ends: Vec<usize> = cuts
        .iter()
        .copied()
        .filter(|cut| *cut < input.len())
        .collect();
    ends.push(input.len());
    for end in ends {
        if end < start {
            continue;
        }
        buffer.extend_from_slice(&input[start..end]);
        start = end;
        loop {
            match decoder.decode(&mut buffer) {
                Ok(Some(data)) => {
                    assert!(!data.is_empty(), "decoder produced an empty frame");
                    body.extend_from_slice(&data);
                    if decoder.finished() {
                        return (body, true, false, leftover(&buffer, &input[start..]));
                    }
                }
                Ok(None) if decoder.finished() => {
                    return (body, true, false, leftover(&buffer, &input[start..]));
                }
                Ok(None) => break,
                Err(_) => return (body, false, true, Vec::new()),
            }
        }
    }
    (body, false, false, buffer.to_vec())
}

/// Unconsumed bytes: what the decoder left plus input not yet delivered.
fn leftover(buffer: &[u8], undelivered: &[u8]) -> Vec<u8> {
    [buffer, undelivered].concat()
}

fn check(rng: &mut Rng, input: &[u8]) {
    let whole = parse(input);
    if let Outcome::Accepted {
        consumed,
        headers,
        framing,
        ..
    } = &whole
    {
        let has = |name: &str| headers.iter().any(|(key, _)| key == name);
        assert!(
            !(has("content-length") && has("transfer-encoding")),
            "accepted both framings: {input:?}"
        );
        if has("transfer-encoding") {
            assert_eq!(
                *framing,
                Framing::Chunked,
                "non-chunked TE accepted: {input:?}"
            );
        }
        // No prefix of the head may parse as a different request.
        for _ in 0..4 {
            let cut = rng.below(*consumed);
            let prefix = parse(&input[..cut]);
            assert!(
                prefix == Outcome::Partial || prefix == whole,
                "prefix of {cut} bytes parsed differently: {prefix:?} vs {whole:?} for {input:?}"
            );
        }
        let rest = &input[*consumed..];
        let reference = decode(*framing, rest, &[]);
        for _ in 0..3 {
            let mut cuts: Vec<usize> = (0..rng.below(6))
                .map(|_| rng.below(rest.len() + 1))
                .collect();
            cuts.sort_unstable();
            let split = decode(*framing, rest, &cuts);
            assert_eq!(
                split, reference,
                "split decode differs at {cuts:?} for {input:?}"
            );
        }
        // Byte by byte, the hardest split.
        let cuts: Vec<usize> = (1..rest.len()).collect();
        assert_eq!(
            decode(*framing, rest, &cuts),
            reference,
            "bytewise decode differs: {input:?}"
        );
    }
}

#[test]
fn mutated_requests_parse_consistently() {
    let iterations: usize = std::env::var("BESSEMER_FUZZ_ITERATIONS")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(20_000);
    let seed: u64 = std::env::var("BESSEMER_FUZZ_SEED")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(0x5eed_b355_e3e7);
    let mut rng = Rng(seed | 1);
    for _ in 0..iterations {
        let mut input = SEEDS[rng.below(SEEDS.len())].to_vec();
        mutate(&mut rng, &mut input);
        check(&mut rng, &input);
    }
}

#[test]
fn chunk_sizes_never_overflow_or_accept_signs() {
    for line in [
        &b"10000000000000000\r\n"[..],
        b"-1\r\n",
        b"+1\r\n",
        b"0x10\r\n",
        b" 1\r\n",
        b"1 \r\n",
        b"\r\n",
        b"1;a\rb\r\n",
        b"1;a\0\r\n",
        b"1;a\nb\r\n",
    ] {
        let parsed = parse_chunk_size(line);
        if line == b"1 \r\n" {
            assert_eq!(parsed.unwrap(), 1);
        } else {
            assert!(parsed.is_err(), "{line:?} accepted");
        }
    }
}

#[test]
fn control_characters_in_chunk_metadata_are_refused() {
    for body in [
        &b"1;ext=\"a\rb\"\r\nx\r\n0\r\n\r\n"[..],
        b"1\r\nx\r\n0\r\nTrailer: a\rb\r\n\r\n",
        b"1\r\nx\r\n0\r\nTrailer: a\0\r\n\r\n",
    ] {
        let (_, complete, failed, _) = decode(Framing::Chunked, body, &[]);
        assert!(failed && !complete, "{body:?} accepted");
    }
    let (data, complete, failed, _) = decode(
        Framing::Chunked,
        b"1;name=\"tab\there\"\r\nx\r\n0\r\nTrailer: a\tb\r\n\r\n",
        &[],
    );
    assert_eq!(
        (data.as_slice(), complete, failed),
        (&b"x"[..], true, false)
    );
}
