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
    Model(Arc<ModelPlan>),
}

struct Field {
    /// Interned attribute name.
    name: Py<PyString>,
    /// The key read from request bodies (the validation alias, if any).
    key: String,
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

/// A compiled description of one plain Pydantic model.
pub struct ModelPlan {
    cls: Py<PyType>,
    forbid_extra: bool,
    fields: Vec<Field>,
    /// Every field name: the `__pydantic_fields_set__` of an instance built
    /// from a body that gave each field, copied rather than built per instance.
    all_fields: Py<PySet>,
    /// Whether this plan describes a ``list[Model]`` response annotation:
    /// its encoded side validates dicts or instances of the model from a
    /// Python list in one pass. Instances are still built per model.
    is_list: bool,
}

impl ModelPlan {
    /// Compile a plan from its Python description:
    /// `("model", cls, forbid_extra, ((name, key, output_key, node, has_default, default), ...))`,
    /// where a node is `("int",)`, `("float",)`, `("str",)`, `("bool",)`,
    /// `("list", node)`, `("nullable", node)` or a model description; or a
    /// `("list", model description)` for a ``list[Model]`` response annotation.
    pub fn from_spec(spec: &Bound<'_, PyAny>) -> PyResult<Arc<Self>> {
        let kind: String = spec.get_item(0)?.extract()?;
        if kind != "list" {
            return match node(spec, 0, false)? {
                Node::Model(plan) => Ok(plan),
                _ => Err(PyValueError::new_err("a model plan must describe a model")),
            };
        }
        let Node::Model(plan) = node(&spec.get_item(1)?, 0, true)? else {
            return Err(PyValueError::new_err("a list plan must describe a model"));
        };
        Ok(plan)
    }

    pub fn model_class<'py>(&self, py: Python<'py>) -> &Bound<'py, PyType> {
        self.cls.bind(py)
    }

    /// Whether the plan describes a ``list[Model]`` response annotation.
    pub fn is_list(&self) -> bool {
        self.is_list
    }
}

fn node(spec: &Bound<'_, PyAny>, depth: usize, is_list: bool) -> PyResult<Node> {
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
        "list" => Node::List(Box::new(node(&spec.get_item(1)?, depth + 1, false)?)),
        "nullable" => Node::Nullable(Box::new(node(&spec.get_item(1)?, depth + 1, false)?)),
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
                let inner = node(&field.get_item(3)?, depth + 1, false)?;
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
            Node::Model(Arc::new(ModelPlan {
                cls,
                forbid_extra,
                fields,
                all_fields,
                is_list: is_list && depth == 0,
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
    model(py, plan, value).ok().flatten()
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
        (Node::Model(plan), value) => return model(py, plan, value),
        _ => return Ok(None),
    }))
}

fn model<'py>(
    py: Python<'py>,
    plan: &ModelPlan,
    value: &Value,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let Value::Object(members) = value else {
        return Ok(None);
    };
    if plan.forbid_extra
        && members
            .keys()
            .any(|key| !plan.fields.iter().any(|field| field.key == *key))
    {
        return Ok(None);
    }
    let mut slots = Vec::with_capacity(plan.fields.len());
    for field in &plan.fields {
        match members.get(&field.key) {
            Some(value) => match build(py, &field.node, value)? {
                Some(built) => slots.push(Some(built)),
                None => return Ok(None),
            },
            None => slots.push(None),
        }
    }
    assemble(py, plan, slots)
}

/// Create the instance from one built value (or `None` for an absent key)
/// per field, in field order. An absent field without a default means `None`.
fn assemble<'py>(
    py: Python<'py>,
    plan: &ModelPlan,
    slots: Vec<Option<Bound<'py, PyAny>>>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let complete = slots.iter().all(Option::is_some);
    let fields_set = if complete {
        // SAFETY: `PySet_New` on a set copies its table without rehashing
        // and returns a new reference, or null with an exception set.
        unsafe { Bound::from_owned_ptr_or_err(py, ffi::PySet_New(plan.all_fields.as_ptr()))? }
            .cast_into::<PySet>()?
    } else {
        PySet::empty(py)?
    };
    let values = PyDict::new(py);
    for (field, slot) in plan.fields.iter().zip(slots) {
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
    let instance = instantiate(py, plan.cls.bind(py))?;
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
    let mut parser = Parser {
        input,
        position: 0,
        sink: Scan,
        scratch: Vec::new(),
    };
    parser.skip_whitespace();
    let Some(built) = parser.typed_model(py, plan, 0)? else {
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
            (Node::Model(plan), b'{') => return self.typed_model(py, plan, depth),
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

    fn typed_model<'py>(&mut self, py: Python<'py>, plan: &ModelPlan, depth: usize) -> Typed<'py> {
        if self.peek() != Some(b'{') {
            return Ok(None);
        }
        self.position += 1;
        let mut slots: Vec<Option<Bound<'py, PyAny>>> = Vec::with_capacity(plan.fields.len());
        slots.resize_with(plan.fields.len(), || None);
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
                let key = self.key()?;
                self.skip_whitespace();
                if self.peek() != Some(b':') {
                    return Err(ParseError);
                }
                self.position += 1;
                self.skip_whitespace();
                match plan.fields.iter().position(|field| field.key == *key) {
                    // A repeated key keeps the last value only after every
                    // value was validated; leave that to the general path.
                    Some(index) if slots[index].is_some() => return Ok(None),
                    Some(index) => match self.typed(py, &plan.fields[index].node, depth + 1)? {
                        Some(built) => slots[index] = Some(built),
                        None => return Ok(None),
                    },
                    None if plan.forbid_extra => return Ok(None),
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
        Ok(assemble(py, plan, slots).unwrap_or(None))
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
    super::with_encoder(
        value,
        max_bytes,
        // SAFETY: see `with_encoder`; models are read with the same rules as
        // containers.
        |encoder, object| unsafe { encoder.model(object, plan, 0) },
        |out| finish(out),
    )
}

/// [`encode_model`] returning the output as the `Vec` it was encoded into.
pub fn encode_model_vec(
    value: &Bound<'_, PyAny>,
    plan: &ModelPlan,
    max_bytes: usize,
) -> Option<Vec<u8>> {
    super::with_encoder(
        value,
        max_bytes,
        // SAFETY: as in `encode_model`.
        |encoder, object| unsafe { encoder.model(object, plan, 0) },
        super::take_output,
    )
}

/// Encode a Python list of plain model values, or return `None` when Pydantic
/// must validate and serialize it: each item is an exact dict, whose values
/// are validated against the plan's field types and written in one pass, or
/// an instance of exactly the plan's model, written from its `__dict__`.
/// Anything else (a subclass, a list subclass, a value needing a coercion, a
/// missing required key, an extra key under `extra="forbid"`, nesting over
/// the depth limit, output over `max_bytes`) refuses the whole list.
/// Such a result always matches the Pydantic pipeline: validating the same
/// list and dumping it produces the same bytes for everything that passes.
pub fn encode_model_list<R>(
    value: &Bound<'_, PyAny>,
    plan: &ModelPlan,
    max_bytes: usize,
    finish: impl FnOnce(&[u8]) -> R,
) -> Option<R> {
    super::with_encoder(
        value,
        max_bytes,
        // SAFETY: see `with_encoder`; items are read with the same rules as
        // containers.
        |encoder, object| unsafe { encoder.model_list(object, plan, 0) },
        |out| finish(out),
    )
}

/// [`encode_model_list`] returning the output as the `Vec` it was encoded into.
pub fn encode_model_list_vec(
    value: &Bound<'_, PyAny>,
    plan: &ModelPlan,
    max_bytes: usize,
) -> Option<Vec<u8>> {
    super::with_encoder(
        value,
        max_bytes,
        // SAFETY: as in `encode_model_list`.
        |encoder, object| unsafe { encoder.model_list(object, plan, 0) },
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
                Node::Model(plan) => self.model(object, plan, depth),
                _ => Err(Bail),
            }
        }
    }

    unsafe fn model(
        &mut self,
        object: *mut ffi::PyObject,
        plan: &ModelPlan,
        depth: usize,
    ) -> Result<(), Bail> {
        if depth > MAX_DEPTH {
            return Err(Bail);
        }
        unsafe {
            if ffi::Py_TYPE(object) != plan.cls.as_ptr().cast::<ffi::PyTypeObject>() {
                return Err(Bail);
            }
            let values = ffi::PyObject_GenericGetDict(object, std::ptr::null_mut());
            if values.is_null() {
                ffi::PyErr_Clear();
                return Err(Bail);
            }
            let result = self.model_fields(values, plan, depth);
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
        plan: &ModelPlan,
        depth: usize,
    ) -> Result<(), Bail> {
        unsafe {
            if ffi::PyDict_Size(values) as usize != plan.fields.len() {
                return Err(Bail);
            }
            let mut in_order = [std::ptr::null_mut(); ORDERED_FIELDS];
            if fields_in_order(values, plan, &mut in_order) {
                self.out.push(b'{');
                for (position, (field, &item)) in plan.fields.iter().zip(&in_order).enumerate() {
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
            for field in &plan.fields {
                let item = self.field_item(values, field.name.as_ptr())?;
                self.entries.push(item);
            }
            self.out.push(b'{');
            for (position, field) in plan.fields.iter().enumerate() {
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

    /// `"[{...}, ...]"`: a Python list whose items the plan validates and
    /// writes in one pass. An exact dict item is validated field by field;
    /// an exact instance of the model is written from its `__dict__`;
    /// anything else refuses the list (see [`encode_model_list`]).
    #[cfg(not(Py_GIL_DISABLED))]
    unsafe fn model_list(
        &mut self,
        object: *mut ffi::PyObject,
        plan: &ModelPlan,
        depth: usize,
    ) -> Result<(), Bail> {
        if depth > MAX_DEPTH {
            return Err(Bail);
        }
        unsafe {
            if ffi::Py_TYPE(object) != &raw mut ffi::PyList_Type {
                return Err(Bail);
            }
            let length = ffi::PyList_GET_SIZE(object);
            self.out.push(b'[');
            for index in 0..length {
                if index > 0 {
                    self.out.push(b',');
                }
                let item = ffi::PyList_GET_ITEM(object, index);
                if ffi::Py_TYPE(item) == plan.cls.as_ptr().cast::<ffi::PyTypeObject>() {
                    self.model(item, plan, depth + 1)?;
                } else if ffi::Py_TYPE(item) == &raw mut ffi::PyDict_Type {
                    self.model_dict(item, plan, depth + 1)?;
                } else {
                    return Err(Bail);
                }
            }
            self.out.push(b']');
            self.check()
        }
    }

    /// The same list, on a free-threaded build: items are held as strong
    /// references (copied under the list's critical section), so an instance
    /// item is written as on the other builds, while dict items keep the
    /// Pydantic path, since their entries are not held.
    #[cfg(Py_GIL_DISABLED)]
    unsafe fn model_list(
        &mut self,
        object: *mut ffi::PyObject,
        plan: &ModelPlan,
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
                let item = self.held[index];
                if ffi::Py_TYPE(item) != plan.cls.as_ptr().cast::<ffi::PyTypeObject>() {
                    return Err(Bail);
                }
                self.model(item, plan, depth + 1)?;
            }
            self.out.push(b']');
            self.check()
        }
    }

    /// Write an exact dict as one model value, validating its values against
    /// the plan: every key is an exact `str` (one walk), each declared field
    /// is then looked up by its interned key and written through its node,
    /// and absent ones are the field's pre-encoded default, or the model
    /// keeps Pydantic's path when it has none. Keys the plan does not declare
    /// are ignored like any extra key unless `extra="forbid"`.
    #[cfg(not(Py_GIL_DISABLED))]
    unsafe fn model_dict(
        &mut self,
        object: *mut ffi::PyObject,
        plan: &ModelPlan,
        depth: usize,
    ) -> Result<(), Bail> {
        unsafe {
            if depth > MAX_DEPTH {
                return Err(Bail);
            }
            let mut position: ffi::Py_ssize_t = 0;
            let mut key: *mut ffi::PyObject = std::ptr::null_mut();
            let mut item: *mut ffi::PyObject = std::ptr::null_mut();
            while ffi::PyDict_Next(object, &mut position, &mut key, &mut item) != 0 {
                if ffi::Py_TYPE(key) != &raw mut ffi::PyUnicode_Type {
                    return Err(Bail);
                }
                if plan.forbid_extra {
                    let text = self.utf8(key)?;
                    if !plan.fields.iter().any(|field| field.key.as_bytes() == text) {
                        return Err(Bail);
                    }
                }
            }
            self.out.push(b'{');
            // Fields in declaration order, so the bytes match
            // model_dump_json's key order.
            for (position, field) in plan.fields.iter().enumerate() {
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

    /// Write a dict-sourced value against a node: nested models accept exact
    /// dicts (recursed through [`Self::model_dict`]) as well as instances of
    /// the model; optionals peel their wrapper; anything else is written like
    /// [`Self::node`] (so a value without its field's exact type refuses the
    /// whole list).
    #[cfg(not(Py_GIL_DISABLED))]
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
                Node::Nullable(inner) if object == ffi::Py_None() => {
                    self.out.extend_from_slice(b"null");
                    self.check()
                }
                Node::Nullable(inner) => self.dict_value(object, inner, depth),
                Node::List(inner) if ffi::Py_TYPE(object) == &raw mut ffi::PyList_Type => {
                    let length = ffi::PyList_GET_SIZE(object);
                    self.out.push(b'[');
                    for index in 0..length {
                        if index > 0 {
                            self.out.push(b',');
                        }
                        self.dict_value(ffi::PyList_GET_ITEM(object, index), inner, depth + 1)?;
                    }
                    self.out.push(b']');
                    self.check()
                }
                Node::Model(plan) => {
                    if ffi::Py_TYPE(object) == plan.cls.as_ptr().cast::<ffi::PyTypeObject>() {
                        self.model(object, plan, depth)
                    } else if ffi::Py_TYPE(object) == &raw mut ffi::PyDict_Type {
                        self.model_dict(object, plan, depth)
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
    plan: &ModelPlan,
    items: &mut [*mut ffi::PyObject; ORDERED_FIELDS],
) -> bool {
    #[cfg(Py_GIL_DISABLED)]
    return false;
    #[cfg(not(Py_GIL_DISABLED))]
    unsafe {
        if plan.fields.len() > ORDERED_FIELDS {
            return false;
        }
        let mut position: ffi::Py_ssize_t = 0;
        let mut key: *mut ffi::PyObject = std::ptr::null_mut();
        let mut item: *mut ffi::PyObject = std::ptr::null_mut();
        let mut index = 0;
        while ffi::PyDict_Next(values, &mut position, &mut key, &mut item) != 0 {
            // Identity with the plan's (exact `str`) name, so no comparison
            // or Python `__eq__` is involved.
            match plan.fields.get(index) {
                Some(field) if field.name.as_ptr() == key => items[index] = item,
                _ => return false,
            }
            index += 1;
        }
        index == plan.fields.len()
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
}
