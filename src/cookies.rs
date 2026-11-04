//! Strict RFC 6265 cookie parsing shared by scalar bindings and Request metadata.
use axum::http::{HeaderMap, header};
use std::collections::HashMap;

pub(crate) fn parse(headers: &HeaderMap) -> Result<HashMap<String, String>, &'static str> {
    let mut cookies = HashMap::new();
    for header in headers.get_all(header::COOKIE) {
        let header = header.to_str().map_err(|_| "Invalid Cookie header")?;
        for pair in header.split(';') {
            let pair = pair.trim();
            if pair.is_empty() {
                continue;
            }
            let (name, value) = pair.split_once('=').ok_or("Invalid Cookie header")?;
            if name.is_empty()
                || !name
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&byte))
            {
                return Err("Invalid Cookie header");
            }
            let value = if value.starts_with('"') {
                value
                    .strip_prefix('"')
                    .and_then(|value| value.strip_suffix('"'))
                    .ok_or("Invalid Cookie header")?
            } else {
                value
            };
            if !value.bytes().all(
                |byte| matches!(byte, 0x21 | 0x23..=0x2B | 0x2D..=0x3A | 0x3C..=0x5B | 0x5D..=0x7E),
            ) {
                return Err("Invalid Cookie header");
            }
            if cookies.insert(name.to_owned(), value.to_owned()).is_some() {
                return Err("Duplicate Cookie names are not supported");
            }
        }
    }
    Ok(cookies)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn quoted_values_are_canonical_and_ambiguity_is_rejected() {
        let mut headers = HeaderMap::new();
        headers.insert(
            header::COOKIE,
            "session=\"abc\"; theme=dark".parse().unwrap(),
        );
        assert_eq!(parse(&headers).unwrap().get("session").unwrap(), "abc");
        for value in [
            "session=one; session=two",
            "session=\"a;b\"",
            "session=\"a\\b\"",
            "session=unquoted space",
            "bad name=value",
        ] {
            headers.insert(header::COOKIE, value.parse().unwrap());
            assert!(parse(&headers).is_err());
        }
    }
}
