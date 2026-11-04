//! Native form decoding: `application/x-www-form-urlencoded` and a strict,
//! single-pass `multipart/form-data` parser (RFC 7578) over the buffered body.
//!
//! The parser never copies part data: it returns byte ranges into the body.
//! Limits on part count and header size bound the work an untrusted body can
//! cause, on top of the server's request body limit.

use memchr::memmem;
use std::ops::Range;

/// At most this many parts (fields and files) per request, as in Starlette.
pub(crate) const MAX_PARTS: usize = 1000;
/// Bytes of headers allowed for one part.
const MAX_PART_HEADER_BYTES: usize = 8192;
/// Header lines allowed for one part.
const MAX_PART_HEADERS: usize = 16;

/// One decoded part.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct Part {
    pub(crate) name: String,
    /// Present for file parts, even when empty (`filename=""`).
    pub(crate) filename: Option<String>,
    pub(crate) content_type: Option<String>,
    /// Every part header, with lowercase names.
    pub(crate) headers: Vec<(String, String)>,
    pub(crate) data: Range<usize>,
}

/// An uploaded file bound to a handler parameter, handed to Python as-is.
pub(crate) struct Upload {
    /// The handler argument key it binds to.
    pub(crate) key: String,
    /// Several files bind as a list (`list[UploadFile]`).
    pub(crate) many: bool,
    /// `bytes` parameters receive the contents only.
    pub(crate) raw: bool,
    pub(crate) filename: String,
    pub(crate) content_type: Option<String>,
    pub(crate) headers: Vec<(String, String)>,
    pub(crate) data: bytes::Bytes,
}

/// The form media types the server decodes.
pub(crate) enum FormKind {
    UrlEncoded,
    Multipart(String),
}

/// Classify a Content-Type header for a form route.
pub(crate) fn form_kind(content_type: &str) -> Option<FormKind> {
    let media: mime::Mime = content_type.parse().ok()?;
    match (media.type_(), media.subtype().as_str()) {
        (mime::APPLICATION, "x-www-form-urlencoded") => Some(FormKind::UrlEncoded),
        (mime::MULTIPART, "form-data") => {
            let boundary = media.get_param(mime::BOUNDARY)?.as_str();
            // RFC 2046: 1 to 70 characters, not ending in a space.
            if boundary.is_empty()
                || boundary.len() > 70
                || boundary.ends_with(' ')
                || !boundary
                    .bytes()
                    .all(|byte| byte.is_ascii_graphic() || byte == b' ')
            {
                return None;
            }
            Some(FormKind::Multipart(boundary.to_owned()))
        }
        _ => None,
    }
}

/// Decode an urlencoded body into ordered fields.
pub(crate) fn parse_urlencoded(body: &[u8]) -> Result<Vec<(String, String)>, &'static str> {
    let fields: Vec<(String, String)> =
        serde_urlencoded::from_bytes(body).map_err(|_| "Invalid form body")?;
    if fields.len() > MAX_PARTS {
        return Err("Too many form fields");
    }
    Ok(fields)
}

/// Decode a multipart/form-data body into parts that borrow from `body`.
pub(crate) fn parse_multipart(body: &[u8], boundary: &str) -> Result<Vec<Part>, &'static str> {
    const MALFORMED: &str = "Invalid multipart body";
    // The first delimiter may start the body; later ones follow a CRLF.
    let first = format!("--{boundary}");
    let delimiter = format!("\r\n--{boundary}");
    let finder = memmem::Finder::new(delimiter.as_bytes());
    let mut position = if body.starts_with(first.as_bytes()) {
        first.len()
    } else {
        // Skip a preamble.
        finder.find(body).ok_or(MALFORMED)? + delimiter.len()
    };
    let mut parts = Vec::new();
    loop {
        let rest = &body[position..];
        if rest.starts_with(b"--") {
            // Close delimiter; anything after it is an ignored epilogue.
            return Ok(parts);
        }
        // Transport padding (spaces and tabs) may follow a delimiter.
        let padding = rest
            .iter()
            .take_while(|&&byte| byte == b' ' || byte == b'\t')
            .count();
        if !rest[padding..].starts_with(b"\r\n") {
            return Err(MALFORMED);
        }
        position += padding + 2;
        if parts.len() == MAX_PARTS {
            return Err("Too many multipart parts");
        }
        let header_end = memmem::find(&body[position..], b"\r\n\r\n");
        let (headers, data_start) = match header_end {
            // A part with no headers at all starts with the blank line.
            _ if body[position..].starts_with(b"\r\n") => (&body[position..position], position + 2),
            Some(end) if end <= MAX_PART_HEADER_BYTES => {
                (&body[position..position + end], position + end + 4)
            }
            Some(_) => return Err("Multipart part headers are too large"),
            None => return Err(MALFORMED),
        };
        let data_end = position_of(&finder, body, data_start).ok_or(MALFORMED)?;
        parts.push(part(headers, data_start..data_end)?);
        position = data_end + delimiter.len();
    }
}

fn position_of(finder: &memmem::Finder<'_>, body: &[u8], from: usize) -> Option<usize> {
    finder.find(&body[from..]).map(|offset| from + offset)
}

fn part(block: &[u8], data: Range<usize>) -> Result<Part, &'static str> {
    const MALFORMED: &str = "Invalid multipart part headers";
    let block = std::str::from_utf8(block).map_err(|_| MALFORMED)?;
    let mut headers = Vec::new();
    if !block.is_empty() {
        for line in block.split("\r\n") {
            if headers.len() == MAX_PART_HEADERS {
                return Err("Too many multipart part headers");
            }
            let (name, value) = line.split_once(':').ok_or(MALFORMED)?;
            if name.is_empty()
                || !name
                    .bytes()
                    .all(|byte| byte.is_ascii_graphic() && byte != b':')
                || value.contains(['\r', '\n', '\0'])
            {
                return Err(MALFORMED);
            }
            headers.push((name.to_ascii_lowercase(), value.trim().to_owned()));
        }
    }
    let mut disposition = None;
    let mut content_type = None;
    for (name, value) in &headers {
        let slot = match name.as_str() {
            "content-disposition" => &mut disposition,
            "content-type" => &mut content_type,
            _ => continue,
        };
        if slot.replace(value.clone()).is_some() {
            return Err(MALFORMED);
        }
    }
    let disposition = disposition.ok_or("A multipart part has no Content-Disposition")?;
    let (name, filename) = parse_disposition(&disposition).ok_or(MALFORMED)?;
    Ok(Part {
        name,
        filename,
        content_type,
        headers,
        data,
    })
}

/// Parse `form-data; name="..."; filename="..."`. Returns the name and the
/// optional filename. `filename*` (RFC 5987) wins over `filename`.
fn parse_disposition(value: &str) -> Option<(String, Option<String>)> {
    let mut rest = value.trim_start();
    let kind_end = rest.find(';').unwrap_or(rest.len());
    if !rest[..kind_end].trim().eq_ignore_ascii_case("form-data") {
        return None;
    }
    rest = &rest[kind_end..];
    let mut name = None;
    let mut filename = None;
    let mut extended = None;
    while let Some(after) = rest.strip_prefix(';') {
        let after = after.trim_start();
        if after.is_empty() {
            break;
        }
        let equals = after.find('=')?;
        let key = after[..equals].trim().to_ascii_lowercase();
        let (value, remaining) = parameter_value(&after[equals + 1..])?;
        rest = remaining.trim_start();
        let slot = match key.as_str() {
            "name" => &mut name,
            "filename" => &mut filename,
            "filename*" => &mut extended,
            _ => continue,
        };
        if slot.replace(value).is_some() {
            return None;
        }
    }
    if !rest.is_empty() {
        return None;
    }
    let name = name.filter(|name| !name.is_empty())?;
    let filename = match extended {
        Some(value) => Some(decode_extended(&value)?),
        None => filename,
    };
    Some((name, filename))
}

/// A token or quoted string, and the text after it.
fn parameter_value(input: &str) -> Option<(String, &str)> {
    let input = input.trim_start();
    if let Some(quoted) = input.strip_prefix('"') {
        let mut value = String::new();
        let mut characters = quoted.char_indices();
        while let Some((index, character)) = characters.next() {
            match character {
                '"' => return Some((value, &quoted[index + 1..])),
                '\\' => value.push(characters.next()?.1),
                _ => value.push(character),
            }
        }
        None
    } else {
        let end = input.find(';').unwrap_or(input.len());
        let token = input[..end].trim();
        if token.is_empty() {
            return None;
        }
        Some((token.to_owned(), &input[end..]))
    }
}

/// `UTF-8''percent-encoded` from RFC 5987.
fn decode_extended(value: &str) -> Option<String> {
    let mut pieces = value.splitn(3, '\'');
    let charset = pieces.next()?;
    let _language = pieces.next()?;
    let encoded = pieces.next()?;
    if !charset.eq_ignore_ascii_case("utf-8") {
        return None;
    }
    let mut bytes = Vec::with_capacity(encoded.len());
    let mut input = encoded.bytes();
    while let Some(byte) = input.next() {
        if byte == b'%' {
            let high = (input.next()? as char).to_digit(16)?;
            let low = (input.next()? as char).to_digit(16)?;
            bytes.push((high * 16 + low) as u8);
        } else {
            bytes.push(byte);
        }
    }
    String::from_utf8(bytes).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn body(parts: &[&str]) -> Vec<u8> {
        let mut output = String::new();
        for part in parts {
            output.push_str("--XyZ\r\n");
            output.push_str(part);
            output.push_str("\r\n");
        }
        output.push_str("--XyZ--\r\n");
        output.into_bytes()
    }

    #[test]
    fn content_types_are_classified() {
        assert!(matches!(
            form_kind("application/x-www-form-urlencoded; charset=utf-8"),
            Some(FormKind::UrlEncoded)
        ));
        assert!(matches!(
            form_kind("multipart/form-data; boundary=\"a b\""),
            Some(FormKind::Multipart(boundary)) if boundary == "a b"
        ));
        for invalid in [
            "application/json",
            "multipart/form-data",
            "multipart/form-data; boundary=",
            "multipart/mixed; boundary=x",
        ] {
            assert!(form_kind(invalid).is_none(), "{invalid}");
        }
        let long = format!("multipart/form-data; boundary={}", "a".repeat(71));
        assert!(form_kind(&long).is_none());
    }

    #[test]
    fn fields_and_files_borrow_from_the_body() {
        let data = body(&[
            "Content-Disposition: form-data; name=\"title\"\r\n\r\nHello",
            "Content-Disposition: form-data; name=\"upload\"; filename=\"a \\\"b\\\".txt\"\r\nContent-Type: text/plain\r\nX-Extra: 1\r\n\r\nline 1\r\nline 2",
            "content-disposition: form-data; name=empty; filename=\"\"\r\n\r\n",
        ]);
        let parts = parse_multipart(&data, "XyZ").unwrap();
        assert_eq!(parts.len(), 3);
        assert_eq!(parts[0].name, "title");
        assert_eq!(parts[0].filename, None);
        assert_eq!(&data[parts[0].data.clone()], b"Hello");
        assert_eq!(parts[1].filename.as_deref(), Some("a \"b\".txt"));
        assert_eq!(parts[1].content_type.as_deref(), Some("text/plain"));
        assert_eq!(parts[1].headers[2], ("x-extra".to_owned(), "1".to_owned()));
        assert_eq!(&data[parts[1].data.clone()], b"line 1\r\nline 2");
        assert_eq!(parts[2].filename.as_deref(), Some(""));
        assert!(parts[2].data.is_empty());
    }

    #[test]
    fn preamble_epilogue_padding_and_extended_names() {
        let data = b"preamble\r\n--b \t\r\nContent-Disposition: form-data; name=\"f\"; filename=\"x\"; filename*=UTF-8''caf%C3%A9.txt\r\n\r\nok\r\n--b--\r\nepilogue";
        let parts = parse_multipart(data, "b").unwrap();
        assert_eq!(parts[0].filename.as_deref(), Some("café.txt"));
        assert_eq!(&data[parts[0].data.clone()], b"ok");
        assert_eq!(parse_multipart(b"--b--", "b").unwrap(), Vec::new());
    }

    #[test]
    fn malformed_bodies_are_rejected() {
        let disposition = "Content-Disposition: form-data; name=\"a\"";
        for invalid in [
            "no delimiter at all".to_owned(),
            format!("--XyZ\r\n{disposition}\r\n\r\nunterminated"),
            format!("--XyZ\r\n{disposition}\r\n\r\nx\r\n--XyZjunk"),
            "--XyZ\r\nContent-Type: text/plain\r\n\r\nx\r\n--XyZ--".to_owned(),
            "--XyZ\r\nContent-Disposition: attachment; name=\"a\"\r\n\r\nx\r\n--XyZ--".to_owned(),
            "--XyZ\r\nContent-Disposition: form-data; filename=\"a\"\r\n\r\nx\r\n--XyZ--"
                .to_owned(),
            "--XyZ\r\nContent-Disposition: form-data; name=\"a\"; name=\"b\"\r\n\r\nx\r\n--XyZ--"
                .to_owned(),
            "--XyZ\r\nContent-Disposition: form-data; name=\"unclosed\r\n\r\nx\r\n--XyZ--"
                .to_owned(),
            format!("--XyZ\r\n{disposition}\r\n{disposition}\r\n\r\nx\r\n--XyZ--"),
            format!("--XyZ\r\nBad Header\r\n{disposition}\r\n\r\nx\r\n--XyZ--"),
        ] {
            assert!(
                parse_multipart(invalid.as_bytes(), "XyZ").is_err(),
                "{invalid:?}"
            );
        }
    }

    #[test]
    fn part_counts_and_header_sizes_are_bounded() {
        let field = "Content-Disposition: form-data; name=\"a\"\r\n\r\nx";
        let many = body(&vec![field; MAX_PARTS + 1]);
        assert_eq!(
            parse_multipart(&many, "XyZ"),
            Err("Too many multipart parts")
        );
        assert_eq!(
            parse_multipart(&body(&vec![field; MAX_PARTS]), "XyZ")
                .unwrap()
                .len(),
            MAX_PARTS
        );
        let huge = format!(
            "Content-Disposition: form-data; name=\"a\"\r\nX-Pad: {}\r\n\r\nx",
            "p".repeat(MAX_PART_HEADER_BYTES)
        );
        assert!(parse_multipart(&body(&[&huge]), "XyZ").is_err());
        let fields = "a=1&".repeat(MAX_PARTS + 1);
        assert!(parse_urlencoded(fields.as_bytes()).is_err());
        assert_eq!(
            parse_urlencoded(b"a=1&b=x+y&a=%C3%A9").unwrap(),
            vec![
                ("a".to_owned(), "1".to_owned()),
                ("b".to_owned(), "x y".to_owned()),
                ("a".to_owned(), "é".to_owned()),
            ]
        );
    }
}
