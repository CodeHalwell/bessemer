//! WebSocket per-message compression (RFC 7692, `permessage-deflate`).
//!
//! Both directions run without context takeover: every message is compressed
//! and decompressed on its own, so a session keeps no compression window
//! between messages. Outbound compressors are created per message and dropped
//! after it; the inbound decompressor is reset between messages.

use flate2::{Compress, Compression, Decompress, FlushCompress, FlushDecompress, Status};

/// The empty stored block a sync flush ends with, which RFC 7692 strips from
/// each compressed message and the receiver adds back.
const TAIL: [u8; 4] = [0x00, 0x00, 0xff, 0xff];
/// Messages shorter than this are sent uncompressed; the frame overhead and
/// compressor setup outweigh what they could save.
pub(crate) const MIN_COMPRESSED_SIZE: usize = 128;

/// What one accepted offer settled.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Agreement {
    /// The LZ77 window this server compresses with (`server_max_window_bits`).
    server_window_bits: u8,
    /// The `Sec-WebSocket-Extensions` response value.
    pub(crate) response: String,
}

/// Choose the first offer this server can honour, in the client's order of
/// preference. Offers with unknown or repeated parameters, or a server window
/// below 9 bits (zlib cannot produce an 8-bit window), are passed over.
pub(crate) fn negotiate<'a>(offers: impl Iterator<Item = &'a str>) -> Option<Agreement> {
    offers
        .flat_map(|value| value.split(','))
        .find_map(|offer| accept(offer.trim()))
}

fn accept(offer: &str) -> Option<Agreement> {
    let mut parts = offer.split(';').map(str::trim);
    if !parts.next()?.eq_ignore_ascii_case("permessage-deflate") {
        return None;
    }
    let mut seen = Vec::new();
    let mut server_window_bits = None;
    let mut client_window_bits_offered = false;
    for part in parts {
        let (name, value) = match part.split_once('=') {
            Some((name, value)) => (name.trim(), Some(value.trim().trim_matches('"'))),
            None => (part, None),
        };
        let name = name.to_ascii_lowercase();
        if seen.contains(&name) {
            return None;
        }
        match (name.as_str(), value) {
            ("server_no_context_takeover" | "client_no_context_takeover", None) => {}
            ("server_max_window_bits", Some(value)) => {
                server_window_bits = Some(window_bits(value)?);
            }
            ("client_max_window_bits", None) => client_window_bits_offered = true,
            ("client_max_window_bits", Some(value)) => {
                window_bits(value)?;
                client_window_bits_offered = true;
            }
            _ => return None,
        }
        seen.push(name);
    }
    if server_window_bits == Some(8) {
        return None;
    }
    let mut response =
        String::from("permessage-deflate; server_no_context_takeover; client_no_context_takeover");
    if let Some(bits) = server_window_bits {
        response.push_str(&format!("; server_max_window_bits={bits}"));
    }
    // Inflating with the largest window accepts whatever the client uses, so
    // its window is never limited; the parameter is only acknowledged.
    if client_window_bits_offered {
        response.push_str("; client_max_window_bits=15");
    }
    Some(Agreement {
        server_window_bits: server_window_bits.unwrap_or(15),
        response,
    })
}

fn window_bits(value: &str) -> Option<u8> {
    if value.is_empty() || value.len() > 2 || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let bits: u8 = value.parse().ok()?;
    (8..=15).contains(&bits).then_some(bits)
}

impl Agreement {
    /// The compressed payload for one outbound message, or `None` when it is
    /// too short to be worth compressing or compression would not shrink it.
    pub(crate) fn compress(&self, data: &[u8]) -> Option<Vec<u8>> {
        if data.len() < MIN_COMPRESSED_SIZE {
            return None;
        }
        let mut compressor =
            Compress::new_with_window_bits(Compression::default(), false, self.server_window_bits);
        let mut output = Vec::with_capacity(data.len() / 2 + 64);
        loop {
            let consumed = compressor.total_in() as usize;
            if output.len() == output.capacity() {
                output.reserve(output.capacity().max(64));
            }
            compressor
                .compress_vec(&data[consumed..], &mut output, FlushCompress::Sync)
                .ok()?;
            // Done once every byte is in and the flush fit in the buffer.
            if compressor.total_in() as usize == data.len() && output.len() < output.capacity() {
                break;
            }
        }
        if !output.ends_with(&TAIL) {
            return None;
        }
        output.truncate(output.len() - TAIL.len());
        (output.len() < data.len()).then_some(output)
    }

    pub(crate) fn inflater(&self) -> Inflater {
        Inflater(None)
    }
}

/// Decompresses inbound messages. Its state is allocated on the first
/// compressed message a session receives and reset for each one after.
pub(crate) struct Inflater(Option<Decompress>);

/// Why an inbound message could not be decompressed.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum InflateError {
    /// The decompressed message is larger than the message limit (1009).
    TooBig,
    /// The payload is not a valid DEFLATE stream (1007).
    Invalid,
}

impl Inflater {
    pub(crate) fn inflate(
        &mut self,
        payload: &[u8],
        limit: usize,
    ) -> Result<Vec<u8>, InflateError> {
        let decompressor = self
            .0
            .get_or_insert_with(|| Decompress::new_with_window_bits(false, 15));
        // No context takeover: every message starts from an empty window.
        decompressor.reset(false);
        let grow = |length: usize| length.min(limit.saturating_add(1)).max(64);
        let mut output = Vec::with_capacity(grow(payload.len().saturating_mul(2)));
        for (input, last) in [(payload, false), (&TAIL[..], true)] {
            let mut consumed = 0;
            loop {
                if output.len() == output.capacity() {
                    output.reserve(grow(output.len()));
                }
                let (read, written) = (decompressor.total_in(), output.len());
                let status = decompressor
                    .decompress_vec(&input[consumed..], &mut output, FlushDecompress::Sync)
                    .map_err(|_| InflateError::Invalid)?;
                consumed += (decompressor.total_in() - read) as usize;
                if output.len() > limit {
                    return Err(InflateError::TooBig);
                }
                if status == Status::StreamEnd {
                    // A final block (BFINAL) may end the message early; only
                    // the appended tail may follow it.
                    if !last && consumed < input.len() {
                        return Err(InflateError::Invalid);
                    }
                    return Ok(output);
                }
                // With room left over, everything available has been written.
                if consumed == input.len() && output.len() < output.capacity() {
                    break;
                }
                if decompressor.total_in() == read && output.len() == written {
                    return Err(InflateError::Invalid);
                }
            }
        }
        Ok(output)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn agreement(offer: &str) -> Option<Agreement> {
        negotiate(std::iter::once(offer))
    }

    #[test]
    fn offers_are_accepted_in_order_and_bad_ones_passed_over() {
        let plain = agreement("permessage-deflate").unwrap();
        assert_eq!(
            plain.response,
            "permessage-deflate; server_no_context_takeover; client_no_context_takeover"
        );
        assert_eq!(plain.server_window_bits, 15);
        // The browser default: client_max_window_bits without a value.
        let browser = agreement("permessage-deflate; client_max_window_bits").unwrap();
        assert!(browser.response.ends_with("; client_max_window_bits=15"));
        let limited = agreement("permessage-deflate; server_max_window_bits=10").unwrap();
        assert_eq!(limited.server_window_bits, 10);
        assert!(limited.response.contains("server_max_window_bits=10"));
        for bad in [
            "x-webkit-deflate-frame",
            "permessage-deflate; server_max_window_bits=8",
            "permessage-deflate; server_max_window_bits=16",
            "permessage-deflate; server_max_window_bits",
            "permessage-deflate; server_max_window_bits=010",
            "permessage-deflate; client_max_window_bits=7",
            "permessage-deflate; server_no_context_takeover; server_no_context_takeover",
            "permessage-deflate; server_no_context_takeover=1",
            "permessage-deflate; unknown",
        ] {
            assert_eq!(agreement(bad), None, "{bad}");
        }
        // The first acceptable offer wins, across header values too.
        let chosen = negotiate(
            [
                "permessage-deflate; server_max_window_bits=8, permessage-deflate; server_max_window_bits=11",
                "permessage-deflate",
            ]
            .into_iter(),
        )
        .unwrap();
        assert_eq!(chosen.server_window_bits, 11);
    }

    #[test]
    fn messages_round_trip_without_context() {
        let agreement = agreement("permessage-deflate").unwrap();
        let mut inflater = agreement.inflater();
        let text = "hello websocket compression ".repeat(64);
        for _ in 0..3 {
            let compressed = agreement.compress(text.as_bytes()).unwrap();
            assert!(compressed.len() < text.len() / 4);
            assert!(!compressed.ends_with(&TAIL));
            assert_eq!(
                inflater.inflate(&compressed, 1 << 20).unwrap(),
                text.as_bytes()
            );
        }
        // Short or incompressible messages are left alone.
        assert_eq!(agreement.compress(b"short"), None);
        let mut state = 0x9e37_79b9_7f4a_7c15u64;
        let noise: Vec<u8> = (0..4096)
            .map(|_| {
                state ^= state << 13;
                state ^= state >> 7;
                state ^= state << 17;
                state as u8
            })
            .collect();
        assert_eq!(agreement.compress(&noise), None);
    }

    #[test]
    fn the_rfc_7692_example_inflates() {
        // RFC 7692 section 7.2.3.1: "Hello" compressed, without the tail.
        let mut inflater = agreement("permessage-deflate").unwrap().inflater();
        let payload = [0xf2, 0x48, 0xcd, 0xc9, 0xc9, 0x07, 0x00];
        assert_eq!(inflater.inflate(&payload, 1024).unwrap(), b"Hello");
        // An empty message is a single empty stored block.
        assert_eq!(inflater.inflate(&[0x00], 1024).unwrap(), b"");
    }

    #[test]
    fn decompression_is_bounded_and_rejects_garbage() {
        let agreement = agreement("permessage-deflate").unwrap();
        let bomb = vec![0u8; 1 << 20];
        let compressed = agreement.compress(&bomb).unwrap();
        assert!(compressed.len() < 4096);
        let mut inflater = agreement.inflater();
        assert_eq!(
            inflater.inflate(&compressed, 64 * 1024),
            Err(InflateError::TooBig)
        );
        assert_eq!(
            inflater.inflate(&compressed, 1 << 20).unwrap().len(),
            1 << 20
        );
        assert_eq!(
            inflater.inflate(&[0xff, 0xff, 0xff, 0xff, 0xff], 1024),
            Err(InflateError::Invalid)
        );
        // The inflater recovers for the next message after a failure.
        let text = "recovered ".repeat(40);
        let compressed = agreement.compress(text.as_bytes()).unwrap();
        assert_eq!(
            inflater.inflate(&compressed, 1 << 20).unwrap(),
            text.as_bytes()
        );
    }

    #[test]
    fn a_smaller_window_still_round_trips() {
        let agreement = agreement("permessage-deflate; server_max_window_bits=9").unwrap();
        let text: String = (0..2000).map(|index| format!("{index:05} ")).collect();
        let compressed = agreement.compress(text.as_bytes()).unwrap();
        let mut inflater = agreement.inflater();
        assert_eq!(
            inflater.inflate(&compressed, 1 << 20).unwrap(),
            text.as_bytes()
        );
    }
}
