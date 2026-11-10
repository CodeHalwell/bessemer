//! RFC 6265 cookie parsing shared by scalar bindings and Request metadata.
//!
//! Each `name=value` pair is checked against the RFC's octets. A pair that
//! does not fit is left out rather than guessed at, and the first cookie with
//! a name wins, as a browser sends the most specific one first. The request
//! is never refused for its cookies: a browser attaches every cookie in scope,
//! including ones set by other applications on the same domain, so one
//! malformed or repeated cookie must not lock a user out.
use axum::http::{HeaderMap, header};
use std::collections::HashMap;

pub(crate) fn parse(headers: &HeaderMap) -> HashMap<String, String> {
    let mut cookies = HashMap::new();
    for header in headers.get_all(header::COOKIE) {
        let Ok(header) = header.to_str() else {
            continue;
        };
        for pair in header.split(';') {
            let Some((name, value)) = well_formed(pair.trim()) else {
                continue;
            };
            if !cookies.contains_key(name) {
                cookies.insert(name.to_owned(), value.to_owned());
            }
        }
    }
    cookies
}

/// The name and value of a pair made only of RFC 6265 octets. A quoted value
/// loses its quotes; one with a quote anywhere else is not well formed.
fn well_formed(pair: &str) -> Option<(&str, &str)> {
    let (name, value) = pair.split_once('=')?;
    if name.is_empty()
        || !name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&byte))
    {
        return None;
    }
    let value = if value.starts_with('"') {
        value.strip_prefix('"')?.strip_suffix('"')?
    } else {
        value
    };
    value
        .bytes()
        .all(|byte| matches!(byte, 0x21 | 0x23..=0x2B | 0x2D..=0x3A | 0x3C..=0x5B | 0x5D..=0x7E))
        .then_some((name, value))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parsed(values: &[&str]) -> HashMap<String, String> {
        let mut headers = HeaderMap::new();
        for value in values {
            headers.append(header::COOKIE, value.parse().unwrap());
        }
        parse(&headers)
    }

    #[test]
    fn quoted_values_are_canonical() {
        let cookies = parsed(&["session=\"abc\"; theme=dark"]);
        assert_eq!(cookies["session"], "abc");
        assert_eq!(cookies["theme"], "dark");
    }

    #[test]
    fn a_pair_that_is_not_well_formed_is_left_out_and_the_rest_kept() {
        for malformed in [
            // A semicolon splits a quoted value: neither half is a cookie.
            "session=\"a;b\"",
            "session=\"a\\b\"",
            "session=unquoted space",
            "session={\"theme\":\"dark\"}",
            "session=a,b",
            "bad name=value",
            "=value",
            "flag",
        ] {
            let cookies = parsed(&[&format!("id=one; {malformed}; theme=dark")]);
            assert_eq!(cookies.len(), 2, "{malformed}");
            assert_eq!(cookies["id"], "one", "{malformed}");
            assert_eq!(cookies["theme"], "dark", "{malformed}");
        }
    }

    #[test]
    fn the_first_cookie_with_a_name_wins() {
        let cookies = parsed(&["session=one; session=two", "session=three"]);
        assert_eq!(cookies.len(), 1);
        assert_eq!(cookies["session"], "one");
        // A malformed first pair does not claim the name.
        let cookies = parsed(&["session=a b; session=two"]);
        assert_eq!(cookies["session"], "two");
    }
}
