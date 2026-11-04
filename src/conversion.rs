//! Bounded JSON conversion. Python integer precision is preserved in both directions.

use pyo3::{
    exceptions::{PyTypeError, PyValueError},
    prelude::*,
    types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple},
};
use serde_json::{Map, Value};

pub(crate) const MAX_INTEGER_DIGITS: usize = 4096;

pub(crate) fn parse_json(input: &[u8]) -> Result<Value, serde_json::Error> {
    // Deserialize containers through RawValue instead of Value's generic map
    // visitor. Its arbitrary_precision visitor reserves a synthetic number key;
    // a client object must never be mistaken for that internal representation.
    let raw: &serde_json::value::RawValue = serde_json::from_slice(input)?;
    from_raw(raw, 0)
}

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

struct OriginalObject {
    depth: usize,
}

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

pub(crate) fn parse_integer(raw: &str) -> Option<Value> {
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
    let digits = digits.trim_start_matches('0');
    let normalized = if digits.is_empty() {
        "0".to_owned()
    } else if negative {
        format!("-{digits}")
    } else {
        digits.to_owned()
    };
    normalized
        .parse::<serde_json::Number>()
        .ok()
        .map(Value::Number)
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
        assert!(parse_integer("+000123").is_some());
        assert!(parse_integer("-01x").is_none());
        Python::attach(|py| {
            let value = PyString::new(py, "x".repeat(4096).as_str());
            assert!(python_to_json_limited(value.as_any(), 0, 128).is_err());
        });
    }
}
