//! Bounded JSON conversion. Python integer precision is preserved in both directions.

use pyo3::{
    exceptions::{PyTypeError, PyValueError},
    prelude::*,
    types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple},
};
use serde_json::{Map, Value};

pub(crate) const MAX_INTEGER_DIGITS: usize = bessemer_codec::MAX_INTEGER_DIGITS;

// Superseded by `codec`; kept as the reference oracle for its tests.
#[cfg(test)]
pub(crate) fn parse_json(input: &[u8]) -> Result<Value, serde_json::Error> {
    // Deserialize containers through RawValue instead of Value's generic map
    // visitor. Its arbitrary_precision visitor reserves a synthetic number key;
    // a client object must never be mistaken for that internal representation.
    let raw: &serde_json::value::RawValue = serde_json::from_slice(input)?;
    from_raw(raw, 0)
}

// Superseded by `codec`; kept as the reference oracle for its tests.
#[cfg(test)]
fn from_raw(raw: &serde_json::value::RawValue, depth: usize) -> Result<Value, serde_json::Error> {
    use serde::de::Error as _;
    if depth > 64 {
        return Err(serde_json::Error::custom("JSON nesting exceeds 64 levels"));
    }
    let text = raw.get();
    match text.as_bytes()[0] {
        b'{' => {
            let mut parser = serde_json::Deserializer::from_str(text);
            let value =
                serde::de::Deserializer::deserialize_map(&mut parser, OriginalObject { depth })?;
            parser.end()?;
            Ok(value)
        }
        b'[' => {
            let items: Vec<&serde_json::value::RawValue> = serde_json::from_str(text)?;
            items
                .into_iter()
                .map(|item| from_raw(item, depth + 1))
                .collect::<Result<Vec<_>, _>>()
                .map(Value::Array)
        }
        b'"' => serde_json::from_str(text).map(Value::String),
        b't' | b'f' => serde_json::from_str(text).map(Value::Bool),
        b'n' => Ok(Value::Null),
        _ => {
            let value = Value::Number(serde_json::from_str(text)?);
            if !validate_input_numbers(&value) {
                return Err(serde_json::Error::custom(
                    "JSON numeric value exceeds the supported range",
                ));
            }
            Ok(value)
        }
    }
}

// Superseded by `codec`; kept as the reference oracle for its tests.
#[cfg(test)]
struct OriginalObject {
    depth: usize,
}

// Superseded by `codec`; kept as the reference oracle for its tests.
#[cfg(test)]
impl<'de> serde::de::Visitor<'de> for OriginalObject {
    type Value = Value;

    fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("a JSON object")
    }

    fn visit_map<M: serde::de::MapAccess<'de>>(self, mut map: M) -> Result<Value, M::Error> {
        let mut values = Map::new();
        while let Some(key) = map.next_key::<String>()? {
            let raw = map.next_value::<&serde_json::value::RawValue>()?;
            // Validate every original value before applying ordinary last-key-wins
            // object semantics. Invalid discarded values must not bypass bounds.
            let value = from_raw(raw, self.depth + 1).map_err(serde::de::Error::custom)?;
            values.insert(key, value);
        }
        Ok(Value::Object(values))
    }
}

struct Budget {
    bytes: usize,
    nodes: usize,
}

impl Budget {
    fn take(&mut self, bytes: usize) -> PyResult<()> {
        self.bytes = self.bytes.checked_sub(bytes).ok_or_else(|| {
            PyValueError::new_err("JSON response exceeds the configured size limit")
        })?;
        Ok(())
    }

    fn node(&mut self) -> PyResult<()> {
        self.nodes = self
            .nodes
            .checked_sub(1)
            .ok_or_else(|| PyValueError::new_err("JSON response contains too many values"))?;
        Ok(())
    }
}

pub(crate) fn python_to_json(value: &Bound<'_, PyAny>, depth: usize) -> PyResult<Value> {
    python_to_json_limited(value, depth, 4_194_304)
}

pub(crate) fn python_to_json_limited(
    value: &Bound<'_, PyAny>,
    depth: usize,
    max_bytes: usize,
) -> PyResult<Value> {
    let mut budget = Budget {
        bytes: max_bytes,
        nodes: max_bytes,
    };
    convert(value, depth, &mut budget)
}

pub(crate) fn encode_json_limited(value: &Bound<'_, PyAny>, max_bytes: usize) -> PyResult<Vec<u8>> {
    let mut budget = Budget {
        bytes: max_bytes,
        nodes: max_bytes,
    };
    let mut out = Vec::new();
    encode_value(value, 0, &mut budget, &mut out)?;
    Ok(out)
}

fn encode_value(
    value: &Bound<'_, PyAny>,
    depth: usize,
    budget: &mut Budget,
    out: &mut Vec<u8>,
) -> PyResult<()> {
    if depth > 64 {
        return Err(PyValueError::new_err(
            "JSON response nesting exceeds 64 levels",
        ));
    }
    budget.node()?;
    if value.is_none() {
        budget.take(4)?;
        out.extend_from_slice(b"null");
        return Ok(());
    }
    if value.is_instance_of::<PyBool>() {
        let value: bool = value.extract()?;
        budget.take(if value { 4 } else { 5 })?;
        out.extend_from_slice(if value { b"true" } else { b"false" });
        return Ok(());
    }
    if value.is_instance_of::<PyInt>() {
        if let Ok(v) = value.extract::<i64>() {
            let mut buffer = itoa::Buffer::new();
            let formatted = buffer.format(v);
            budget.take(formatted.len())?;
            out.extend_from_slice(formatted.as_bytes());
            return Ok(());
        }
        if let Ok(v) = value.extract::<u64>() {
            let mut buffer = itoa::Buffer::new();
            let formatted = buffer.format(v);
            budget.take(formatted.len())?;
            out.extend_from_slice(formatted.as_bytes());
            return Ok(());
        }
        // Calling the base type descriptor bypasses a subclass __str__/__repr__.
        let number: String = value
            .py()
            .get_type::<PyInt>()
            .getattr("__repr__")?
            .call1((value,))?
            .extract()?;
        if number.trim_start_matches('-').len() > MAX_INTEGER_DIGITS {
            return Err(PyValueError::new_err(
                "JSON integers cannot exceed 4096 digits",
            ));
        }
        budget.take(number.len())?;
        let parsed = number
            .parse::<serde_json::Number>()
            .map_err(|_| PyValueError::new_err("invalid JSON integer"))?;
        serde_json::to_writer(out, &parsed)
            .map_err(|_| PyValueError::new_err("invalid JSON integer"))?;
        return Ok(());
    }
    if value.is_instance_of::<PyFloat>() {
        let value = serde_json::Number::from_f64(value.extract()?)
            .ok_or_else(|| PyValueError::new_err("non-finite floats are not valid JSON"))?;
        let formatted = value.to_string();
        budget.take(formatted.len())?;
        out.extend_from_slice(formatted.as_bytes());
        return Ok(());
    }
    if let Ok(string) = value.cast::<PyString>() {
        let string = string.to_str()?;
        budget.take(json_string_size(string))?;
        serde_json::to_writer(out, string)
            .map_err(|_| PyValueError::new_err("invalid JSON response"))?;
        return Ok(());
    }
    if let Ok(dict) = value.cast::<PyDict>() {
        if dict.len() > budget.nodes {
            return Err(PyValueError::new_err(
                "JSON response contains too many values",
            ));
        }
        budget.take(2 + dict.len().saturating_sub(1) + dict.len())?;
        out.push(b'{');
        for (index, (key, value)) in dict.iter().enumerate() {
            let key = key
                .cast::<PyString>()
                .map_err(|_| PyTypeError::new_err("JSON object keys must be strings"))?;
            let key = key.to_str()?;
            budget.node()?;
            budget.take(json_string_size(key))?;
            if index > 0 {
                out.push(b',');
            }
            serde_json::to_writer(&mut *out, key)
                .map_err(|_| PyValueError::new_err("invalid JSON response"))?;
            out.push(b':');
            encode_value(&value, depth + 1, budget, out)?;
        }
        out.push(b'}');
        return Ok(());
    }
    if let Ok(list) = value.cast::<PyList>() {
        if list.len() > budget.nodes {
            return Err(PyValueError::new_err(
                "JSON response contains too many values",
            ));
        }
        budget.take(2 + list.len().saturating_sub(1))?;
        out.push(b'[');
        for (index, item) in list.iter().enumerate() {
            if index > 0 {
                out.push(b',');
            }
            encode_value(&item, depth + 1, budget, out)?;
        }
        out.push(b']');
        return Ok(());
    }
    if let Ok(tuple) = value.cast::<PyTuple>() {
        if tuple.len() > budget.nodes {
            return Err(PyValueError::new_err(
                "JSON response contains too many values",
            ));
        }
        budget.take(2 + tuple.len().saturating_sub(1))?;
        out.push(b'[');
        for (index, item) in tuple.iter().enumerate() {
            if index > 0 {
                out.push(b',');
            }
            encode_value(&item, depth + 1, budget, out)?;
        }
        out.push(b']');
        return Ok(());
    }
    Err(PyTypeError::new_err(
        "handler must return JSON-compatible values",
    ))
}

fn convert(value: &Bound<'_, PyAny>, depth: usize, budget: &mut Budget) -> PyResult<Value> {
    if depth > 64 {
        return Err(PyValueError::new_err(
            "JSON response nesting exceeds 64 levels",
        ));
    }
    budget.node()?;
    if value.is_none() {
        budget.take(4)?;
        return Ok(Value::Null);
    }
    if value.is_instance_of::<PyBool>() {
        let value: bool = value.extract()?;
        budget.take(if value { 4 } else { 5 })?;
        return Ok(Value::Bool(value));
    }
    if value.is_instance_of::<PyInt>() {
        if let Ok(v) = value.extract::<i64>() {
            budget.take(v.to_string().len())?;
            return Ok(Value::from(v));
        }
        if let Ok(v) = value.extract::<u64>() {
            budget.take(v.to_string().len())?;
            return Ok(Value::from(v));
        }
        // Calling the base type descriptor bypasses a subclass __str__/__repr__.
        let number: String = value
            .py()
            .get_type::<PyInt>()
            .getattr("__repr__")?
            .call1((value,))?
            .extract()?;
        if number.trim_start_matches('-').len() > MAX_INTEGER_DIGITS {
            return Err(PyValueError::new_err(
                "JSON integers cannot exceed 4096 digits",
            ));
        }
        budget.take(number.len())?;
        return number
            .parse::<serde_json::Number>()
            .map(Value::Number)
            .map_err(|_| PyValueError::new_err("invalid JSON integer"));
    }
    if value.is_instance_of::<PyFloat>() {
        let value = serde_json::Number::from_f64(value.extract()?)
            .ok_or_else(|| PyValueError::new_err("non-finite floats are not valid JSON"))?;
        budget.take(value.to_string().len())?;
        return Ok(Value::Number(value));
    }
    if let Ok(string) = value.cast::<PyString>() {
        let string = string.to_str()?;
        budget.take(json_string_size(string))?;
        return Ok(Value::String(string.to_owned()));
    }
    if let Ok(dict) = value.cast::<PyDict>() {
        if dict.len() > budget.nodes {
            return Err(PyValueError::new_err(
                "JSON response contains too many values",
            ));
        }
        let mut output = Map::new();
        budget.take(2 + dict.len().saturating_sub(1) + dict.len())?;
        for (key, value) in dict.iter() {
            let key = key
                .cast::<PyString>()
                .map_err(|_| PyTypeError::new_err("JSON object keys must be strings"))?;
            let key = key.to_str()?;
            budget.node()?;
            budget.take(json_string_size(key))?;
            output.insert(key.to_owned(), convert(&value, depth + 1, budget)?);
        }
        return Ok(Value::Object(output));
    }
    if let Ok(list) = value.cast::<PyList>() {
        if list.len() > budget.nodes {
            return Err(PyValueError::new_err(
                "JSON response contains too many values",
            ));
        }
        budget.take(2 + list.len().saturating_sub(1))?;
        let mut output = Vec::new();
        for item in list.iter() {
            output.push(convert(&item, depth + 1, budget)?);
        }
        return Ok(Value::Array(output));
    }
    if let Ok(tuple) = value.cast::<PyTuple>() {
        if tuple.len() > budget.nodes {
            return Err(PyValueError::new_err(
                "JSON response contains too many values",
            ));
        }
        budget.take(2 + tuple.len().saturating_sub(1))?;
        let mut output = Vec::new();
        for item in tuple.iter() {
            output.push(convert(&item, depth + 1, budget)?);
        }
        return Ok(Value::Array(output));
    }
    Err(PyTypeError::new_err(
        "handler must return JSON-compatible values",
    ))
}

fn json_string_size(value: &str) -> usize {
    value.bytes().fold(2usize, |total, byte| {
        total.saturating_add(match byte {
            b'"' | b'\\' | b'\n' | b'\r' | b'\t' | 8 | 12 => 2,
            0..=31 => 6,
            _ => 1,
        })
    })
}

// Superseded by `codec`; kept as the reference oracle for its tests.
#[cfg(test)]
pub(crate) fn validate_input_numbers(value: &Value) -> bool {
    match value {
        Value::Number(number) => {
            let raw = number.as_str();
            if raw.contains(['.', 'e', 'E']) {
                number.as_f64().is_some_and(f64::is_finite)
            } else {
                raw.trim_start_matches('-').len() <= MAX_INTEGER_DIGITS
            }
        }
        Value::Array(values) => values.iter().all(validate_input_numbers),
        Value::Object(values) => values.values().all(validate_input_numbers),
        _ => true,
    }
}

/// A bound request input. Scalars parsed from a path, query, header or form
/// value keep their type, so converting one to Python builds the object
/// directly instead of going through a JSON `Number` (which, with
/// `arbitrary_precision`, holds its digits in a `String`).
#[derive(Clone, Debug, PartialEq)]
pub(crate) enum Argument {
    Json(Value),
    Int(i64),
    /// Always finite.
    Float(f64),
    Bool(bool),
    /// An integer outside `i64`, as its canonical digits (at most 4096).
    Big(String),
}

impl From<Value> for Argument {
    fn from(value: Value) -> Self {
        Self::Json(value)
    }
}

pub(crate) fn argument_to_python(py: Python<'_>, value: &Argument) -> PyResult<Py<PyAny>> {
    match value {
        Argument::Json(value) => json_to_python(py, value),
        Argument::Int(value) => Ok(value.into_pyobject(py)?.into_any().unbind()),
        Argument::Float(value) => Ok(value.into_pyobject(py)?.into_any().unbind()),
        Argument::Bool(value) => Ok(value.into_pyobject(py)?.to_owned().into_any().unbind()),
        Argument::Big(digits) => Ok(py.get_type::<PyInt>().call1((digits.as_str(),))?.unbind()),
    }
}

pub(crate) fn parse_integer(raw: &str) -> Option<Argument> {
    let (negative, digits) = if let Some(raw) = raw.strip_prefix('-') {
        (true, raw)
    } else {
        (false, raw.strip_prefix('+').unwrap_or(raw))
    };
    if digits.is_empty()
        || digits.len() > MAX_INTEGER_DIGITS
        || !digits.bytes().all(|v| v.is_ascii_digit())
    {
        return None;
    }
    // Leading zeros and a `+` sign do not change the value.
    let digits = digits.trim_start_matches('0');
    if digits.is_empty() {
        return Some(Argument::Int(0));
    }
    // Up to 18 digits always fit.
    if digits.len() <= 18 {
        let magnitude = digits
            .bytes()
            .fold(0i64, |sum, digit| sum * 10 + i64::from(digit - b'0'));
        return Some(Argument::Int(if negative { -magnitude } else { magnitude }));
    }
    let normalized = if negative {
        format!("-{digits}")
    } else {
        digits.to_owned()
    };
    Some(match normalized.parse::<i64>() {
        Ok(value) => Argument::Int(value),
        Err(_) => Argument::Big(normalized),
    })
}

pub(crate) fn json_to_python(py: Python<'_>, value: &Value) -> PyResult<Py<PyAny>> {
    match value {
        Value::Null => Ok(py.None()),
        Value::Bool(v) => Ok(v.into_pyobject(py)?.to_owned().into_any().unbind()),
        Value::String(v) => Ok(v.into_pyobject(py)?.into_any().unbind()),
        Value::Number(v) => {
            if let Some(v) = v.as_i64() {
                return Ok(v.into_pyobject(py)?.into_any().unbind());
            }
            if let Some(v) = v.as_u64() {
                return Ok(v.into_pyobject(py)?.into_any().unbind());
            }
            let raw = v.as_str();
            if !raw.contains(['.', 'e', 'E']) {
                if raw.trim_start_matches('-').len() > MAX_INTEGER_DIGITS {
                    return Err(PyValueError::new_err("JSON integer exceeds 4096 digits"));
                }
                return Ok(py.get_type::<PyInt>().call1((raw,))?.unbind());
            }
            let number = v.as_f64().filter(|v| v.is_finite()).ok_or_else(|| {
                PyValueError::new_err("JSON float is outside the finite supported range")
            })?;
            Ok(number.into_pyobject(py)?.into_any().unbind())
        }
        Value::Array(values) => {
            let list = PyList::empty(py);
            for value in values {
                list.append(json_to_python(py, value)?)?;
            }
            Ok(list.into_any().unbind())
        }
        Value::Object(values) => {
            let dict = PyDict::new(py);
            for (key, value) in values {
                dict.set_item(key, json_to_python(py, value)?)?;
            }
            Ok(dict.into_any().unbind())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn input_integers_preserve_python_precision() {
        Python::attach(|py| {
            for raw in [
                "18446744073709551617",
                "-18446744073709551617",
                "1267650600228229401496703205376",
            ] {
                let input: Value = serde_json::from_str(raw).unwrap();
                let python = json_to_python(py, &input).unwrap();
                assert!(python.bind(py).is_instance_of::<PyInt>());
                assert_eq!(python_to_json(python.bind(py), 0).unwrap().to_string(), raw);
            }
        });
    }

    #[test]
    fn client_objects_cannot_spoof_internal_number_tokens() {
        for input in [
            br#"{"$serde_json::private::Number":"123"}"#.as_slice(),
            br#"{"nested":{"$serde_json::private::Number":"1e9999","keep":true}}"#.as_slice(),
        ] {
            let value = parse_json(input).unwrap();
            assert!(value.is_object());
            assert_eq!(
                parse_json(&serde_json::to_vec(&value).unwrap()).unwrap(),
                value
            );
        }
    }

    #[test]
    fn discarded_duplicate_values_are_validated_before_overwrite() {
        for input in [
            br#"{"text":"\ud800","text":"valid"}"#.as_slice(),
            br#"{"value":1e9999,"value":1}"#.as_slice(),
        ] {
            assert!(parse_json(input).is_err());
        }
        assert_eq!(
            parse_json(br#"{"value":1,"value":2}"#).unwrap()["value"],
            Value::from(2)
        );
        let deep = format!("{{\"x\":{}0{},\"x\":1}}", "[".repeat(65), "]".repeat(65));
        assert!(parse_json(deep.as_bytes()).is_err());
    }

    #[test]
    fn huge_numbers_and_copy_budgets_are_bounded() {
        assert!(!validate_input_numbers(
            &serde_json::from_str::<Value>("1e9999").unwrap()
        ));
        assert!(parse_integer("-01x").is_none());
        for (raw, expected) in [
            ("+000123", "123"),
            ("123", "123"),
            ("-45", "-45"),
            ("-0045", "-45"),
            ("000", "0"),
            ("-0", "0"),
        ] {
            let parsed = parse_integer(raw).unwrap();
            assert_eq!(parsed, Argument::Int(expected.parse().unwrap()), "{raw}");
        }
        Python::attach(|py| {
            let value = PyString::new(py, "x".repeat(4096).as_str());
            assert!(python_to_json_limited(value.as_any(), 0, 128).is_err());
        });
    }

    /// Differential tests: the single-pass codec must agree with the serde_json
    /// implementation it replaced, on valid documents and on corrupted ones.
    mod codec_parity {
        use super::*;

        struct Rng(u64);
        impl Rng {
            fn next(&mut self) -> u64 {
                self.0 ^= self.0 << 13;
                self.0 ^= self.0 >> 7;
                self.0 ^= self.0 << 17;
                self.0
            }
            fn below(&mut self, n: u64) -> u64 {
                self.next() % n
            }
            fn pick<'a>(&mut self, items: &[&'a str]) -> &'a str {
                items[self.below(items.len() as u64) as usize]
            }
        }

        const STRINGS: &[&str] = &[
            r#""""#,
            r#""plain""#,
            r#""a\"b""#,
            r#""tab\there""#,
            r#""été""#,
            r#""😀""#,
            r#""\ud83d""#,
            r#""\ude00""#,
            r#""\ud83dx""#,
            r#""\/\b\f\n\r""#,
            "\"caf\u{e9} \u{1F600}\"",
            r#""\x""#,
            "\"raw\u{1}control\"",
            r#""\u12""#,
            r#""a\\""#,
            r#""long string with no escapes at all, more than eight bytes""#,
        ];
        const NUMBERS: &[&str] = &[
            "0",
            "-0",
            "1",
            "-1",
            "42",
            "9223372036854775807",
            "9223372036854775808",
            "-9223372036854775808",
            "-9223372036854775809",
            "18446744073709551615",
            "18446744073709551616",
            "123456789012345678901234567890",
            "1.5",
            "-0.0",
            "0.1",
            "1e20",
            "1E-7",
            "2.5e+3",
            "1e400",
            "-1e400",
            "1e-400",
            "01",
            "1.",
            ".5",
            "-",
            "1e",
            "1.e3",
            "+1",
            "0x10",
            "1.7976931348623157e308",
            "5e-324",
            "123.456e-2",
        ];
        const KEYS: &[&str] = &["a", "b", "id", "name", "z", "\u{e9}", "a\\u0062", "", "B"];

        fn document(rng: &mut Rng, depth: usize, out: &mut String) {
            let choice = if depth > 6 {
                rng.below(5)
            } else {
                rng.below(9)
            };
            match choice {
                0 => out.push_str(rng.pick(STRINGS)),
                1 | 2 => out.push_str(rng.pick(NUMBERS)),
                3 => out.push_str(rng.pick(&["true", "false", "null"])),
                4 => out.push_str(rng.pick(STRINGS)),
                5 | 6 => {
                    out.push('[');
                    for index in 0..rng.below(4) {
                        if index > 0 {
                            out.push_str(rng.pick(&[",", " , ", ",\n"]));
                        }
                        document(rng, depth + 1, out);
                    }
                    out.push(']');
                }
                _ => {
                    out.push('{');
                    for index in 0..rng.below(5) {
                        if index > 0 {
                            out.push(',');
                        }
                        out.push('"');
                        out.push_str(rng.pick(KEYS));
                        out.push_str(rng.pick(&["\":", "\" : ", "\"\t:"]));
                        document(rng, depth + 1, out);
                    }
                    out.push('}');
                }
            }
        }

        fn corrupt(rng: &mut Rng, text: &str) -> Vec<u8> {
            let mut bytes = text.as_bytes().to_vec();
            if bytes.is_empty() {
                return bytes;
            }
            let at = rng.below(bytes.len() as u64) as usize;
            match rng.below(5) {
                0 => bytes.truncate(at),
                1 => {
                    bytes.remove(at);
                }
                2 => {
                    const INSERTS: &[u8] = b"{}[],:\"\\0e-. \x01\xff";
                    bytes.insert(at, INSERTS[rng.below(INSERTS.len() as u64) as usize]);
                }
                3 => bytes[at] = rng.below(256) as u8,
                _ => bytes.extend_from_slice(rng.pick(&[" ", "x", ",", "\n\t", "}"]).as_bytes()),
            }
            bytes
        }

        fn old_python(py: Python<'_>, input: &[u8]) -> Option<String> {
            let value = parse_json(input).ok()?;
            let object = json_to_python(py, &value).ok()?;
            Some(object.bind(py).repr().unwrap().to_string())
        }

        fn check(py: Python<'_>, input: &[u8]) -> bool {
            let expected = old_python(py, input);
            let direct = bessemer_codec::parse_python(py, input)
                .ok()
                .map(|object| object.repr().unwrap().to_string());
            let via_value = bessemer_codec::parse_value(input)
                .ok()
                .and_then(|value| json_to_python(py, &value).ok())
                .map(|object| object.bind(py).repr().unwrap().to_string());
            assert_eq!(
                direct,
                expected,
                "parse_python disagrees on {:?}",
                String::from_utf8_lossy(input)
            );
            assert_eq!(
                via_value,
                expected,
                "parse_value disagrees on {:?}",
                String::from_utf8_lossy(input)
            );
            expected.is_some()
        }

        #[test]
        fn parsers_match_the_previous_implementation() {
            Python::attach(|py| {
                let mut rng = Rng(0x9E37_79B9_7F4A_7C15);
                let (mut valid, mut invalid) = (0, 0);
                for _ in 0..40_000 {
                    let mut text = String::new();
                    if rng.below(4) == 0 {
                        text.push_str(rng.pick(&[" ", "\n", "\t\r"]));
                    }
                    document(&mut rng, 0, &mut text);
                    if check(py, text.as_bytes()) {
                        valid += 1
                    } else {
                        invalid += 1
                    }
                    let corrupted = corrupt(&mut rng, &text);
                    if check(py, &corrupted) {
                        valid += 1
                    } else {
                        invalid += 1
                    }
                }
                for depth in [63, 64, 65, 66, 200] {
                    for (open, close) in [("[", "]"), ("{\"a\":", "}")] {
                        for inner in ["0", "[]", "{}"] {
                            let text =
                                format!("{}{}{}", open.repeat(depth), inner, close.repeat(depth));
                            check(py, text.as_bytes());
                        }
                    }
                }
                for edge in [
                    "",
                    " ",
                    "\u{feff}1",
                    "1 2",
                    "[1,]",
                    "{\"a\":1,}",
                    "nul",
                    "truex",
                    "[1]x",
                ] {
                    check(py, edge.as_bytes());
                }
                let long = format!("1{}", "0".repeat(4095));
                assert!(check(py, long.as_bytes()));
                assert!(!check(py, format!("{long}0").as_bytes()));
                assert!(
                    valid > 10_000 && invalid > 10_000,
                    "{valid} valid, {invalid} invalid"
                );
            });
        }

        #[test]
        fn encoder_matches_the_previous_implementation() {
            Python::attach(|py| {
                let mut rng = Rng(0xD1B5_4A32_D192_ED03);
                let mut encoded = 0;
                for _ in 0..20_000 {
                    let mut text = String::new();
                    document(&mut rng, 0, &mut text);
                    let Some(value) = parse_json(text.as_bytes()).ok() else {
                        continue;
                    };
                    let object = json_to_python(py, &value).unwrap();
                    let object = object.bind(py);
                    let limit = if rng.below(3) == 0 {
                        rng.below(64) as usize
                    } else {
                        1 << 20
                    };
                    let expected = encode_json_limited(object, limit)
                        .ok()
                        .filter(|bytes| bytes.len() <= limit);
                    let actual = bessemer_codec::encode_plain(object, limit, <[u8]>::to_vec);
                    // Big integers beyond 64 bits take the general path by design.
                    let has_big = text.contains("18446744073709551616")
                        || text.contains("123456789012345678901234567890")
                        || text.contains("-9223372036854775809");
                    if !has_big {
                        assert_eq!(actual, expected, "encoder disagrees on {text}");
                    } else if let Some(actual) = actual {
                        assert_eq!(Some(actual), expected, "encoder disagrees on {text}");
                    }
                    encoded += usize::from(expected.is_some());
                }
                assert!(encoded > 5_000, "{encoded}");
                // Values the plain encoder must hand to the general path.
                for expression in [
                    c"(1, 2)",
                    c"float('nan')",
                    c"{1: 2}",
                    c"'\\ud800'",
                    c"[1, (2,)]",
                    c"type('S', (str,), {})('x')",
                    c"2**64",
                    c"-2**63-1",
                    c"{'a': float('inf')}",
                ] {
                    let value = py.eval(expression, None, None).unwrap();
                    assert!(
                        bessemer_codec::encode_plain(&value, 1 << 20, <[u8]>::to_vec).is_none()
                    );
                }
                let deep = py.eval(c"eval('[' * 65 + ']' * 65)", None, None).unwrap();
                assert!(bessemer_codec::encode_plain(&deep, 1 << 20, <[u8]>::to_vec).is_some());
                let deeper = py.eval(c"eval('[' * 66 + ']' * 66)", None, None).unwrap();
                assert!(bessemer_codec::encode_plain(&deeper, 1 << 20, <[u8]>::to_vec).is_none());
                let cycle = py
                    .eval(c"(lambda l: (l.append(l), l)[1])([])", None, None)
                    .unwrap();
                assert!(bessemer_codec::encode_plain(&cycle, 1 << 20, <[u8]>::to_vec).is_none());
            });
        }
    }
}
