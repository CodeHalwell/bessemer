//! Native fast path for plain Pydantic models.
//!
//! A [`ModelPlan`] describes a Pydantic model whose validation and JSON
//! serialization reduce to type checks: fields of `int`, `float`, `str`,
//! `bool`, lists, optionals and nested plain models, with no validators,
//! constraints, custom serializers or config that changes either. Python
//! decides which models qualify (see `bessemer._model_plan`) and describes
//! them as nested tuples.
//!
//! [`build_model`] turns a parsed request body into model instances directly,
//! the way pydantic-core's model validator builds them, when every value already
//! has its field's exact JSON type. Anything else (a coercion, a missing field
//! without a default, an extra key under `extra="forbid"`, an integer outside
//! 64 bits) returns `None` and the caller validates with Pydantic, so coercion
//! results and error reports stay Pydantic's own.
//!
//! [`encode_model`] writes a model instance as the JSON that
//! `dump_python(mode="json", by_alias=True)` followed by [`super::encode_plain`]
//! produces, with object keys in field order. A value that does not have its
//! field's exact type returns `None` so the caller keeps Pydantic's behaviour,
//! including its serialization warnings.

use std::sync::Arc;

use pyo3::{
    exceptions::PyValueError,
    ffi,
    prelude::*,
    types::{PyBool, PyDict, PyFloat, PyInt, PyList, PySet, PyString, PyTuple, PyType},
};
use serde_json::Value;

use super::{Bail, Encoder, MAX_DEPTH, Member, ParseError, Parser, Sink, Text, write_escaped};

/// The expected type of one value.
enum Node {
    Int,
    Float,
    Str,
    Bool,
    List(Box<Node>),
    Nullable(Box<Node>),
    /// A string-keyed dict whose values follow the inner node (exactly `str`
    /// keys, written in the dict's order).
    StrMap(Box<Node>),
    Model(Arc<ModelBody>),
}

struct Field {
    /// Interned attribute name.
    name: Py<PyString>,
    /// The key read from request bodies (the validation alias, if any).
    key: String,
    /// The key is written in JSON as its own bytes between quotes: it has no
    /// quote, backslash or control character to escape.
    plain_key: bool,
    /// The same key, interned, for O(1) dict lookups on the response side.
    key_py: Py<PyString>,
    /// Pre-escaped `"key":` prefix for JSON output, in declaration order.
    prefix: Vec<u8>,
    /// An immutable default, used when the key is absent.
    default: Option<Default>,
    /// The default's JSON bytes, written when a validated dict or body lacks
    /// the key and the field has a default. Compiled once, so the encoder
    /// never re-formats or re-escapes defaults.
    default_bytes: Option<Vec<u8>>,
    node: Node,
}

/// A field default: a shared immutable value, or a mutable one that is
/// rebuilt fresh (the way Pydantic deep-copies it) for every instance.
enum Default {
    Shared(Py<PyAny>),
    Fresh(Py<PyAny>),
}

/// A plain Pydantic model, compiled: the class, its plain fields, and every
/// field name (the `__pydantic_fields_set__` of an instance built from a body
/// that gave each field, copied rather than built per instance).
pub struct ModelBody {
    cls: Py<PyType>,
    forbid_extra: bool,
    fields: Vec<Field>,
    /// No two fields read the same body key, so the first field with a key
    /// is the only one (see `Parser::typed_model`).
    distinct_keys: bool,
    all_fields: Py<PySet>,
}

/// A compiled response shape for a `response_model`: a single model, a list
/// of values, a string-keyed dict of values, or an optional of these.
pub struct ModelPlan {
    /// The shape, with [`ModelBody`]s at its model nodes.
    root: Node,
    /// The model class at the plan's root (its `model`).
    cls: Py<PyType>,
    is_list: bool,
    optional: bool,
}

impl ModelPlan {
    /// Compile a plan from its Python description:
    /// `("model", cls, forbid_extra, ((name, key, output_key, node, has_default, default), ...))`
    /// for one model, or `("list", value)`, `("nullable", value)`,
    /// `("dict", value)` for a wrapped one, where a value is a model
    /// description or another wrapper.
    pub fn from_spec(spec: &Bound<'_, PyAny>) -> PyResult<Arc<Self>> {
        let kind: String = spec.get_item(0)?.extract()?;
        let (root, optional, is_list) = if kind == "list" {
            (
                Node::List(Box::new(node(&spec.get_item(1)?, 0)?)),
                false,
                true,
            )
        } else if kind == "nullable" {
            (
                Node::Nullable(Box::new(node(&spec.get_item(1)?, 0)?)),
                true,
                false,
            )
        } else if kind == "dict" {
            (
                Node::StrMap(Box::new(node(&spec.get_item(1)?, 0)?)),
                false,
                false,
            )
        } else {
            match node(spec, 0)? {
                Node::Model(body) => (Node::Model(body), false, false),
                _ => return Err(PyValueError::new_err("a model plan must describe a model")),
            }
        };
        let cls = response_class(spec.py(), &root)?;
        Ok(Arc::new(Self {
            root,
            cls,
            is_list,
            optional,
        }))
    }

    /// The model body at the root, for the request-side functions that build
    /// exactly one model: None when this plan wraps its model.
    pub fn body(&self) -> Option<&Arc<ModelBody>> {
        match &self.root {
            Node::Model(body) => Some(body),
            _ => None,
        }
    }

    pub fn model_class<'py>(&self, py: Python<'py>) -> &Bound<'py, PyType> {
        self.cls.bind(py)
    }

    /// Whether the plan describes a ``list[...]`` response annotation.
    pub fn is_list(&self) -> bool {
        self.is_list
    }

    /// Whether the plan describes an ``... | None`` response annotation.
    pub fn is_optional(&self) -> bool {
        self.optional
    }
}

/// The single model class a response node describes: its own for a model, or
/// the inner one for a wrapper (the Python-visible plan's `model`).
fn response_class<'py>(py: Python<'py>, root: &Node) -> PyResult<Py<PyType>> {
    match root {
        Node::Model(body) => Ok(body.cls.clone_ref(py)),
        Node::List(inner) | Node::Nullable(inner) | Node::StrMap(inner) => {
            response_class(py, inner)
        }
        _ => Err(PyValueError::new_err(
            "a model plan must describe at least one model",
        )),
    }
}

fn node(spec: &Bound<'_, PyAny>, depth: usize) -> PyResult<Node> {
    if depth > MAX_DEPTH {
        return Err(PyValueError::new_err("model plan is nested too deeply"));
    }
    let spec = spec.cast::<PyTuple>()?;
    let kind: String = spec.get_item(0)?.extract()?;
    Ok(match kind.as_str() {
        "int" => Node::Int,
        "float" => Node::Float,
        "str" => Node::Str,
        "bool" => Node::Bool,
        "list" => Node::List(Box::new(node(&spec.get_item(1)?, depth + 1)?)),
        "nullable" => Node::Nullable(Box::new(node(&spec.get_item(1)?, depth + 1)?)),
        "dict" => Node::StrMap(Box::new(node(&spec.get_item(1)?, depth + 1)?)),
        "model" => {
            let py = spec.py();
            let cls = spec.get_item(1)?.cast_into::<PyType>()?.unbind();
            let forbid_extra: bool = spec.get_item(2)?.extract()?;
            let mut fields = Vec::new();
            let mut output_keys = Vec::new();

            for field in spec.get_item(3)?.cast::<PyTuple>()?.iter() {
                let field = field.cast::<PyTuple>()?;
                let name: String = field.get_item(0)?.extract()?;
                let key: String = field.get_item(1)?.extract()?;
                let key_py = PyString::intern(py, &key).unbind();
                let output_key: String = field.get_item(2)?.extract()?;
                let has_default: bool = field.get_item(4)?.extract()?;
                let mut prefix = Vec::with_capacity(output_key.len() + 3);
                write_escaped(&mut prefix, output_key.as_bytes(), usize::MAX)
                    .map_err(|_| PyValueError::new_err("model plan key is too large"))?;
                prefix.push(b':');
                let inner = node(&field.get_item(3)?, depth + 1)?;
                let value = has_default.then(|| field.get_item(5)).transpose()?;
                let (default, default_bytes) = match value {
                    Some(value) => {
                        let mut bytes = Vec::new();
                        if !default_bytes(&inner, &value, &mut bytes) {
                            return Err(PyValueError::new_err(
                                "model plan default does not match its field type",
                            ));
                        }
                        let kind = if value.is_exact_instance_of::<PyDict>()
                            || value.is_exact_instance_of::<PyList>()
                        {
                            // Pydantic deep-copies a mutable default for every
                            // instance; the builder rebuilds one the same way.
                            Default::Fresh(value.unbind())
                        } else {
                            Default::Shared(value.unbind())
                        };
                        (Some(kind), Some(bytes))
                    }
                    None => (None, None),
                };
                fields.push(Field {
                    name: PyString::intern(py, &name).unbind(),
                    key_py,
                    plain_key: super::first_escape(key.as_bytes(), 0) == key.len(),
                    key,
                    prefix,
                    default,
                    default_bytes,
                    node: inner,
                });
                output_keys.push(output_key);
            }
            output_keys.sort_unstable();
            if output_keys.windows(2).any(|pair| pair[0] == pair[1]) {
                return Err(PyValueError::new_err(
                    "model plan has duplicate output keys",
                ));
            }
            let all_fields =
                PySet::new(py, fields.iter().map(|field| field.name.bind(py)))?.unbind();
            let distinct_keys = fields
                .iter()
                .enumerate()
                .all(|(index, field)| fields[..index].iter().all(|other| other.key != field.key));
            Node::Model(Arc::new(ModelBody {
                cls,
                forbid_extra,
                fields,
                distinct_keys,
                all_fields,
            }))
        }
        _ => return Err(PyValueError::new_err("unknown model plan node")),
    })
}

// ---------------------------------------------------------------------------
// Building instances from request bodies
// ---------------------------------------------------------------------------

/// Build a model instance from a parsed body, or `None` when Pydantic must
/// validate it. Never raises: a Python error while building also means `None`.
pub fn build_model<'py>(
    py: Python<'py>,
    plan: &ModelPlan,
    value: &Value,
) -> Option<Bound<'py, PyAny>> {
    // A Python error here was already taken from the interpreter by PyO3.
    let body = plan.body()?;
    model(py, body, value).ok().flatten()
}

fn build<'py>(py: Python<'py>, node: &Node, value: &Value) -> PyResult<Option<Bound<'py, PyAny>>> {
    Ok(Some(match (node, value) {
        (Node::Int, Value::Number(number)) => {
            if let Some(value) = number.as_i64() {
                value.into_pyobject(py)?.into_any()
            } else if let Some(value) = number.as_u64() {
                value.into_pyobject(py)?.into_any()
            } else {
                return Ok(None);
            }
        }
        (Node::Float, Value::Number(number)) => {
            // Python's float(int) and Rust's `as` both round to nearest even.
            let value = if let Some(value) = number.as_i64() {
                value as f64
            } else if let Some(value) = number.as_u64() {
                value as f64
            } else if number.as_str().contains(['.', 'e', 'E']) {
                match number.as_f64() {
                    Some(value) if value.is_finite() => value,
                    _ => return Ok(None),
                }
            } else {
                // An integer outside 64 bits.
                return Ok(None);
            };
            value.into_pyobject(py)?.into_any()
        }
        (Node::Str, Value::String(text)) => PyString::new(py, text).into_any(),
        (Node::Bool, Value::Bool(flag)) => flag.into_pyobject(py)?.to_owned().into_any(),
        (Node::Nullable(_), Value::Null) => py.None().into_bound(py),
        (Node::Nullable(inner), value) => return build(py, inner, value),
        (Node::List(inner), Value::Array(items)) => {
            let list = PyList::empty(py);
            for item in items {
                match build(py, inner, item)? {
                    Some(item) => list.append(item)?,
                    None => return Ok(None),
                }
            }
            list.into_any()
        }
        (Node::Model(body), value) => return model(py, body, value),
        _ => return Ok(None),
    }))
}

fn model<'py>(
    py: Python<'py>,
    body: &ModelBody,
    value: &Value,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let Value::Object(members) = value else {
        return Ok(None);
    };
    if body.forbid_extra
        && members
            .keys()
            .any(|key| !body.fields.iter().any(|field| field.key == *key))
    {
        return Ok(None);
    }
    let mut slots = Vec::with_capacity(body.fields.len());
    for field in &body.fields {
        match members.get(&field.key) {
            Some(value) => match build(py, &field.node, value)? {
                Some(built) => slots.push(Some(built)),
                None => return Ok(None),
            },
            None => slots.push(None),
        }
    }
    assemble(py, body, slots)
}

/// Create the instance from one built value (or `None` for an absent key)
/// per field, in field order. An absent field without a default means `None`.
fn assemble<'py>(
    py: Python<'py>,
    body: &ModelBody,
    slots: Vec<Option<Bound<'py, PyAny>>>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let complete = slots.iter().all(Option::is_some);
    let fields_set = if complete {
        // SAFETY: `PySet_New` on a set copies its table without rehashing
        // and returns a new reference, or null with an exception set.
        unsafe { Bound::from_owned_ptr_or_err(py, ffi::PySet_New(body.all_fields.as_ptr()))? }
            .cast_into::<PySet>()?
    } else {
        PySet::empty(py)?
    };
    let values = PyDict::new(py);
    for (field, slot) in body.fields.iter().zip(slots) {
        let name = field.name.bind(py);
        match slot {
            Some(built) => {
                values.set_item(name, built)?;
                if !complete {
                    fields_set.add(name)?;
                }
            }
            None => match field.default.as_ref() {
                Some(Default::Shared(value)) => values.set_item(name, value.bind(py))?,
                // The deep copy Pydantic makes of a mutable default.
                Some(Default::Fresh(value)) => {
                    values.set_item(name, rebuild(py, value.bind(py))?)?
                }
                None => return Ok(None),
            },
        }
    }
    let instance = instantiate(py, body.cls.bind(py))?;
    // The same attributes, in the same order, that pydantic-core's model
    // validator sets on an instance it builds.
    set_dict(&instance, values.as_any())?;
    set_attribute(
        &instance,
        pyo3::intern!(py, "__pydantic_extra__"),
        &py.None().into_bound(py),
    )?;
    set_attribute(
        &instance,
        pyo3::intern!(py, "__pydantic_private__"),
        &py.None().into_bound(py),
    )?;
    set_attribute(
        &instance,
        pyo3::intern!(py, "__pydantic_fields_set__"),
        fields_set.as_any(),
    )?;
    Ok(Some(instance))
}

/// Create an uninitialised instance through the class's `tp_new`, as
/// pydantic-core does, without running `__init__`.
fn instantiate<'py>(py: Python<'py>, cls: &Bound<'py, PyType>) -> PyResult<Bound<'py, PyAny>> {
    let args = PyTuple::empty(py);
    // SAFETY: `cls` is a live type object. `tp_new` is called with a valid
    // empty argument tuple and no keywords, and returns a new reference or
    // null with an exception set.
    unsafe {
        let raw = cls.as_type_ptr();
        let slot = ffi::PyType_GetSlot(raw, ffi::Py_tp_new);
        if slot.is_null() {
            return Err(pyo3::exceptions::PyTypeError::new_err(
                "base type without tp_new",
            ));
        }
        let new: ffi::newfunc = std::mem::transmute(slot);
        Bound::from_owned_ptr_or_err(py, new(raw, args.as_ptr(), std::ptr::null_mut()))
    }
}

/// `object.__setattr__`, bypassing `BaseModel.__setattr__`.
fn set_attribute(
    instance: &Bound<'_, PyAny>,
    name: &Bound<'_, PyString>,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    // SAFETY: all three are live objects borrowed for the call.
    let status =
        unsafe { ffi::PyObject_GenericSetAttr(instance.as_ptr(), name.as_ptr(), value.as_ptr()) };
    if status == 0 {
        Ok(())
    } else {
        Err(PyErr::fetch(instance.py()))
    }
}

/// Install `values` as the instance's `__dict__`, the way the `__dict__`
/// slot descriptor does (the same as a `__dict__` setattr, without the
/// attribute-name lookup).
fn set_dict(instance: &Bound<'_, PyAny>, values: &Bound<'_, PyAny>) -> PyResult<()> {
    let status = unsafe {
        ffi::PyObject_GenericSetDict(instance.as_ptr(), values.as_ptr(), std::ptr::null_mut())
    };
    if status == 0 {
        Ok(())
    } else {
        Err(PyErr::fetch(instance.py()))
    }
}

/// A fresh copy of a mutable default: new exact lists and dicts, the same
/// immutable leaves a deep copy would share, in the same order.
fn rebuild<'py>(py: Python<'py>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    if let Ok(dict) = value.cast_exact::<PyDict>() {
        let fresh = PyDict::new(py);
        for (key, item) in dict.iter() {
            let value =
                if item.is_exact_instance_of::<PyDict>() || item.is_exact_instance_of::<PyList>() {
                    rebuild(py, &item)?
                } else {
                    item
                };
            fresh.set_item(key, value)?;
        }
        Ok(fresh.into_any())
    } else if let Ok(list) = value.cast_exact::<PyList>() {
        let fresh = PyList::empty(py);
        for item in list.iter() {
            if item.is_exact_instance_of::<PyDict>() || item.is_exact_instance_of::<PyList>() {
                fresh.append(rebuild(py, &item)?)?;
            } else {
                fresh.append(item)?;
            }
        }
        Ok(fresh.into_any())
    } else {
        Ok(value.clone())
    }
}

/// The JSON bytes of a field default, as `dump_python(mode="json", by_alias=True)`
/// writes an instance holding it as its field value: with the field's node
/// coercions (an int default for a `float` field is a float) and escapes.
/// False means the default is not a value this encoding accepts for the field.
fn default_bytes(node: &Node, object: &Bound<'_, PyAny>, out: &mut Vec<u8>) -> bool {
    // Pydantic serialises a None-valued field as null for every type,
    // whether or not the field can hold one, and validates none of its
    // defaults, so a None default writes null under any field type.
    if object.is_none() {
        out.extend_from_slice(b"null");
        return true;
    }
    match node {
        Node::Nullable(inner) => default_bytes(inner, object, out),
        Node::List(inner) => {
            let Ok(list) = object.cast_exact::<PyList>() else {
                return false;
            };
            out.push(b'[');
            for (index, item) in list.iter().enumerate() {
                if index > 0 {
                    out.push(b',');
                }
                if !default_bytes(inner, &item, out) {
                    return false;
                }
            }
            out.push(b']');
            true
        }
        Node::Int => {
            // bools are exact ints in Python; only an int field default is.
            if object.is_exact_instance_of::<PyBool>() {
                return false;
            }
            let Ok(value) = object.extract::<i64>() else {
                return false;
            };
            let mut buffer = itoa::Buffer::new();
            out.extend_from_slice(buffer.format(value).as_bytes());
            true
        }
        Node::Float => {
            if object.is_exact_instance_of::<PyBool>() {
                return false;
            }
            let Ok(value) = object.extract::<f64>() else {
                return false;
            };
            if !value.is_finite() {
                return false;
            }
            let mut buffer = zmij::Buffer::new();
            out.extend_from_slice(buffer.format_finite(value).as_bytes());
            true
        }
        Node::Str => {
            let Ok(text) = object.extract::<&str>() else {
                return false;
            };
            write_escaped(out, text.as_bytes(), usize::MAX).is_ok()
        }
        Node::Bool => {
            let Ok(flag) = object.cast_exact::<PyBool>() else {
                return false;
            };
            out.extend_from_slice(if flag.is_true() {
                &b"true"[..]
            } else {
                &b"false"[..]
            });
            true
        }
        Node::StrMap(_) => false,
        Node::Model(_) => false,
    }
}

// ---------------------------------------------------------------------------
// Building instances straight from request bytes
// ---------------------------------------------------------------------------

/// Check that `input` is a JSON document [`super::parse_value`] accepts,
/// with the same limits, without building anything.
pub fn validate_json(input: &[u8]) -> Result<(), ParseError> {
    super::parse(input, Scan).map(|_| ())
}

/// Build a model instance straight from JSON bytes, in one pass over them.
///
/// `Ok(None)` means the caller must parse the body as before and let Pydantic
/// validate it: a value without its field's exact type, a duplicate field key,
/// an extra key under `extra="forbid"`, or a missing field without a default.
/// Such a result always matches [`build_model`] on the parsed value. Parsing
/// stops at that first mismatch, so the rest of the input is not checked: a
/// caller that has not run [`validate_json`] must treat a later failure of
/// [`super::parse_value`] as invalid JSON.
///
/// `Err` is a syntax or bounds error met before any mismatch, or content after
/// the document: the input is not valid JSON.
pub fn parse_model<'py>(
    py: Python<'py>,
    plan: &ModelPlan,
    input: &[u8],
) -> Result<Option<Bound<'py, PyAny>>, ParseError> {
    // Only a plain model plan parses from bytes: a wrapped response plan is
    // not a request body shape, and the caller keeps Pydantic for those.
    let Some(body) = plan.body() else {
        return Ok(None);
    };
    let mut parser = Parser {
        input,
        position: 0,
        sink: Scan,
        scratch: Vec::new(),
    };
    parser.skip_whitespace();
    let Some(built) = parser.typed_model(py, body, 0)? else {
        return Ok(None);
    };
    parser.skip_whitespace();
    if parser.position != input.len() {
        return Err(ParseError);
    }
    Ok(Some(built))
}

/// A leaf read while scanning. Containers and strings are only validated.
enum Leaf {
    Int(i64),
    UInt(u64),
    Float(f64),
    Other,
}

/// A sink that keeps nothing, for validation and for skipping ignored keys.
struct Scan;

impl Sink for Scan {
    type Out = Leaf;
    const COLLECT_CONTAINERS: bool = false;
    fn null(&mut self) -> Result<Leaf, ParseError> {
        Ok(Leaf::Other)
    }
    fn boolean(&mut self, _: bool) -> Result<Leaf, ParseError> {
        Ok(Leaf::Other)
    }
    fn int(&mut self, value: i64) -> Result<Leaf, ParseError> {
        Ok(Leaf::Int(value))
    }
    fn uint(&mut self, value: u64) -> Result<Leaf, ParseError> {
        Ok(Leaf::UInt(value))
    }
    fn big_int(&mut self, _: &str) -> Result<Leaf, ParseError> {
        Ok(Leaf::Other)
    }
    fn float(&mut self, value: f64) -> Result<Leaf, ParseError> {
        Ok(Leaf::Float(value))
    }
    fn string(&mut self, _: &str) -> Result<Leaf, ParseError> {
        Ok(Leaf::Other)
    }
    fn array(&mut self, _: Vec<Leaf>) -> Result<Leaf, ParseError> {
        Ok(Leaf::Other)
    }
    fn object(&mut self, _: Vec<Member<'_, Leaf>>) -> Result<Leaf, ParseError> {
        Ok(Leaf::Other)
    }
}

/// `Ok(None)` means the value does not have the expected exact type; the
/// position is then meaningless and the caller gives up.
type Typed<'py> = Result<Option<Bound<'py, PyAny>>, ParseError>;

impl Parser<'_, Scan> {
    fn typed<'py>(&mut self, py: Python<'py>, node: &Node, depth: usize) -> Typed<'py> {
        // The same limit as `value`, so a body the typed parse accepts is
        // one the scanner accepts.
        if depth > MAX_DEPTH {
            return Err(ParseError);
        }
        self.skip_whitespace();
        let next = self.peek().ok_or(ParseError)?;
        Ok(Some(match (node, next) {
            (Node::Nullable(_), b'n') => {
                self.literal(b"null")?;
                py.None().into_bound(py)
            }
            (Node::Nullable(inner), _) => return self.typed(py, inner, depth),
            (Node::Int, b'-' | b'0'..=b'9') => match self.number()? {
                Leaf::Int(value) => PyInt::new(py, value).into_any(),
                Leaf::UInt(value) => PyInt::new(py, value).into_any(),
                _ => return Ok(None),
            },
            (Node::Float, b'-' | b'0'..=b'9') => {
                // Python's float(int) and Rust's `as` both round to nearest even.
                let value = match self.number()? {
                    Leaf::Int(value) => value as f64,
                    Leaf::UInt(value) => value as f64,
                    Leaf::Float(value) if value.is_finite() => value,
                    _ => return Ok(None),
                };
                PyFloat::new(py, value).into_any()
            }
            (Node::Str, b'"') => {
                self.position += 1;
                let text = match self.string()? {
                    Text::Input(start, end) => &self.input[start..end],
                    Text::Scratch => &self.scratch[..],
                };
                // SAFETY: `string` validated these bytes as UTF-8.
                let text = unsafe { std::str::from_utf8_unchecked(text) };
                // SAFETY: `new_str` returns a new reference or null with an
                // exception set.
                unsafe { Bound::from_owned_ptr_or_err(py, super::new_str(text)) }
                    .map_err(|_| ParseError)?
            }
            (Node::Bool, b't') => {
                self.literal(b"true")?;
                PyBool::new(py, true).to_owned().into_any()
            }
            (Node::Bool, b'f') => {
                self.literal(b"false")?;
                PyBool::new(py, false).to_owned().into_any()
            }
            (Node::List(inner), b'[') => return self.typed_list(py, inner, depth),
            (Node::Model(body), b'{') => return self.typed_model(py, body, depth),
            _ => return Ok(None),
        }))
    }

    fn typed_list<'py>(&mut self, py: Python<'py>, inner: &Node, depth: usize) -> Typed<'py> {
        self.position += 1;
        let list = PyList::empty(py);
        self.skip_whitespace();
        if self.peek() == Some(b']') {
            self.position += 1;
            return Ok(Some(list.into_any()));
        }
        loop {
            match self.typed(py, inner, depth + 1)? {
                Some(item) if list.append(&item).is_ok() => {}
                _ => return Ok(None),
            }
            self.skip_whitespace();
            match self.peek() {
                Some(b',') => self.position += 1,
                Some(b']') => {
                    self.position += 1;
                    return Ok(Some(list.into_any()));
                }
                _ => return Err(ParseError),
            }
        }
    }

    fn typed_model<'py>(&mut self, py: Python<'py>, body: &ModelBody, depth: usize) -> Typed<'py> {
        if self.peek() != Some(b'{') {
            return Ok(None);
        }
        self.position += 1;
        let mut slots: Vec<Option<Bound<'py, PyAny>>> = Vec::with_capacity(body.fields.len());
        slots.resize_with(body.fields.len(), || None);
        // Bodies mostly give a model's keys in its field order, so the field
        // after the one just read is tried before the fields are searched.
        let mut expected = 0;
        let input = self.input;
        self.skip_whitespace();
        if self.peek() == Some(b'}') {
            self.position += 1;
        } else {
            loop {
                self.skip_whitespace();
                if self.peek() != Some(b'"') {
                    return Err(ParseError);
                }
                self.position += 1;
                // The expected field's key is recognised where it stands:
                // its own bytes and the closing quote. A plain key has no
                // other spelling without escapes, and with distinct keys
                // this is the field the search finds, so nothing is scanned,
                // validated or compared for it.
                let start = self.position;
                let in_place = body
                    .fields
                    .get(expected)
                    .filter(|field| body.distinct_keys && field.plain_key)
                    .map(|field| start + field.key.len())
                    .filter(|&end| {
                        input.get(end) == Some(&b'"')
                            && input[start..end] == *body.fields[expected].key.as_bytes()
                    });
                let found = match in_place {
                    Some(end) => {
                        self.position = end + 1;
                        Some(expected)
                    }
                    None => {
                        let key = self.key()?;
                        body.fields.iter().position(|field| field.key == *key)
                    }
                };
                self.skip_whitespace();
                if self.peek() != Some(b':') {
                    return Err(ParseError);
                }
                self.position += 1;
                self.skip_whitespace();
                match found {
                    // A repeated key keeps the last value only after every
                    // value was validated; leave that to the general path.
                    Some(index) if slots[index].is_some() => return Ok(None),
                    Some(index) => {
                        expected = index + 1;
                        match self.typed(py, &body.fields[index].node, depth + 1)? {
                            Some(built) => slots[index] = Some(built),
                            None => return Ok(None),
                        }
                    }
                    None if body.forbid_extra => return Ok(None),
                    None => {
                        // Ignored, but still validated like any other value.
                        self.value(depth + 1)?;
                    }
                }
                self.skip_whitespace();
                match self.peek() {
                    Some(b',') => self.position += 1,
                    Some(b'}') => {
                        self.position += 1;
                        break;
                    }
                    _ => return Err(ParseError),
                }
            }
        }
        Ok(assemble(py, body, slots).unwrap_or(None))
    }
}

// ---------------------------------------------------------------------------
// Encoding instances
// ---------------------------------------------------------------------------

/// Encode an instance of exactly `plan`'s class, or return `None` when it needs
/// Pydantic's serializer (a subclass, a value of another type, a non-finite
/// float, an integer outside 64 bits, output over `max_bytes`, ...).
pub fn encode_model<R>(
    value: &Bound<'_, PyAny>,
    plan: &ModelPlan,
    max_bytes: usize,
    finish: impl FnOnce(&[u8]) -> R,
) -> Option<R> {
    let body = plan.body()?;
    super::with_encoder(
        value,
        max_bytes,
        // SAFETY: see `with_encoder`; models are read with the same rules as
        // containers.
        |encoder, object| unsafe { encoder.model(object, body, 0) },
        |out| finish(out),
    )
}

/// [`encode_model`] returning the output as the `Vec` it was encoded into.
pub fn encode_model_vec(
    value: &Bound<'_, PyAny>,
    plan: &ModelPlan,
    max_bytes: usize,
) -> Option<Vec<u8>> {
    let body = plan.body()?;
    super::with_encoder(
        value,
        max_bytes,
        // SAFETY: as in `encode_model`.
        |encoder, object| unsafe { encoder.model(object, body, 0) },
        super::take_output,
    )
}

/// Encode a response-model value, or return `None` when Pydantic must
/// validate and serialize it. `plan`'s root describes which shape: an exact
/// instance of the model (written from its `__dict__`) or an exact dict whose
/// values pass the model's exact type checks (validated and written in one
/// pass); a ``list[...]`` of such values; a string-keyed dict of them, in the
/// dict's order; or the same with ``... | None``, where `None` writes `null`.
/// Anything else refuses: a subclass or non-exact type, a coercion, a missing
/// required key, an extra key under `extra="forbid"`, a nesting over the
/// depth limit, or output over `max_bytes`. Everything that passes produces
/// the same bytes as the Pydantic pipeline's `dump_json`.
pub fn encode_response<R>(
    value: &Bound<'_, PyAny>,
    plan: &ModelPlan,
    max_bytes: usize,
    finish: impl FnOnce(&[u8]) -> R,
) -> Option<R> {
    super::with_encoder(
        value,
        max_bytes,
        // SAFETY: see `with_encoder`; values are read with the same rules as
        // containers (a dict is snapshotted on free-threaded builds), and no
        // Python code runs between the entry checks and the write loop.
        |encoder, object| unsafe { encoder.response_value(object, plan, 0) },
        |out| finish(out),
    )
}

/// [`encode_response`] returning the output as the `Vec` it was encoded into.
pub fn encode_response_vec(
    value: &Bound<'_, PyAny>,
    plan: &ModelPlan,
    max_bytes: usize,
) -> Option<Vec<u8>> {
    super::with_encoder(
        value,
        max_bytes,
        // SAFETY: as in `encode_response`.
        |encoder, object| unsafe { encoder.response_value(object, plan, 0) },
        super::take_output,
    )
}

/// A ``list[body]`` convenience for a plain body plan: the value must be an
/// exact list whose items are exact dicts or instances of `body`, validated
/// and written in one pass (a ``list[...]`` response plan does this through
/// its own root; the list-taking callers that predate wrapped plans keep
/// their old shape). Each item is read like a response value, so dict items
/// are snapshotted on free-threaded builds.
pub fn encode_body_list_vec(
    value: &Bound<'_, PyAny>,
    body: &Arc<ModelBody>,
    max_bytes: usize,
) -> Option<Vec<u8>> {
    // A synthesized root: the body wrapped in one list node, sharing its
    // reference for the read side. Nothing Python-visible changes.
    let root = Node::List(Box::new(Node::Model(Arc::clone(body))));
    super::with_encoder(
        value,
        max_bytes,
        // SAFETY: as in `encode_response`: the same reading rules (a dict is
        // snapshotted under its critical section on free-threaded builds).
        |encoder, object| unsafe { encoder.dict_value(object, &root, 0) },
        super::take_output,
    )
}

impl Encoder<'_, '_> {
    unsafe fn node(
        &mut self,
        object: *mut ffi::PyObject,
        node: &Node,
        depth: usize,
    ) -> Result<(), Bail> {
        unsafe {
            let kind = ffi::Py_TYPE(object);
            match node {
                Node::Int if kind == &raw mut ffi::PyLong_Type => self.integer(object),
                Node::Float if kind == &raw mut ffi::PyFloat_Type => self.value(object, depth),
                // An exact int carries a float field's value the way Pydantic
                // serialises it: as a float (the default 2 of a `float` field
                // is written 2.0). An int that does not fit a double refuses.
                Node::Float if kind == &raw mut ffi::PyLong_Type => {
                    let value = ffi::PyLong_AsDouble(object);
                    if value == -1.0 && !ffi::PyErr_Occurred().is_null() {
                        ffi::PyErr_Clear();
                        return Err(Bail);
                    }
                    if !value.is_finite() {
                        return Err(Bail);
                    }
                    let mut buffer = zmij::Buffer::new();
                    self.out
                        .extend_from_slice(buffer.format_finite(value).as_bytes());
                    self.check()
                }
                Node::Str if kind == &raw mut ffi::PyUnicode_Type => self.string(object),
                Node::Bool if object == ffi::Py_True() || object == ffi::Py_False() => {
                    self.value(object, depth)
                }
                Node::Nullable(_) if object == ffi::Py_None() => {
                    self.out.extend_from_slice(b"null");
                    self.check()
                }
                Node::Nullable(inner) => self.node(object, inner, depth),
                Node::List(inner) if kind == &raw mut ffi::PyList_Type => {
                    self.typed_list(object, inner, depth)
                }
                Node::Model(body) => self.model(object, body, depth),
                _ => Err(Bail),
            }
        }
    }

    unsafe fn model(
        &mut self,
        object: *mut ffi::PyObject,
        body: &ModelBody,
        depth: usize,
    ) -> Result<(), Bail> {
        if depth > MAX_DEPTH {
            return Err(Bail);
        }
        unsafe {
            if ffi::Py_TYPE(object) != body.cls.as_ptr().cast::<ffi::PyTypeObject>() {
                return Err(Bail);
            }
            let values = ffi::PyObject_GenericGetDict(object, std::ptr::null_mut());
            if values.is_null() {
                ffi::PyErr_Clear();
                return Err(Bail);
            }
            let result = self.model_fields(values, body, depth);
            #[cfg(not(Py_GIL_DISABLED))]
            ffi::Py_DECREF(values);
            #[cfg(Py_GIL_DISABLED)]
            self.held.push(values);
            result
        }
    }

    /// Write the fields of a model's `__dict__`. A dict that does not hold
    /// exactly the plan's fields (one was deleted or another attribute added)
    /// takes Pydantic's path.
    unsafe fn model_fields(
        &mut self,
        values: *mut ffi::PyObject,
        body: &ModelBody,
        depth: usize,
    ) -> Result<(), Bail> {
        unsafe {
            if ffi::PyDict_Size(values) as usize != body.fields.len() {
                return Err(Bail);
            }
            let mut in_order = [std::ptr::null_mut(); ORDERED_FIELDS];
            if fields_in_order(values, body, &mut in_order) {
                self.out.push(b'{');
                for (position, (field, &item)) in body.fields.iter().zip(&in_order).enumerate() {
                    if position > 0 {
                        self.out.push(b',');
                    }
                    // Check before copying: a fixed escaped alias can be much
                    // larger than the remaining response budget.
                    if field.prefix.len() > self.max_bytes.saturating_sub(self.out.len()) {
                        return Err(Bail);
                    }
                    self.out.extend_from_slice(&field.prefix);
                    self.node(item, &field.node, depth + 1)?;
                }
                self.out.push(b'}');
                return self.check();
            }
            if !self.exact_keys(values) {
                return Err(Bail);
            }
            // Item references for this model share the entries stack, so
            // nested models never allocate: this model's are base..base+n.
            let base = self.entries.len();
            for field in &body.fields {
                let item = self.field_item(values, field.name.as_ptr())?;
                self.entries.push(item);
            }
            self.out.push(b'{');
            for (position, field) in body.fields.iter().enumerate() {
                if position > 0 {
                    self.out.push(b',');
                }
                if field.prefix.len() > self.max_bytes.saturating_sub(self.out.len()) {
                    return Err(Bail);
                }
                self.out.extend_from_slice(&field.prefix);
                let item = self.entries[base + position];
                self.node(item, &field.node, depth + 1)?;
            }
            self.entries.truncate(base);
            self.out.push(b'}');
            self.check()
        }
    }

    /// Whether every key of `values` is an exact `str`. Looking up an exact
    /// `str` among exact `str` keys compares them in C, so no Python `__eq__`
    /// can run, and replace a value already borrowed, during the lookups.
    #[cfg(not(Py_GIL_DISABLED))]
    unsafe fn exact_keys(&mut self, values: *mut ffi::PyObject) -> bool {
        unsafe { all_keys_exact(values) }
    }

    #[cfg(Py_GIL_DISABLED)]
    unsafe fn exact_keys(&mut self, values: *mut ffi::PyObject) -> bool {
        // Values are held as strong references here, but keep one rule for
        // both builds; iterating needs the dict's critical section.
        unsafe { self.locked(values, |_| all_keys_exact(values)) }
    }

    #[cfg(not(Py_GIL_DISABLED))]
    unsafe fn field_item(
        &mut self,
        values: *mut ffi::PyObject,
        name: *mut ffi::PyObject,
    ) -> Result<*mut ffi::PyObject, Bail> {
        unsafe {
            // Borrowed: the instance's dict keeps it alive while the GIL is held.
            let item = ffi::PyDict_GetItemWithError(values, name);
            if item.is_null() {
                ffi::PyErr_Clear();
                return Err(Bail);
            }
            Ok(item)
        }
    }

    #[cfg(Py_GIL_DISABLED)]
    unsafe fn field_item(
        &mut self,
        values: *mut ffi::PyObject,
        name: *mut ffi::PyObject,
    ) -> Result<*mut ffi::PyObject, Bail> {
        unsafe {
            // A strong reference, released after encoding, so another thread
            // replacing the attribute cannot free it mid-encode.
            let mut item = std::ptr::null_mut();
            if ffi::compat::PyDict_GetItemRef(values, name, &mut item) != 1 {
                ffi::PyErr_Clear();
                return Err(Bail);
            }
            self.held.push(item);
            Ok(item)
        }
    }

    #[cfg(not(Py_GIL_DISABLED))]
    unsafe fn typed_list(
        &mut self,
        object: *mut ffi::PyObject,
        inner: &Node,
        depth: usize,
    ) -> Result<(), Bail> {
        if depth > MAX_DEPTH {
            return Err(Bail);
        }
        unsafe {
            let length = ffi::PyList_GET_SIZE(object);
            self.out.push(b'[');
            for index in 0..length {
                if index > 0 {
                    self.out.push(b',');
                }
                self.node(ffi::PyList_GET_ITEM(object, index), inner, depth + 1)?;
            }
            self.out.push(b']');
            self.check()
        }
    }

    #[cfg(Py_GIL_DISABLED)]
    unsafe fn typed_list(
        &mut self,
        object: *mut ffi::PyObject,
        inner: &Node,
        depth: usize,
    ) -> Result<(), Bail> {
        if depth > MAX_DEPTH {
            return Err(Bail);
        }
        unsafe {
            let (start, end) = self.hold_list(object)?;
            self.out.push(b'[');
            for index in start..end {
                if index > start {
                    self.out.push(b',');
                }
                self.node(self.held[index], inner, depth + 1)?;
            }
            self.out.push(b']');
            self.check()
        }
    }

    /// `"{"key": value, ...}`: an exact dict whose keys are the dict's own
    /// `str` keys (Pydantic's `dict[str, T]` writes them in the dict's order;
    /// a key that is not an exact `str` refuses, and the caller keeps
    /// Pydantic, which would coerce or report it). Values follow `inner`.
    /// Free-threaded builds encode a private snapshot of the dict (PyDict_Copy
    /// takes the source's critical section), the way [`Self::model_dict`]
    /// does; nested mutable values are snapshotted by their encoders.
    unsafe fn str_map(
        &mut self,
        object: *mut ffi::PyObject,
        inner: &Node,
        depth: usize,
    ) -> Result<(), Bail> {
        if depth > MAX_DEPTH {
            return Err(Bail);
        }
        unsafe {
            #[cfg(Py_GIL_DISABLED)]
            let object = self.locked(object, |this| {
                if ffi::PyDict_Size(object) as usize
                    > this
                        .out
                        .len()
                        .saturating_add(this.max_bytes.saturating_sub(this.out.len()))
                {
                    return Err(Bail);
                }
                let snapshot = ffi::PyDict_Copy(object);
                if snapshot.is_null() {
                    ffi::PyErr_Clear();
                    return Err(Bail);
                }
                this.held.push(snapshot);
                Ok(snapshot)
            })?;
            #[cfg(not(Py_GIL_DISABLED))]
            {
                let length = ffi::PyDict_Size(object) as usize;
                if length.saturating_mul(5).saturating_add(1)
                    > self.max_bytes.saturating_sub(self.out.len())
                {
                    // n entries need at least `{` + n * `":0` + (n - 1) commas + `}`.
                    return Err(Bail);
                }
            }
            self.out.push(b'{');
            let mut position: ffi::Py_ssize_t = 0;
            let mut key: *mut ffi::PyObject = std::ptr::null_mut();
            let mut item: *mut ffi::PyObject = std::ptr::null_mut();
            let mut first = true;
            while ffi::PyDict_Next(object, &mut position, &mut key, &mut item) != 0 {
                if ffi::Py_TYPE(key) != &raw mut ffi::PyUnicode_Type {
                    return Err(Bail);
                }
                if !first {
                    self.out.push(b',');
                }
                first = false;
                let text = self.utf8(key)?;
                write_escaped(self.out, text, self.max_bytes)?;
                self.out.push(b':');
                self.check()?;
                self.dict_value(item, inner, depth + 1)?;
            }
            self.out.push(b'}');
            self.check()
        }
    }

    /// Write an exact dict as one model value, validating its values against
    /// the plan: every key is an exact `str` (one walk), each declared field
    /// is then looked up by its interned key and written through its node,
    /// and absent ones are the field's pre-encoded default, or the model
    /// keeps Pydantic's path when it has none. Keys the plan does not declare
    /// are ignored like any extra key unless `extra="forbid"`.
    unsafe fn model_dict(
        &mut self,
        object: *mut ffi::PyObject,
        body: &ModelBody,
        depth: usize,
    ) -> Result<(), Bail> {
        unsafe {
            if depth > MAX_DEPTH {
                return Err(Bail);
            }
            // PyDict_Copy takes the source critical section. The private
            // snapshot owns its entries and cannot be mutated by Python;
            // nested mutable values are snapshotted by their encoders too.
            #[cfg(Py_GIL_DISABLED)]
            let object = self.locked(object, |this| {
                if ffi::PyDict_Size(object) as usize > this.max_bytes.saturating_sub(this.out.len())
                {
                    return Err(Bail);
                }
                let snapshot = ffi::PyDict_Copy(object);
                if snapshot.is_null() {
                    ffi::PyErr_Clear();
                    return Err(Bail);
                }
                this.held.push(snapshot);
                Ok(snapshot)
            })?;
            let mut position: ffi::Py_ssize_t = 0;
            let mut key: *mut ffi::PyObject = std::ptr::null_mut();
            let mut item: *mut ffi::PyObject = std::ptr::null_mut();
            while ffi::PyDict_Next(object, &mut position, &mut key, &mut item) != 0 {
                if ffi::Py_TYPE(key) != &raw mut ffi::PyUnicode_Type {
                    return Err(Bail);
                }
                if body.forbid_extra {
                    let text = self.utf8(key)?;
                    if !body.fields.iter().any(|field| field.key.as_bytes() == text) {
                        return Err(Bail);
                    }
                }
            }
            self.out.push(b'{');
            // Fields in declaration order, so the bytes match
            // model_dump_json's key order.
            for (position, field) in body.fields.iter().enumerate() {
                if position > 0 {
                    self.out.push(b',');
                }
                // The prefix is fixed at plan build time; check before copying.
                if field.prefix.len() > self.max_bytes.saturating_sub(self.out.len()) {
                    return Err(Bail);
                }
                self.out.extend_from_slice(&field.prefix);
                // SAFETY: `object` is a live dict the caller is encoding, and
                // every key is an exact `str`, so the lookup runs no Python
                // code and the GIL keeps the dict unchanged meanwhile.
                let item = ffi::PyDict_GetItemWithError(object, field.key_py.as_ptr());
                if item.is_null() {
                    if !ffi::PyErr_Occurred().is_null() {
                        ffi::PyErr_Clear();
                        return Err(Bail);
                    }
                    match &field.default_bytes {
                        Some(bytes) => self.out.extend_from_slice(bytes),
                        None => return Err(Bail),
                    }
                } else {
                    self.dict_value(item, &field.node, depth + 1)?;
                }
                self.check()?;
            }
            self.out.push(b'}');
            self.check()
        }
    }

    /// The root of one response-model value: a model (an exact instance, or
    /// an exact dict validated field by field), a list of them, a string-keyed
    /// dict of them, or the same with ``... | None`` (a `None` writes `null`).
    unsafe fn response_value(
        &mut self,
        object: *mut ffi::PyObject,
        plan: &ModelPlan,
        depth: usize,
    ) -> Result<(), Bail> {
        // SAFETY: as `dict_value`; the same reading rules apply at the root.
        unsafe { self.dict_value(object, &plan.root, depth) }
    }

    /// Write a value against a node: nested models accept exact dicts
    /// (recursed through [`Self::model_dict`]) as well as instances of the
    /// model; optionals peel their wrapper; string-keyed dicts recurse
    /// through [`Self::str_map`]; anything else is written like
    /// [`Self::node`] (so a value without its field's exact type refuses the
    /// whole value).
    unsafe fn dict_value(
        &mut self,
        object: *mut ffi::PyObject,
        node: &Node,
        depth: usize,
    ) -> Result<(), Bail> {
        if depth > MAX_DEPTH {
            return Err(Bail);
        }
        unsafe {
            match node {
                Node::Nullable(_) if object == ffi::Py_None() => {
                    self.out.extend_from_slice(b"null");
                    self.check()
                }
                Node::Nullable(inner) => self.dict_value(object, inner, depth),
                Node::List(inner) if ffi::Py_TYPE(object) == &raw mut ffi::PyList_Type => {
                    #[cfg(not(Py_GIL_DISABLED))]
                    let (start, end) = (0, ffi::PyList_GET_SIZE(object) as usize);
                    #[cfg(Py_GIL_DISABLED)]
                    let (start, end) = self.hold_list(object)?;
                    self.out.push(b'[');
                    for index in start..end {
                        if index > start {
                            self.out.push(b',');
                        }
                        #[cfg(not(Py_GIL_DISABLED))]
                        let item = ffi::PyList_GET_ITEM(object, index as ffi::Py_ssize_t);
                        #[cfg(Py_GIL_DISABLED)]
                        let item = self.held[index];
                        self.dict_value(item, inner, depth + 1)?;
                    }
                    self.out.push(b']');
                    self.check()
                }
                Node::StrMap(inner) if ffi::Py_TYPE(object) == &raw mut ffi::PyDict_Type => {
                    self.str_map(object, inner, depth)
                }
                Node::Model(body) => {
                    if ffi::Py_TYPE(object) == body.cls.as_ptr().cast::<ffi::PyTypeObject>() {
                        self.model(object, body, depth)
                    } else if ffi::Py_TYPE(object) == &raw mut ffi::PyDict_Type {
                        self.model_dict(object, body, depth)
                    } else {
                        Err(Bail)
                    }
                }
                _ => self.node(object, node, depth),
            }
        }
    }
}

/// Most fields a model may have for [`fields_in_order`] to read its values.
const ORDERED_FIELDS: usize = 16;

/// Whether the dict `values` holds the plan's field name objects themselves,
/// in field order, as the `__dict__` of an instance built by `assemble` (or
/// by pydantic-core, which sets the same interned names in the same order)
/// does. If so, `items[i]` is set to the value of field `i`, borrowed from
/// the dict, so the encoder reads each value in the one pass over the dict
/// rather than looking every field up by name. Runs no Python code. On a
/// free-threaded build it always declines: values there are read as strong
/// references (see `field_item`).
#[allow(unused_variables)]
unsafe fn fields_in_order(
    values: *mut ffi::PyObject,
    body: &ModelBody,
    items: &mut [*mut ffi::PyObject; ORDERED_FIELDS],
) -> bool {
    #[cfg(Py_GIL_DISABLED)]
    return false;
    #[cfg(not(Py_GIL_DISABLED))]
    unsafe {
        if body.fields.len() > ORDERED_FIELDS {
            return false;
        }
        let mut position: ffi::Py_ssize_t = 0;
        let mut key: *mut ffi::PyObject = std::ptr::null_mut();
        let mut item: *mut ffi::PyObject = std::ptr::null_mut();
        let mut index = 0;
        while ffi::PyDict_Next(values, &mut position, &mut key, &mut item) != 0 {
            // Identity with the plan's (exact `str`) name, so no comparison
            // or Python `__eq__` is involved.
            match body.fields.get(index) {
                Some(field) if field.name.as_ptr() == key => items[index] = item,
                _ => return false,
            }
            index += 1;
        }
        index == body.fields.len()
    }
}

/// Whether every key of the dict `values` is an exact `str`. Runs no Python code.
unsafe fn all_keys_exact(values: *mut ffi::PyObject) -> bool {
    unsafe {
        let mut position: ffi::Py_ssize_t = 0;
        let mut key: *mut ffi::PyObject = std::ptr::null_mut();
        let mut item: *mut ffi::PyObject = std::ptr::null_mut();
        while ffi::PyDict_Next(values, &mut position, &mut key, &mut item) != 0 {
            if ffi::Py_TYPE(key) != &raw mut ffi::PyUnicode_Type {
                return false;
            }
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validation_scan_matches_value_parsing_for_documents_and_corruptions() {
        let mut documents = vec![
            r#"{"z":[true,null,{},[],"é🙂"],"a":{"escaped\nkey":-1.25e2}}"#
                .as_bytes()
                .to_vec(),
            br#"{"duplicate":1,"duplicate":[2,{"duplicate":false}]}"#.to_vec(),
            br#"{"escape":"\uD83D\uDE42\u0000\b\f\n\r\t\/\\\""}"#.to_vec(),
            br#"{"duplicate":1e999,"duplicate":1}"#.to_vec(),
            br#"{"duplicate":1,"duplicate":"\uD800"}"#.to_vec(),
            br#"[0,-0,9223372036854775807,18446744073709551615,18446744073709551616]"#.to_vec(),
            br#"{"\udfff":0}"#.to_vec(),
            b"{\"invalid\xff\":1}".to_vec(),
        ];
        for depth in [MAX_DEPTH, MAX_DEPTH + 1, MAX_DEPTH + 2] {
            documents.push(format!("{}0{}", "[".repeat(depth), "]".repeat(depth)).into_bytes());
        }
        for digits in [
            super::super::MAX_INTEGER_DIGITS,
            super::super::MAX_INTEGER_DIGITS + 1,
        ] {
            documents.push(format!("{{\"integer\":{}}}", "9".repeat(digits)).into_bytes());
        }
        for document in documents {
            let check = |input: &[u8]| {
                assert_eq!(
                    validate_json(input).is_ok(),
                    super::super::parse_value(input).is_ok(),
                    "input {:?}",
                    String::from_utf8_lossy(input)
                );
            };
            check(&document);
            // Exercise delimiters, truncated escapes/UTF-8 and malformed
            // numbers without making the large integer cases quadratic.
            if document.len() < 256 {
                for end in 0..document.len() {
                    check(&document[..end]);
                }
                for index in 0..document.len() {
                    for byte in [b'{', b'}', b'[', b']', b',', b':', b'"', b'\\', b'0', 0xff] {
                        let mut corrupted = document.clone();
                        corrupted[index] = byte;
                        check(&corrupted);
                    }
                }
            }
        }
    }

    #[test]
    fn model_keys_are_escaped_once_in_field_order() {
        Python::attach(|py| {
            let locals = PyDict::new(py);
            py.run(
                cr#"class Record: pass
aliases = [('unicode', '\u00e9\U0001f642'), ('empty', ''), ('nul', '\x00'),
           ('newline', '\n'), ('quote', '\"'), ('slash', '\\'), ('ascii', 'a')]
value = Record()
for index, (name, alias) in enumerate(aliases):
    setattr(value, name, index)
spec = ('model', Record, False,
        tuple((name, 'input_' + name, alias, ('int',), False, None)
              for name, alias in aliases))
"#,
                Some(&locals),
                None,
            )
            .unwrap();
            let plan = ModelPlan::from_spec(&locals.get_item("spec").unwrap().unwrap()).unwrap();
            let value = locals.get_item("value").unwrap().unwrap();
            let expected = r#"{"é🙂":0,"":1,"\u0000":2,"\n":3,"\"":4,"\\":5,"a":6}"#
                .as_bytes()
                .to_vec();
            assert_eq!(
                encode_model(&value, &plan, usize::MAX, <[u8]>::to_vec),
                Some(expected)
            );
        });
    }

    #[test]
    fn escaped_model_keys_keep_exact_response_byte_boundaries() {
        Python::attach(|py| {
            let locals = PyDict::new(py);
            py.run(
                cr#"class Child: pass
class Root: pass
child = Child()
child.name = 'quoted\"\n\u00e9'
value = Root()
value.child = child
value.flag = True
child_spec = ('model', Child, False,
              (('name', 'input_name', '\x01\"\\\u00e9', ('str',), False, None),))
spec = ('model', Root, False,
        (('flag', 'input_flag', 'z', ('bool',), False, None),
         ('child', 'input_child', '\n', child_spec, False, None)))
dumped = {'z': True, '\n': {'\x01\"\\\u00e9': child.name}}
"#,
                Some(&locals),
                None,
            )
            .unwrap();
            let plan = ModelPlan::from_spec(&locals.get_item("spec").unwrap().unwrap()).unwrap();
            let value = locals.get_item("value").unwrap().unwrap();
            let dumped = locals.get_item("dumped").unwrap().unwrap();
            let expected = super::super::encode_plain(&dumped, usize::MAX, <[u8]>::to_vec).unwrap();
            for limit in 0..=expected.len() + 1 {
                let encoded = encode_model(&value, &plan, limit, <[u8]>::to_vec);
                assert_eq!(
                    encoded.as_ref(),
                    (limit >= expected.len()).then_some(&expected),
                    "limit {limit}"
                );
            }
        });
    }

    #[test]
    fn oversized_model_alias_is_rejected_before_copying() {
        Python::attach(|py| {
            let locals = PyDict::new(py);
            py.run(
                cr#"class Record: pass
value = Record()
value.field = 1
spec = ('model', Record, False,
        (('field', 'input', '\x00' * 10000, ('int',), False, None),))
"#,
                Some(&locals),
                None,
            )
            .unwrap();
            let plan = ModelPlan::from_spec(&locals.get_item("spec").unwrap().unwrap()).unwrap();
            let value = locals.get_item("value").unwrap().unwrap();
            super::super::SCRATCH.with(|cell| {
                *cell.borrow_mut() = <super::super::Scratch as std::default::Default>::default()
            });
            assert!(encode_model(&value, &plan, 64, |_| panic!("over budget")).is_none());
            super::super::SCRATCH.with(|cell| {
                let scratch = cell.borrow();
                assert!(scratch.out.len() <= 64);
                assert!(scratch.out.capacity() <= 64);
            });
        });
    }

    #[test]
    fn instance_dicts_in_any_key_order_encode_the_same() {
        Python::attach(|py| {
            let locals = PyDict::new(py);
            py.run(
                cr#"class Record: pass
in_order = Record()
in_order.a = 1
in_order.b = 'x'
reversed_ = Record()
reversed_.b = 'x'
reversed_.a = 1
spec = ('model', Record, False,
        (('a', 'a', 'a', ('int',), False, None),
         ('b', 'b', 'b', ('str',), False, None)))
"#,
                Some(&locals),
                None,
            )
            .unwrap();
            let plan = ModelPlan::from_spec(&locals.get_item("spec").unwrap().unwrap()).unwrap();
            for name in ["in_order", "reversed_"] {
                let value = locals.get_item(name).unwrap().unwrap();
                assert_eq!(
                    encode_model(&value, &plan, usize::MAX, <[u8]>::to_vec).as_deref(),
                    Some(&br#"{"a":1,"b":"x"}"#[..]),
                    "{name}"
                );
            }
            // A missing field still takes Pydantic's path.
            py.run(c"del in_order.b", Some(&locals), None).unwrap();
            let value = locals.get_item("in_order").unwrap().unwrap();
            assert!(encode_model(&value, &plan, usize::MAX, <[u8]>::to_vec).is_none());
        });
    }

    #[test]
    fn parsed_ascii_and_unicode_strings_build_equal_python_strings() {
        Python::attach(|py| {
            let input = r#"["plain","éx","esc\n\u00e9",""]"#;
            let parsed = super::super::parse_python(py, input.as_bytes()).unwrap();
            let expected = py
                .eval(c"['plain', '\\u00e9x', 'esc\\n\\u00e9', '']", None, None)
                .unwrap();
            assert!(parsed.eq(&expected).unwrap());
        });
    }

    #[test]
    fn duplicate_serialization_aliases_are_still_rejected() {
        Python::attach(|py| {
            let spec = py
                .eval(
                    c"('model', type('Record', (), {}), False,
                         (('a', 'a', '\\n', ('int',), False, None),
                          ('b', 'b', '\\n', ('int',), False, None)))",
                    None,
                    None,
                )
                .unwrap();
            assert!(ModelPlan::from_spec(&spec).is_err());
        });
    }

    /// What a body builds, as the reprs of its field values and of the
    /// fields it set, or `None` when it is left to Pydantic.
    ///
    /// The stand-in classes here have no slots, unlike a Pydantic model, so
    /// Pydantic's three bookkeeping attributes are stored in the instance
    /// `__dict__` beside the fields (on CPython 3.13 they stay in the
    /// instance's inline values instead and the dict shows the fields alone).
    /// Only the fields are reported, so the tests read the same everywhere.
    fn built(value: Option<Bound<'_, PyAny>>) -> Option<(String, String)> {
        value.map(|instance| {
            let attributes = instance.getattr("__dict__").unwrap();
            let values = PyDict::new(instance.py());
            for (name, value) in attributes.cast::<PyDict>().unwrap().iter() {
                if !name.to_string().starts_with("__pydantic_") {
                    values.set_item(name, value).unwrap();
                }
            }
            let mut set: Vec<String> = instance
                .getattr("__pydantic_fields_set__")
                .unwrap()
                .try_iter()
                .unwrap()
                .map(|name| name.unwrap().to_string())
                .collect();
            set.sort();
            (values.to_string(), set.join(","))
        })
    }

    /// Every sequence, repeats included, of up to `length` of `keys`.
    fn sequences(keys: &[&'static str], length: usize) -> Vec<Vec<&'static str>> {
        let mut all = vec![Vec::new()];
        let mut last = all.clone();
        for _ in 0..length {
            let mut next = Vec::new();
            for prefix in &last {
                for key in keys {
                    let mut longer = prefix.clone();
                    longer.push(*key);
                    next.push(longer);
                }
            }
            all.extend(next.iter().cloned());
            last = next;
        }
        all
    }

    /// The body `{"<key>": 10, "<key>": 11, ...}` for `keys`.
    fn numbered(keys: &[&str]) -> String {
        let members: Vec<String> = keys
            .iter()
            .enumerate()
            .map(|(index, key)| format!("\"{key}\": {}", index + 10))
            .collect();
        format!("{{{}}}", members.join(", "))
    }

    const MODEL_SPECS: &std::ffi::CStr = cr#"class Record: pass
def spec(keys):
    return ('model', Record, False,
            tuple((name, key, name, ('int',), True, default)
                  for default, (name, key) in enumerate(keys)))
distinct = spec([('a', 'ka'), ('b', 'kb'), ('c', 'kc'), ('d', 'kd')])
shared = spec([('a', 'ka'), ('b', 'ka'), ('c', 'kc'), ('d', 'ka')])
"#;

    #[test]
    fn keys_in_any_order_reach_the_field_the_value_path_gives_them() {
        Python::attach(|py| {
            let locals = PyDict::new(py);
            py.run(MODEL_SPECS, Some(&locals), None).unwrap();
            let plan =
                ModelPlan::from_spec(&locals.get_item("distinct").unwrap().unwrap()).unwrap();
            assert!(plan.body().unwrap().distinct_keys);
            let mut instances = 0;
            for keys in sequences(&["ka", "kb", "kc", "kd", "other"], 4) {
                let body = numbered(&keys);
                let value = super::super::parse_value(body.as_bytes()).unwrap();
                let expected = built(build_model(py, &plan, &value));
                let parsed = parse_model(py, &plan, body.as_bytes()).unwrap();
                // The typed parser gives up at a repeated field key, where
                // the value path keeps the last value; its caller then takes
                // the value path, so the result is the same.
                let repeated = (1..keys.len())
                    .any(|index| keys[index] != "other" && keys[..index].contains(&keys[index]));
                assert_eq!(parsed.is_none(), repeated, "{body}");
                if let Some(parsed) = parsed {
                    assert_eq!(built(Some(parsed)), expected, "{body}");
                    instances += 1;
                }
            }
            assert!(instances > 100, "{instances}");
        });
    }

    /// Keys the parser must not take for one another, or recognise in place
    /// when they are not written plainly: a key that starts another, keys
    /// that need escaping, a non-ASCII key and the empty key.
    #[test]
    fn awkward_keys_are_read_as_the_value_path_reads_them() {
        const KEYS: [&str; 7] = ["ab", "a", "a\"b", "a\\b", "\n", "é", ""];
        Python::attach(|py| {
            let locals = PyDict::new(py);
            py.run(
                cr#"class Record: pass
keys = ['ab', 'a', 'a"b', 'a\\b', '\n', '\u00e9', '']
spec = ('model', Record, False,
        tuple(('f%d' % index, key, 'f%d' % index, ('int',), True, -index)
              for index, key in enumerate(keys)))
"#,
                Some(&locals),
                None,
            )
            .unwrap();
            let plan = ModelPlan::from_spec(&locals.get_item("spec").unwrap().unwrap()).unwrap();
            let body = plan.body().unwrap();
            assert!(body.distinct_keys);
            let plain: Vec<bool> = body.fields.iter().map(|field| field.plain_key).collect();
            assert_eq!(plain, [true, true, false, false, false, true, true]);
            // Escaped as JSON requires, and with every character escaped.
            let escaped = |key: &str| serde_json::to_string(key).unwrap();
            let hexed = |key: &str| {
                let units: String = key
                    .encode_utf16()
                    .map(|unit| format!("\\u{unit:04x}"))
                    .collect();
                format!("\"{units}\"")
            };
            let mut instances = 0;
            for keys in sequences(&KEYS, 3) {
                for spell in [&escaped as &dyn Fn(&str) -> String, &hexed] {
                    let members: Vec<String> = keys
                        .iter()
                        .enumerate()
                        .map(|(index, key)| format!("{}:{}", spell(key), index + 10))
                        .collect();
                    let text = format!("{{{}}}", members.join(","));
                    let value = super::super::parse_value(text.as_bytes()).unwrap();
                    let expected = built(build_model(py, &plan, &value));
                    let parsed = parse_model(py, &plan, text.as_bytes()).unwrap();
                    let repeated =
                        (1..keys.len()).any(|index| keys[..index].contains(&keys[index]));
                    assert_eq!(parsed.is_none(), repeated, "{text}");
                    if let Some(parsed) = parsed {
                        assert_eq!(built(Some(parsed)), expected, "{text}");
                        instances += 1;
                    }
                }
            }
            assert!(instances > 400, "{instances}");
            // Written without their escapes, such keys are other keys or no
            // JSON at all, as the scanner finds them.
            for text in [
                "{\"a\"b\":1}",
                "{\"a\\b\":1}",
                "{\"\n\":1}",
                "{\"ab\":1,\"a\"b\":2}",
                "{\"ab",
                "{\"ab\"",
                "{\"a",
                "{\"",
                "{\"abc\":1}",
                "{\"ab\" :1 , \"a\":2}",
                "{\"\\u00e9\":1,\"\u{e9}\":2}",
            ] {
                let scanned = super::super::parse_value(text.as_bytes());
                match parse_model(py, &plan, text.as_bytes()) {
                    Err(_) => assert!(scanned.is_err(), "{text:?}"),
                    Ok(None) => assert!(scanned.is_ok(), "{text:?}"),
                    Ok(Some(parsed)) => assert_eq!(
                        built(Some(parsed)),
                        built(build_model(py, &plan, &scanned.unwrap())),
                        "{text:?}"
                    ),
                }
            }
        });
    }

    /// Plans compiled from Pydantic models never have two fields reading one
    /// key (`_model_plan.py` refuses them), but a spec may: the typed parser
    /// then gives the key to the first of them, in any key order.
    #[test]
    fn a_key_two_fields_read_goes_to_the_first_of_them() {
        Python::attach(|py| {
            let locals = PyDict::new(py);
            py.run(MODEL_SPECS, Some(&locals), None).unwrap();
            let plan = ModelPlan::from_spec(&locals.get_item("shared").unwrap().unwrap()).unwrap();
            assert!(!plan.body().unwrap().distinct_keys);
            let mut instances = 0;
            for keys in sequences(&["ka", "kb", "kc", "kd", "other"], 4) {
                let body = numbered(&keys);
                let parsed = built(parse_model(py, &plan, body.as_bytes()).unwrap());
                // `a` reads `ka` and `c` reads `kc`; `b` and `d` keep their
                // defaults, and the other keys are ignored.
                let values = |key: &str| -> Vec<usize> {
                    (0..keys.len())
                        .filter(|&index| keys[index] == key)
                        .map(|index| index + 10)
                        .collect()
                };
                let (a, c) = (values("ka"), values("kc"));
                let expected = (a.len() <= 1 && c.len() <= 1).then(|| {
                    let mut set = Vec::new();
                    if !a.is_empty() {
                        set.push("a");
                    }
                    if !c.is_empty() {
                        set.push("c");
                    }
                    (
                        format!(
                            "{{'a': {}, 'b': 1, 'c': {}, 'd': 3}}",
                            a.first().copied().unwrap_or(0),
                            c.first().copied().unwrap_or(2)
                        ),
                        set.join(","),
                    )
                });
                assert_eq!(parsed, expected, "{body}");
                instances += usize::from(expected.is_some());
            }
            assert!(instances > 100, "{instances}");
        });
    }
}
