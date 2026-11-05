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
    types::{PyDict, PyList, PySet, PyString, PyTuple, PyType},
};
use serde_json::Value;

use super::{Bail, Encoder, MAX_DEPTH, write_escaped};

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
    /// An immutable default, used when the key is absent.
    default: Option<Py<PyAny>>,
    node: Node,
}

/// A compiled description of one plain Pydantic model.
pub struct ModelPlan {
    cls: Py<PyType>,
    forbid_extra: bool,
    fields: Vec<Field>,
    /// Serialized keys with their field index, in byte order.
    output: Vec<(String, usize)>,
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
    let values = PyDict::new(py);
    let fields_set = PySet::empty(py)?;
    for field in &plan.fields {
        let name = field.name.bind(py);
        match members.get(&field.key) {
            Some(value) => {
                let Some(built) = build(py, &field.node, value)? else {
                    return Ok(None);
                };
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
        finish,
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
            if ffi::PyDict_Size(values) as usize != plan.fields.len() {
                return Err(Bail);
            }
            // Item references for this model share the entries stack, so
            // nested models never allocate: this model's are base..base+n.
            let base = self.entries.len();
            for (key, index) in &plan.output {
                let item = self.field_item(values, plan.fields[*index].name.as_ptr())?;
                self.entries.push((key.as_ptr(), key.len(), item));
            }
            self.out.push(b'{');
            for position in 0..plan.output.len() {
                if position > 0 {
                    self.out.push(b',');
                }
                let (key, length, item) = self.entries[base + position];
                write_escaped(
                    self.out,
                    std::slice::from_raw_parts(key, length),
                    self.max_bytes,
                )?;
                self.out.push(b':');
                self.check()?;
                let field = &plan.fields[plan.output[position].1];
                self.node(item, &field.node, depth + 1)?;
            }
            self.entries.truncate(base);
            self.out.push(b'}');
            self.check()
        }
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
            let start = self.held.len();
            let room = self.max_bytes.saturating_sub(self.out.len());
            let fits = self.locked(object, |this| {
                let length = ffi::PyList_GET_SIZE(object) as usize;
                if length.saturating_mul(2).saturating_add(1) > room {
                    return false;
                }
                this.held.reserve(length);
                for index in 0..length {
                    this.hold(ffi::PyList_GET_ITEM(object, index as ffi::Py_ssize_t));
                }
                true
            });
            if !fits {
                return Err(Bail);
            }
            let end = self.held.len();
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
