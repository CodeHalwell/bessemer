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
//! produces, with object keys in byte order. A value that does not have its
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

impl Node {
    fn contains_model(&self) -> bool {
        match self {
            Self::List(inner) | Self::Nullable(inner) => inner.contains_model(),
            Self::Model(_) => true,
            _ => false,
        }
    }
}

struct Field {
    /// Interned attribute name.
    name: Py<PyString>,
    /// The key read from request bodies (the validation alias, if any).
    key: String,
    /// An immutable default, used when the key is absent.
    default: Option<Py<PyAny>>,
    node: Node,
}

/// A compiled description of one plain Pydantic model.
pub struct ModelPlan {
    cls: Py<PyType>,
    forbid_extra: bool,
    fields: Vec<Field>,
    /// Pre-escaped `"key":` prefixes with their field index, sorted by the
    /// original key's UTF-8 bytes (escaping can change their byte order).
    output: Vec<(Vec<u8>, usize)>,
}

impl ModelPlan {
    /// Compile a plan from its Python description:
    /// `("model", cls, forbid_extra, ((name, key, output_key, node, has_default, default), ...))`,
    /// where a node is `("int",)`, `("float",)`, `("str",)`, `("bool",)`,
    /// `("list", node)`, `("nullable", node)` or a model description.
    pub fn from_spec(spec: &Bound<'_, PyAny>) -> PyResult<Arc<Self>> {
        match node(spec, 0)? {
            Node::Model(plan) => Ok(plan),
            _ => Err(PyValueError::new_err("a model plan must describe a model")),
        }
    }

    pub fn model_class<'py>(&self, py: Python<'py>) -> &Bound<'py, PyType> {
        self.cls.bind(py)
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
        "model" => {
            let py = spec.py();
            let cls = spec.get_item(1)?.cast_into::<PyType>()?.unbind();
            let forbid_extra: bool = spec.get_item(2)?.extract()?;
            let mut fields = Vec::new();
            let mut output = Vec::new();
            for (index, field) in spec.get_item(3)?.cast::<PyTuple>()?.iter().enumerate() {
                let field = field.cast::<PyTuple>()?;
                let name: String = field.get_item(0)?.extract()?;
                let key: String = field.get_item(1)?.extract()?;
                let output_key: String = field.get_item(2)?.extract()?;
                let has_default: bool = field.get_item(4)?.extract()?;
                fields.push(Field {
                    name: PyString::intern(py, &name).unbind(),
                    key,
                    default: has_default
                        .then(|| field.get_item(5))
                        .transpose()?
                        .map(Bound::unbind),
                    node: node(&field.get_item(3)?, depth + 1)?,
                });
                output.push((output_key, index));
            }
            output.sort_unstable_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
            if output.windows(2).any(|pair| pair[0].0 == pair[1].0) {
                return Err(PyValueError::new_err(
                    "model plan has duplicate output keys",
                ));
            }
            let output = output
                .into_iter()
                .map(|(key, index)| {
                    let mut prefix = Vec::with_capacity(key.len() + 3);
                    write_escaped(&mut prefix, key.as_bytes(), usize::MAX)
                        .map_err(|_| PyValueError::new_err("model plan key is too large"))?;
                    prefix.push(b':');
                    Ok((prefix, index))
                })
                .collect::<PyResult<_>>()?;
            Node::Model(Arc::new(ModelPlan {
                cls,
                forbid_extra,
                fields,
                output,
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
    let values = PyDict::new(py);
    let fields_set = PySet::empty(py)?;
    for (field, slot) in plan.fields.iter().zip(slots) {
        let name = field.name.bind(py);
        match slot {
            Some(built) => {
                values.set_item(name, built)?;
                fields_set.add(name)?;
            }
            None => match &field.default {
                Some(default) => values.set_item(name, default.bind(py))?,
                None => return Ok(None),
            },
        }
    }
    let instance = instantiate(py, plan.cls.bind(py))?;
    // The same attributes, in the same order, that pydantic-core's model
    // validator sets on an instance it builds.
    set_attribute(&instance, pyo3::intern!(py, "__dict__"), values.as_any())?;
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
    let Some(parsed) = parser.typed_model(py, plan, 0)? else {
        return Ok(None);
    };
    parser.skip_whitespace();
    if parser.position != input.len() {
        return Err(ParseError);
    }
    Ok(parsed.materialize(py).unwrap_or(None))
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

enum Parsed<'py, 'plan> {
    Value(Bound<'py, PyAny>),
    List(Vec<Self>),
    Model(&'plan ModelPlan, Vec<Option<Self>>),
}

impl<'py> Parsed<'py, '_> {
    fn materialize(self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyAny>>> {
        match self {
            Self::Value(value) => Ok(Some(value)),
            Self::List(items) => {
                let list = PyList::empty(py);
                for item in items {
                    let Some(value) = item.materialize(py)? else {
                        return Ok(None);
                    };
                    list.append(value)?;
                }
                Ok(Some(list.into_any()))
            }
            Self::Model(plan, slots) => {
                let mut built = Vec::with_capacity(slots.len());
                for slot in slots {
                    built.push(match slot {
                        Some(item) => match item.materialize(py)? {
                            Some(value) => Some(value),
                            None => return Ok(None),
                        },
                        None => None,
                    });
                }
                assemble(py, plan, built)
            }
        }
    }
}

/// `Ok(None)` means the value does not have the expected exact type; the
/// position is then meaningless and the caller gives up.
type Typed<'py, 'plan> = Result<Option<Parsed<'py, 'plan>>, ParseError>;

impl Parser<'_, Scan> {
    fn typed<'py, 'plan>(
        &mut self,
        py: Python<'py>,
        node: &'plan Node,
        depth: usize,
    ) -> Typed<'py, 'plan> {
        // The same limit as `value`, so a body the typed parse accepts is
        // one the scanner accepts.
        if depth > MAX_DEPTH {
            return Err(ParseError);
        }
        self.skip_whitespace();
        let next = self.peek().ok_or(ParseError)?;
        Ok(Some(Parsed::Value(match (node, next) {
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
                PyString::new(py, unsafe { std::str::from_utf8_unchecked(text) }).into_any()
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
        })))
    }

    fn typed_list<'py, 'plan>(
        &mut self,
        py: Python<'py>,
        inner: &'plan Node,
        depth: usize,
    ) -> Typed<'py, 'plan> {
        self.position += 1;
        let immediate = (!inner.contains_model()).then(|| PyList::empty(py));
        let mut pending = Vec::new();
        self.skip_whitespace();
        if self.peek() == Some(b']') {
            self.position += 1;
            return Ok(Some(match immediate {
                Some(list) => Parsed::Value(list.into_any()),
                None => Parsed::List(pending),
            }));
        }
        loop {
            match self.typed(py, inner, depth + 1)? {
                Some(Parsed::Value(value)) if immediate.is_some() => {
                    if immediate.as_ref().unwrap().append(value).is_err() {
                        return Ok(None);
                    }
                }
                Some(item) if immediate.is_none() => pending.push(item),
                _ => return Ok(None),
            }
            self.skip_whitespace();
            match self.peek() {
                Some(b',') => self.position += 1,
                Some(b']') => {
                    self.position += 1;
                    return Ok(Some(match immediate {
                        Some(list) => Parsed::Value(list.into_any()),
                        None => Parsed::List(pending),
                    }));
                }
                _ => return Err(ParseError),
            }
        }
    }

    fn typed_model<'py, 'plan>(
        &mut self,
        py: Python<'py>,
        plan: &'plan ModelPlan,
        depth: usize,
    ) -> Typed<'py, 'plan> {
        if self.peek() != Some(b'{') {
            return Ok(None);
        }
        self.position += 1;
        let mut slots: Vec<Option<Parsed<'py, 'plan>>> = Vec::with_capacity(plan.fields.len());
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
        Ok(Some(Parsed::Model(plan, slots)))
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
            if ffi::PyDict_Size(values) as usize != plan.fields.len() || !self.exact_keys(values) {
                return Err(Bail);
            }
            // Item references for this model share the entries stack, so
            // nested models never allocate: this model's are base..base+n.
            let base = self.entries.len();
            for (prefix, index) in &plan.output {
                let item = self.field_item(values, plan.fields[*index].name.as_ptr())?;
                self.entries.push((prefix.as_ptr(), prefix.len(), item));
            }
            self.out.push(b'{');
            for position in 0..plan.output.len() {
                if position > 0 {
                    self.out.push(b',');
                }
                let (prefix, length, item) = self.entries[base + position];
                // Check before copying: a fixed escaped alias can be much
                // larger than the remaining response budget.
                if length > self.max_bytes.saturating_sub(self.out.len()) {
                    return Err(Bail);
                }
                self.out
                    .extend_from_slice(std::slice::from_raw_parts(prefix, length));
                let field = &plan.fields[plan.output[position].1];
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
    fn model_keys_are_escaped_once_and_sorted_before_escaping() {
        Python::attach(|py| {
            let locals = PyDict::new(py);
            py.run(
                cr#"class Record: pass
aliases = [('empty', ''), ('nul', '\x00'), ('newline', '\n'),
           ('quote', '\"'), ('slash', '\\'), ('ascii', 'a'),
           ('unicode', '\u00e9\U0001f642')]
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
            let expected = serde_json::to_vec(&serde_json::json!({
                "": 0, "\0": 1, "\n": 2, "\"": 3, "\\": 4, "a": 5, "é🙂": 6,
            }))
            .unwrap();
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
            super::super::SCRATCH.with(|cell| *cell.borrow_mut() = Default::default());
            assert!(encode_model(&value, &plan, 64, |_| panic!("over budget")).is_none());
            super::super::SCRATCH.with(|cell| {
                let scratch = cell.borrow();
                assert!(scratch.out.len() <= 64);
                assert!(scratch.out.capacity() <= 64);
            });
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
