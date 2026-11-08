//! Purpose-built JSON codec for the request/response hot path.
//!
//! The previous path went through `serde_json::Value` in both directions:
//! Python objects were copied into a `Value` tree and then serialized, and
//! request bodies were parsed into `RawValue`s, re-parsed once per nesting
//! level, and copied again into Python objects. This module removes those
//! intermediate trees:
//!
//! * [`encode_plain`] writes exact built-in Python JSON values straight into a
//!   byte buffer, reading CPython objects through the C API. Containers are
//!   snapshotted into strong references under their critical section, so the
//!   encoder stays sound on free-threaded Python, where another thread can
//!   mutate a list or dict while it is being encoded.
//! * [`parse_python`] parses bytes straight into Python objects in one pass.
//! * [`parse_value`] parses bytes into a `Value` in one pass without the GIL,
//!   for the request-body path that binds arguments before entering Python.
//!
//! Behaviour matches the code it replaces, while keeping Python's key order:
//! object keys are emitted in dict insertion order, duplicate keys in parsed
//! objects keep the last value after every value has been validated, nesting is
//! limited to 64 levels, integers keep full precision up to 4096 digits, floats
//! must be finite and numbers are formatted exactly as `serde_json` formatted
//! them.
//!
//! This crate is the audited unsafe boundary for direct CPython object access;
//! the server crate itself forbids unsafe code.
#![deny(unsafe_op_in_unsafe_fn)]

use std::borrow::Cow;
use std::cell::RefCell;

#[cfg(Py_GIL_DISABLED)]
use pyo3::sync::critical_section::with_critical_section;
use pyo3::{ffi, prelude::*};
use serde_json::{Map, Value};

mod model;
pub mod runtime;
pub use model::{
    ModelPlan, build_model, encode_model, encode_model_list, encode_model_list_vec,
    encode_model_vec, parse_model, validate_json,
};

/// Largest accepted integer, in decimal digits, in either direction.
pub const MAX_INTEGER_DIGITS: usize = 4096;
/// Deepest accepted nesting level.
pub const MAX_DEPTH: usize = 64;

// ---------------------------------------------------------------------------
// Encoding
// ---------------------------------------------------------------------------

struct Scratch {
    out: Vec<u8>,
    entries: Vec<Entry>,
    /// Recent float encodings, direct-mapped by the float's bits: a repeated
    /// value (the same handler's data across requests) skips re-formatting
    /// with zmij. The bytes were produced by zmij itself. This cache is
    /// bounded: a fixed table of [`FLOAT_SLOTS`] slots created here when a
    /// thread's scratch space is built and never grown (see [`FloatSlot`]).
    /// It lives in this per-thread scratch space, so on free-threaded Python
    /// every interpreter thread has its own table and no state is shared:
    /// the cached bytes depend only on the float's bits, so any table is
    /// correct for its thread.
    floats: Vec<FloatSlot>,
    /// Strong references taken from containers on free-threaded Python,
    /// released after encoding.
    #[cfg(Py_GIL_DISABLED)]
    held: Vec<*mut ffi::PyObject>,
}

/// One cached encoding: `zmij`'s bytes of a finite double, tagged with its
/// bit pattern. Fixed shape (a 32-byte buffer, whatever the value), so the
/// whole table of [`FLOAT_SLOTS`] slots is bounded at about 6 KiB.
#[derive(Clone, Copy)]
struct FloatSlot {
    bits: u64,
    /// Up to `zmij`'s longest finite output (about 24 bytes) plus slack.
    bytes: [u8; 32],
    length: usize,
}

/// Direct-mapped float cache size: the biggest repeated-value responses seen
/// here repeat a few dozen values across requests; a miss re-formats. The
/// table is built at this size once per thread and never grows.
const FLOAT_SLOTS: usize = 128;

impl FloatSlot {
    const EMPTY: FloatSlot = FloatSlot {
        bits: 0,
        bytes: [0; 32],
        length: 0,
    };
}

/// The `float` slot's format: an exact finite double's `zmij` output never
/// exceeds 24 bytes (`-1.7976931348623157e308`), so 32 always fits.
const _: () = assert!(32 >= 1 + 1 + 24);

thread_local! {
    // Reused buffers. Responses are copied into a bytes object, so neither
    // buffer escapes; they only avoid fresh allocations per response.
    static SCRATCH: RefCell<Scratch> = RefCell::new(Scratch::default());
}

impl Default for Scratch {
    fn default() -> Self {
        Scratch {
            out: Vec::new(),
            entries: Vec::new(),
            floats: vec![FloatSlot::EMPTY; FLOAT_SLOTS],
            #[cfg(Py_GIL_DISABLED)]
            held: Vec::new(),
        }
    }
}

/// Retained buffer capacity. Larger buffers are released after use so one big
/// response does not pin memory on the thread indefinitely.
const RETAINED_CAPACITY: usize = 64 * 1024;
/// Retained key-stack capacity, in entries, on the same principle.
const RETAINED_ENTRIES: usize = 4096;

struct Bail;

/// Encode a value made only of exact built-in JSON types, or return `None`.
///
/// `None` means the value needs the general normalizing path: a subclass, a
/// tuple, a non-string key, a non-finite float, an integer outside 64 bits, a
/// string that is not valid UTF-8, nesting over 64 levels, or output larger
/// than `max_bytes`. The caller keeps one conversion and error behaviour for
/// all of those.
pub fn encode_plain<R>(
    value: &Bound<'_, PyAny>,
    max_bytes: usize,
    finish: impl FnOnce(&[u8]) -> R,
) -> Option<R> {
    // SAFETY: see `with_encoder`.
    with_encoder(
        value,
        max_bytes,
        |encoder, object| unsafe { encoder.value(object, 0) },
        |out| finish(out),
    )
}

/// [`encode_plain`] returning the output as the `Vec` it was encoded into,
/// without copying it out of the scratch buffer.
pub fn encode_plain_vec(value: &Bound<'_, PyAny>, max_bytes: usize) -> Option<Vec<u8>> {
    // SAFETY: see `with_encoder`.
    with_encoder(
        value,
        max_bytes,
        |encoder, object| unsafe { encoder.value(object, 0) },
        take_output,
    )
}

/// Hand the encoded buffer to the caller and leave the scratch space a new one
/// sized for the next response of this length. When the buffer is much larger
/// than its contents (it was grown for an earlier, bigger response) the bytes
/// are copied out instead, so a small body never pins a large allocation.
pub(crate) fn take_output(out: &mut Vec<u8>) -> Vec<u8> {
    let len = out.len();
    if out.capacity() > len * 2 + 64 {
        return out.clone();
    }
    std::mem::replace(out, Vec::with_capacity(len + len / 8 + 16))
}

/// Run `encode` on `value` with the shared scratch buffers, returning
/// `finish(output)` when it succeeds within `max_bytes`.
fn with_encoder<R>(
    value: &Bound<'_, PyAny>,
    max_bytes: usize,
    encode: impl FnOnce(&mut Encoder<'_, '_>, *mut ffi::PyObject) -> Result<(), Bail>,
    finish: impl FnOnce(&mut Vec<u8>) -> R,
) -> Option<R> {
    SCRATCH.with(|cell| {
        // A re-entrant call (impossible today, since no Python code runs while
        // encoding) would fall back to private buffers instead of panicking.
        let mut borrowed = cell.try_borrow_mut();
        let mut private = Scratch::default();
        let scratch: &mut Scratch = match borrowed.as_deref_mut() {
            Ok(scratch) => scratch,
            Err(_) => &mut private,
        };
        scratch.out.clear();
        scratch.entries.clear();
        #[cfg(Py_GIL_DISABLED)]
        scratch.held.clear();
        let mut encoder = Encoder {
            #[cfg(Py_GIL_DISABLED)]
            py: value.py(),
            out: &mut scratch.out,
            entries: &mut scratch.entries,
            floats: &mut scratch.floats,
            #[cfg(Py_GIL_DISABLED)]
            held: &mut scratch.held,
            #[cfg(not(Py_GIL_DISABLED))]
            _py: std::marker::PhantomData,
            max_bytes,
        };
        // The caller owns `value` and the encoder runs no Python code. With
        // the GIL held, nothing it visits through borrowed references can be
        // mutated or freed while it runs. On free-threaded Python every object
        // below `value` is a strong reference in `held`, copied out of its
        // container under that container's critical section, so nothing it
        // visits can be freed even if another thread mutates a container.
        let encoded = encode(&mut encoder, value.as_ptr());
        let result = match encoded {
            Ok(()) if encoder.out.len() <= max_bytes => Some(finish(&mut *encoder.out)),
            _ => None,
        };
        scratch.entries.clear();
        #[cfg(Py_GIL_DISABLED)]
        {
            // Releasing a reference can run a finalizer, which could encode
            // again, so the references leave the scratch space first.
            let mut held = std::mem::take(&mut scratch.held);
            for object in held.drain(..) {
                // SAFETY: each pointer is a strong reference taken by `hold`.
                unsafe { ffi::Py_DECREF(object) };
            }
            if held.capacity() <= RETAINED_ENTRIES {
                scratch.held = held;
            }
        }
        if scratch.out.capacity() > RETAINED_CAPACITY {
            scratch.out = Vec::new();
        }
        if scratch.entries.capacity() > RETAINED_ENTRIES {
            scratch.entries = Vec::new();
        }
        result
    })
}

type Entry = *mut ffi::PyObject;

struct Encoder<'a, 'py> {
    #[cfg(Py_GIL_DISABLED)]
    py: Python<'py>,
    out: &'a mut Vec<u8>,
    entries: &'a mut Vec<Entry>,
    floats: &'a mut [FloatSlot],
    #[cfg(Py_GIL_DISABLED)]
    held: &'a mut Vec<*mut ffi::PyObject>,
    #[cfg(not(Py_GIL_DISABLED))]
    _py: std::marker::PhantomData<Python<'py>>,
    max_bytes: usize,
}

impl Encoder<'_, '_> {
    #[inline]
    fn check(&self) -> Result<(), Bail> {
        if self.out.len() > self.max_bytes {
            Err(Bail)
        } else {
            Ok(())
        }
    }

    unsafe fn value(&mut self, object: *mut ffi::PyObject, depth: usize) -> Result<(), Bail> {
        if depth > MAX_DEPTH {
            return Err(Bail);
        }
        unsafe {
            let kind = ffi::Py_TYPE(object);
            if kind == &raw mut ffi::PyUnicode_Type {
                self.string(object)
            } else if kind == &raw mut ffi::PyLong_Type {
                self.integer(object)
            } else if object == ffi::Py_None() {
                self.out.extend_from_slice(b"null");
                self.check()
            } else if object == ffi::Py_True() {
                self.out.extend_from_slice(b"true");
                self.check()
            } else if object == ffi::Py_False() {
                self.out.extend_from_slice(b"false");
                self.check()
            } else if kind == &raw mut ffi::PyFloat_Type {
                let number = ffi::PyFloat_AS_DOUBLE(object);
                if !number.is_finite() {
                    return Err(Bail);
                }
                self.float(number)
            } else if kind == &raw mut ffi::PyDict_Type {
                self.dict(object, depth)
            } else if kind == &raw mut ffi::PyList_Type {
                self.list(object, depth)
            } else {
                Err(Bail)
            }
        }
    }

    unsafe fn integer(&mut self, object: *mut ffi::PyObject) -> Result<(), Bail> {
        unsafe {
            let mut overflow = 0;
            let signed = ffi::PyLong_AsLongLongAndOverflow(object, &mut overflow);
            let mut buffer = itoa::Buffer::new();
            if overflow == 0 {
                if signed == -1 && !ffi::PyErr_Occurred().is_null() {
                    ffi::PyErr_Clear();
                    return Err(Bail);
                }
                self.out.extend_from_slice(buffer.format(signed).as_bytes());
            } else if overflow > 0 {
                let unsigned = ffi::PyLong_AsUnsignedLongLong(object);
                if unsigned == u64::MAX && !ffi::PyErr_Occurred().is_null() {
                    ffi::PyErr_Clear();
                    return Err(Bail);
                }
                self.out
                    .extend_from_slice(buffer.format(unsigned).as_bytes());
            } else {
                return Err(Bail);
            }
            self.check()
        }
    }

    /// Write a finite double. Repeats of the same bit pattern (a repeated
    /// handler's data across requests) reuse the zmij-formatted bytes cached
    /// by an earlier encode of the same value: same bytes, no re-formatting.
    #[inline]
    fn float(&mut self, number: f64) -> Result<(), Bail> {
        let bits = number.to_bits();
        // Direct-mapped by the high bits of the golden-ratio multiple.
        let index = (bits.wrapping_mul(0x9E37_79B9_7F4A_7C15) >> (u64::BITS - 7)) as usize;
        let slot = &mut self.floats[index];
        if slot.length != 0 && slot.bits == bits {
            self.out.extend_from_slice(&slot.bytes[..slot.length]);
        } else {
            // serde_json formats floats with zmij; use the same formatter.
            let mut buffer = zmij::Buffer::new();
            let text = buffer.format_finite(number).as_bytes();
            self.out.extend_from_slice(text);
            slot.bytes[..text.len()].copy_from_slice(text);
            slot.length = text.len();
            slot.bits = bits;
        }
        self.check()
    }

    unsafe fn utf8<'s>(&self, object: *mut ffi::PyObject) -> Result<&'s [u8], Bail> {
        unsafe {
            // Every character encodes to at least one byte, so a string that
            // cannot fit is rejected before any UTF-8 conversion or copy.
            let characters = ffi::PyUnicode_GET_LENGTH(object) as usize;
            if characters.saturating_add(2) > self.max_bytes.saturating_sub(self.out.len()) {
                return Err(Bail);
            }
            let mut size: ffi::Py_ssize_t = 0;
            let data = ffi::PyUnicode_AsUTF8AndSize(object, &mut size);
            if data.is_null() {
                // Lone surrogates cannot be encoded as UTF-8.
                ffi::PyErr_Clear();
                return Err(Bail);
            }
            // Non-ASCII text needs up to four bytes per character; charge the
            // real UTF-8 size before anything is copied.
            if (size as usize).saturating_add(2) > self.max_bytes.saturating_sub(self.out.len()) {
                return Err(Bail);
            }
            Ok(std::slice::from_raw_parts(data.cast::<u8>(), size as usize))
        }
    }

    unsafe fn string(&mut self, object: *mut ffi::PyObject) -> Result<(), Bail> {
        let bytes = unsafe { self.utf8(object)? };
        write_escaped(self.out, bytes, self.max_bytes)?;
        self.check()
    }

    #[cfg(Py_GIL_DISABLED)]
    /// Run `copy` under `object`'s critical section. On free-threaded Python
    /// this holds the container's lock, so it is neither resized nor has its
    /// items replaced while they are copied; with the GIL it is a plain call.
    unsafe fn locked<R>(
        &mut self,
        object: *mut ffi::PyObject,
        copy: impl FnOnce(&mut Self) -> R,
    ) -> R {
        // SAFETY: `object` is alive: it is the caller's value or held here.
        let container = unsafe { Bound::ref_from_ptr(self.py, &object) };
        with_critical_section(container, || copy(self))
    }

    #[cfg(Py_GIL_DISABLED)]
    #[inline]
    unsafe fn hold(&mut self, object: *mut ffi::PyObject) {
        // SAFETY: `object` was just read from a container whose critical
        // section is held, so it is alive; the new reference keeps it so.
        unsafe { ffi::Py_INCREF(object) };
        self.held.push(object);
    }

    #[cfg(Py_GIL_DISABLED)]
    /// Hold strong references to every item of a list, copied under its
    /// critical section; they are `self.held[start..end]`. Refuses, before
    /// copying, a list whose shortest encoding (`[` + n digits + (n - 1)
    /// commas + `]`) could not fit in what is left of the output.
    unsafe fn hold_list(&mut self, object: *mut ffi::PyObject) -> Result<(usize, usize), Bail> {
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
            if fits {
                Ok((start, self.held.len()))
            } else {
                Err(Bail)
            }
        }
    }

    // With the GIL held nothing can mutate or free a container while it is
    // read, so the GIL build reads items through borrowed references.
    #[cfg(not(Py_GIL_DISABLED))]
    unsafe fn list(&mut self, object: *mut ffi::PyObject, depth: usize) -> Result<(), Bail> {
        unsafe {
            let length = ffi::PyList_GET_SIZE(object);
            self.out.push(b'[');
            for index in 0..length {
                if index > 0 {
                    self.out.push(b',');
                }
                self.value(ffi::PyList_GET_ITEM(object, index), depth + 1)?;
            }
            self.out.push(b']');
            self.check()
        }
    }

    #[cfg(not(Py_GIL_DISABLED))]
    unsafe fn dict(&mut self, object: *mut ffi::PyObject, depth: usize) -> Result<(), Bail> {
        unsafe {
            let length = ffi::PyDict_Size(object) as usize;
            if length.saturating_mul(5).saturating_add(1)
                > self.max_bytes.saturating_sub(self.out.len())
            {
                // n entries need at least `{` + n * `"":0` + (n - 1) commas + `}`.
                return Err(Bail);
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
                self.value(item, depth + 1)?;
            }
            self.out.push(b'}');
            self.check()
        }
    }

    #[cfg(Py_GIL_DISABLED)]
    unsafe fn list(&mut self, object: *mut ffi::PyObject, depth: usize) -> Result<(), Bail> {
        unsafe {
            let (start, end) = self.hold_list(object)?;
            self.out.push(b'[');
            for index in start..end {
                if index > start {
                    self.out.push(b',');
                }
                self.value(self.held[index], depth + 1)?;
            }
            self.out.push(b']');
            self.check()
        }
    }

    #[cfg(Py_GIL_DISABLED)]
    unsafe fn dict(&mut self, object: *mut ffi::PyObject, depth: usize) -> Result<(), Bail> {
        unsafe {
            // Keys and items are copied under the dict's critical section;
            // UTF-8 views of the keys are taken afterwards, outside it.
            let start = self.held.len();
            let room = self.max_bytes.saturating_sub(self.out.len());
            let fits = self.locked(object, |this| {
                let length = ffi::PyDict_Size(object) as usize;
                // n entries need at least `{` + n * `"":0` + (n - 1) commas + `}`;
                // refuse before copying anything that could not fit.
                if length.saturating_mul(5).saturating_add(1) > room {
                    return false;
                }
                this.held.reserve(2 * length);
                let mut position: ffi::Py_ssize_t = 0;
                let mut key: *mut ffi::PyObject = std::ptr::null_mut();
                let mut item: *mut ffi::PyObject = std::ptr::null_mut();
                while ffi::PyDict_Next(object, &mut position, &mut key, &mut item) != 0 {
                    this.hold(key);
                    this.hold(item);
                }
                true
            });
            if !fits {
                return Err(Bail);
            }
            let length = (self.held.len() - start) / 2;
            self.out.push(b'{');
            for pair in 0..length {
                if pair > 0 {
                    self.out.push(b',');
                }
                let key = self.held[start + 2 * pair];
                let item = self.held[start + 2 * pair + 1];
                if ffi::Py_TYPE(key) != &raw mut ffi::PyUnicode_Type {
                    return Err(Bail);
                }
                let text = self.utf8(key)?;
                write_escaped(self.out, text, self.max_bytes)?;
                self.out.push(b':');
                self.check()?;
                self.value(item, depth + 1)?;
            }
            self.out.push(b'}');
            self.check()
        }
    }
}

// Escape classes, matching serde_json's output byte for byte.
const UU: u8 = b'u'; // \u00XX
const BB: u8 = b'b';
const TT: u8 = b't';
const NN: u8 = b'n';
const FF: u8 = b'f';
const RR: u8 = b'r';
const QU: u8 = b'"';
const BS: u8 = b'\\';
const __: u8 = 0;

static ESCAPE: [u8; 256] = [
    //   1   2   3   4   5   6   7   8   9   A   B   C   D   E   F
    UU, UU, UU, UU, UU, UU, UU, UU, BB, TT, NN, UU, FF, RR, UU, UU, // 0
    UU, UU, UU, UU, UU, UU, UU, UU, UU, UU, UU, UU, UU, UU, UU, UU, // 1
    __, __, QU, __, __, __, __, __, __, __, __, __, __, __, __, __, // 2
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // 3
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // 4
    __, __, __, __, __, __, __, __, __, __, __, __, BS, __, __, __, // 5
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // 6
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // 7
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // 8
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // 9
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // A
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // B
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // C
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // D
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // E
    __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, __, // F
];

/// Index of the first byte at or after `from` that needs escaping, scanning
/// eight bytes at a time. Most API strings need no escaping at all.
#[inline]
fn first_escape(bytes: &[u8], from: usize) -> usize {
    const ONES: u64 = 0x0101_0101_0101_0101;
    const HIGHS: u64 = 0x8080_8080_8080_8080;
    let mut index = from;
    while index + 8 <= bytes.len() {
        let word = u64::from_le_bytes(bytes[index..index + 8].try_into().unwrap());
        // High bit set in a lane when that byte is < 0x20, '"' or '\\'.
        let control = word.wrapping_sub(ONES * 0x20) & !word;
        let quote = (word ^ (ONES * u64::from(b'"'))).wrapping_sub(ONES)
            & !(word ^ (ONES * u64::from(b'"')));
        let slash = (word ^ (ONES * u64::from(b'\\'))).wrapping_sub(ONES)
            & !(word ^ (ONES * u64::from(b'\\')));
        let found = (control | quote | slash) & HIGHS;
        if found != 0 {
            return index + (found.trailing_zeros() / 8) as usize;
        }
        index += 8;
    }
    while index < bytes.len() && ESCAPE[bytes[index] as usize] == 0 {
        index += 1;
    }
    index
}

/// Write `bytes` as a JSON string, stopping once `out` exceeds `limit`.
///
/// Callers have already checked that the unescaped bytes fit, so only escapes
/// can grow the output; checking after each one keeps the overshoot to a single
/// escape (six bytes) instead of the full escaped size.
fn write_escaped(out: &mut Vec<u8>, bytes: &[u8], limit: usize) -> Result<(), Bail> {
    out.reserve(bytes.len() + 2);
    out.push(b'"');
    let first = first_escape(bytes, 0);
    if first == bytes.len() {
        out.extend_from_slice(bytes);
        out.push(b'"');
        return Ok(());
    }
    let mut start = 0;
    for (index, &byte) in bytes.iter().enumerate().skip(first) {
        let escape = ESCAPE[byte as usize];
        if escape == 0 {
            continue;
        }
        out.extend_from_slice(&bytes[start..index]);
        match escape {
            UU => {
                const HEX: &[u8; 16] = b"0123456789abcdef";
                out.extend_from_slice(&[
                    b'\\',
                    b'u',
                    b'0',
                    b'0',
                    HEX[(byte >> 4) as usize],
                    HEX[(byte & 0xF) as usize],
                ]);
            }
            other => out.extend_from_slice(&[b'\\', other]),
        }
        if out.len() > limit {
            return Err(Bail);
        }
        start = index + 1;
    }
    out.extend_from_slice(&bytes[start..]);
    out.push(b'"');
    Ok(())
}

// ---------------------------------------------------------------------------
// Decoding
// ---------------------------------------------------------------------------

/// A JSON syntax or bounds error. Callers map it to their own message.
#[derive(Debug)]
pub struct ParseError;

/// Receives parsed values. One parser drives both Python and `Value` output.
trait Sink {
    type Out;
    /// Validation-only sinks still visit every value, but need neither
    /// container storage nor owned/sorted object keys.
    const COLLECT_CONTAINERS: bool = true;
    fn null(&mut self) -> Result<Self::Out, ParseError>;
    fn boolean(&mut self, value: bool) -> Result<Self::Out, ParseError>;
    fn int(&mut self, value: i64) -> Result<Self::Out, ParseError>;
    fn uint(&mut self, value: u64) -> Result<Self::Out, ParseError>;
    fn big_int(&mut self, text: &str) -> Result<Self::Out, ParseError>;
    fn float(&mut self, value: f64) -> Result<Self::Out, ParseError>;
    fn string(&mut self, value: &str) -> Result<Self::Out, ParseError>;
    fn array(&mut self, items: Vec<Self::Out>) -> Result<Self::Out, ParseError>;
    /// Members are in document order; later duplicate keys overwrite earlier ones.
    fn object(&mut self, members: Vec<Member<'_, Self::Out>>) -> Result<Self::Out, ParseError>;
}

/// One parsed object member in document order.
pub struct Member<'a, T> {
    key: Cow<'a, str>,
    value: T,
}

/// Where a parsed string's validated UTF-8 bytes live.
enum Text {
    Input(usize, usize),
    Scratch,
}

struct Parser<'a, S> {
    input: &'a [u8],
    position: usize,
    sink: S,
    scratch: Vec<u8>,
}

fn parse<S: Sink>(input: &[u8], sink: S) -> Result<S::Out, ParseError> {
    let mut parser = Parser {
        input,
        position: 0,
        sink,
        scratch: Vec::new(),
    };
    parser.skip_whitespace();
    let value = parser.value(0)?;
    parser.skip_whitespace();
    if parser.position != input.len() {
        return Err(ParseError);
    }
    Ok(value)
}

impl<'a, S: Sink> Parser<'a, S> {
    #[inline]
    fn peek(&self) -> Option<u8> {
        self.input.get(self.position).copied()
    }

    #[inline]
    fn skip_whitespace(&mut self) {
        while let Some(b' ' | b'\n' | b'\t' | b'\r') = self.peek() {
            self.position += 1;
        }
    }

    fn literal(&mut self, text: &[u8]) -> Result<(), ParseError> {
        if self.input[self.position..].starts_with(text) {
            self.position += text.len();
            Ok(())
        } else {
            Err(ParseError)
        }
    }

    fn value(&mut self, depth: usize) -> Result<S::Out, ParseError> {
        if depth > MAX_DEPTH {
            return Err(ParseError);
        }
        match self.peek().ok_or(ParseError)? {
            b'{' => self.object(depth),
            b'[' => self.array(depth),
            b'"' => {
                self.position += 1;
                let location = self.string()?;
                let text = match location {
                    Text::Input(start, end) => &self.input[start..end],
                    Text::Scratch => &self.scratch[..],
                };
                // SAFETY: `string` validated these bytes as UTF-8.
                self.sink
                    .string(unsafe { std::str::from_utf8_unchecked(text) })
            }
            b't' => {
                self.literal(b"true")?;
                self.sink.boolean(true)
            }
            b'f' => {
                self.literal(b"false")?;
                self.sink.boolean(false)
            }
            b'n' => {
                self.literal(b"null")?;
                self.sink.null()
            }
            b'-' | b'0'..=b'9' => self.number(),
            _ => Err(ParseError),
        }
    }

    fn array(&mut self, depth: usize) -> Result<S::Out, ParseError> {
        self.position += 1;
        let mut items = Vec::new();
        self.skip_whitespace();
        if self.peek() == Some(b']') {
            self.position += 1;
            return self.sink.array(items);
        }
        loop {
            self.skip_whitespace();
            let item = self.value(depth + 1)?;
            if S::COLLECT_CONTAINERS {
                items.push(item);
            }
            self.skip_whitespace();
            match self.peek() {
                Some(b',') => self.position += 1,
                Some(b']') => {
                    self.position += 1;
                    return self.sink.array(items);
                }
                _ => return Err(ParseError),
            }
        }
    }

    fn object(&mut self, depth: usize) -> Result<S::Out, ParseError> {
        self.position += 1;
        let mut entries: Vec<Member<'a, S::Out>> = Vec::new();
        self.skip_whitespace();
        if self.peek() == Some(b'}') {
            self.position += 1;
            return self.sink.object(entries);
        }
        loop {
            self.skip_whitespace();
            if self.peek() != Some(b'"') {
                return Err(ParseError);
            }
            self.position += 1;
            let key = if S::COLLECT_CONTAINERS {
                Some(self.key()?)
            } else {
                self.string()?;
                None
            };
            self.skip_whitespace();
            if self.peek() != Some(b':') {
                return Err(ParseError);
            }
            self.position += 1;
            self.skip_whitespace();
            // Every value is parsed and validated, including ones a later
            // duplicate key discards.
            let item = self.value(depth + 1)?;
            if let Some(key) = key {
                entries.push(Member { key, value: item });
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
        self.sink.object(entries)
    }

    /// Parse an object key, borrowing from the input when it has no escapes.
    fn key(&mut self) -> Result<Cow<'a, str>, ParseError> {
        let input: &'a [u8] = self.input;
        let start = self.position;
        if let Some(end) = plain_run(input, start)
            && input[end] == b'"'
        {
            self.position = end + 1;
            let text = &input[start..end];
            if text.is_ascii() {
                // SAFETY: ASCII is valid UTF-8.
                return Ok(Cow::Borrowed(unsafe {
                    std::str::from_utf8_unchecked(text)
                }));
            }
            return std::str::from_utf8(text)
                .map(Cow::Borrowed)
                .map_err(|_| ParseError);
        }
        self.string()?;
        // SAFETY: `string` validated the scratch bytes as UTF-8.
        Ok(Cow::Owned(unsafe {
            String::from_utf8_unchecked(self.scratch.clone())
        }))
    }

    /// Parse a string body after its opening quote.
    fn string(&mut self) -> Result<Text, ParseError> {
        let input = self.input;
        let start = self.position;
        let end = plain_run(input, start).ok_or(ParseError)?;
        if input[end] == b'"' {
            self.position = end + 1;
            // ASCII (nearly every API string) is valid UTF-8; the word-wise
            // ASCII check is cheaper than the general validator's call.
            let text = &input[start..end];
            if !text.is_ascii() {
                std::str::from_utf8(text).map_err(|_| ParseError)?;
            }
            return Ok(Text::Input(start, end));
        }
        self.scratch.clear();
        self.scratch.extend_from_slice(&input[start..end]);
        self.position = end;
        loop {
            match input.get(self.position).copied().ok_or(ParseError)? {
                b'"' => {
                    self.position += 1;
                    break;
                }
                b'\\' => {
                    self.position += 1;
                    let escape = input.get(self.position).copied().ok_or(ParseError)?;
                    self.position += 1;
                    match escape {
                        b'"' => self.scratch.push(b'"'),
                        b'\\' => self.scratch.push(b'\\'),
                        b'/' => self.scratch.push(b'/'),
                        b'b' => self.scratch.push(8),
                        b'f' => self.scratch.push(12),
                        b'n' => self.scratch.push(b'\n'),
                        b'r' => self.scratch.push(b'\r'),
                        b't' => self.scratch.push(b'\t'),
                        b'u' => {
                            let character = self.unicode_escape()?;
                            let mut buffer = [0; 4];
                            self.scratch
                                .extend_from_slice(character.encode_utf8(&mut buffer).as_bytes());
                        }
                        _ => return Err(ParseError),
                    }
                }
                _ => {
                    let run_start = self.position;
                    let run_end = plain_run(input, run_start).ok_or(ParseError)?;
                    self.scratch.extend_from_slice(&input[run_start..run_end]);
                    self.position = run_end;
                }
            }
        }
        std::str::from_utf8(&self.scratch).map_err(|_| ParseError)?;
        Ok(Text::Scratch)
    }

    fn hex4(&mut self) -> Result<u16, ParseError> {
        let digits = self
            .input
            .get(self.position..self.position + 4)
            .ok_or(ParseError)?;
        let mut value = 0u16;
        for &digit in digits {
            let nibble = match digit {
                b'0'..=b'9' => digit - b'0',
                b'a'..=b'f' => digit - b'a' + 10,
                b'A'..=b'F' => digit - b'A' + 10,
                _ => return Err(ParseError),
            };
            value = value << 4 | u16::from(nibble);
        }
        self.position += 4;
        Ok(value)
    }

    fn unicode_escape(&mut self) -> Result<char, ParseError> {
        let first = self.hex4()?;
        match first {
            0xD800..=0xDBFF => {
                // A leading surrogate must be followed by an escaped trailing one.
                if self.input.get(self.position..self.position + 2) != Some(b"\\u") {
                    return Err(ParseError);
                }
                self.position += 2;
                let second = self.hex4()?;
                if !(0xDC00..=0xDFFF).contains(&second) {
                    return Err(ParseError);
                }
                let code =
                    0x10000 + ((u32::from(first) - 0xD800) << 10) + (u32::from(second) - 0xDC00);
                char::from_u32(code).ok_or(ParseError)
            }
            0xDC00..=0xDFFF => Err(ParseError),
            code => char::from_u32(u32::from(code)).ok_or(ParseError),
        }
    }

    fn number(&mut self) -> Result<S::Out, ParseError> {
        let input = self.input;
        let start = self.position;
        let mut position = start;
        let negative = input[position] == b'-';
        if negative {
            position += 1;
        }
        let digits_start = position;
        match input.get(position) {
            Some(b'0') => position += 1,
            Some(b'1'..=b'9') => {
                while let Some(b'0'..=b'9') = input.get(position) {
                    position += 1;
                }
            }
            _ => return Err(ParseError),
        }
        let integer_end = position;
        let mut is_float = false;
        if input.get(position) == Some(&b'.') {
            is_float = true;
            position += 1;
            if !matches!(input.get(position), Some(b'0'..=b'9')) {
                return Err(ParseError);
            }
            while let Some(b'0'..=b'9') = input.get(position) {
                position += 1;
            }
        }
        if let Some(b'e' | b'E') = input.get(position) {
            is_float = true;
            position += 1;
            if let Some(b'+' | b'-') = input.get(position) {
                position += 1;
            }
            if !matches!(input.get(position), Some(b'0'..=b'9')) {
                return Err(ParseError);
            }
            while let Some(b'0'..=b'9') = input.get(position) {
                position += 1;
            }
        }
        self.position = position;
        // SAFETY: the scanned range contains only ASCII digits, '-', '+', '.', 'e', 'E'.
        let text = unsafe { std::str::from_utf8_unchecked(&input[start..position]) };
        if is_float {
            let value: f64 = text.parse().map_err(|_| ParseError)?;
            if !value.is_finite() {
                return Err(ParseError);
            }
            return self.sink.float(value);
        }
        let digits = &input[digits_start..integer_end];
        if digits.len() <= 18 {
            let magnitude = digits
                .iter()
                .fold(0i64, |total, digit| total * 10 + i64::from(digit - b'0'));
            return self.sink.int(if negative { -magnitude } else { magnitude });
        }
        if digits.len() > MAX_INTEGER_DIGITS {
            return Err(ParseError);
        }
        if let Ok(value) = text.parse::<i64>() {
            return self.sink.int(value);
        }
        if let Ok(value) = text.parse::<u64>() {
            return self.sink.uint(value);
        }
        self.sink.big_int(text)
    }
}

/// Find the end of a run of string bytes that need no unescaping.
///
/// Returns the index of the first `"` or `\`, or `None` for a raw control
/// character or end of input.
#[inline]
fn plain_run(input: &[u8], start: usize) -> Option<usize> {
    let index = first_escape(input, start);
    match input.get(index) {
        Some(b'"' | b'\\') => Some(index),
        _ => None,
    }
}

/// Parse JSON into a `serde_json::Value` in a single pass. No GIL is needed.
pub fn parse_value(input: &[u8]) -> Result<Value, ParseError> {
    parse(input, ValueSink)
}

struct ValueSink;

impl Sink for ValueSink {
    type Out = Value;
    fn null(&mut self) -> Result<Value, ParseError> {
        Ok(Value::Null)
    }
    fn boolean(&mut self, value: bool) -> Result<Value, ParseError> {
        Ok(Value::Bool(value))
    }
    fn int(&mut self, value: i64) -> Result<Value, ParseError> {
        Ok(Value::from(value))
    }
    fn uint(&mut self, value: u64) -> Result<Value, ParseError> {
        Ok(Value::from(value))
    }
    fn big_int(&mut self, text: &str) -> Result<Value, ParseError> {
        text.parse::<serde_json::Number>()
            .map(Value::Number)
            .map_err(|_| ParseError)
    }
    fn float(&mut self, value: f64) -> Result<Value, ParseError> {
        serde_json::Number::from_f64(value)
            .map(Value::Number)
            .ok_or(ParseError)
    }
    fn string(&mut self, value: &str) -> Result<Value, ParseError> {
        Ok(Value::String(value.to_owned()))
    }
    fn array(&mut self, items: Vec<Value>) -> Result<Value, ParseError> {
        Ok(Value::Array(items))
    }
    fn object(&mut self, members: Vec<Member<'_, Value>>) -> Result<Value, ParseError> {
        let mut map = Map::new();
        for member in members {
            map.insert(member.key.into_owned(), member.value);
        }
        Ok(Value::Object(map))
    }
}

/// A new `str` for `value`, or null with an exception set.
///
/// ASCII text is copied straight into a compact ASCII string: CPython's
/// UTF-8 decoder would scan it again to find its maximum character, which
/// the caller's parse has in effect already done.
#[inline]
pub(crate) fn new_str(value: &str) -> *mut ffi::PyObject {
    let length = value.len() as ffi::Py_ssize_t;
    #[cfg(not(any(Py_LIMITED_API, PyPy, GraalPy)))]
    if value.is_ascii() {
        // SAFETY: a string made by `PyUnicode_New` with maximum character
        // 127 is compact ASCII with room for `length` one-byte characters
        // (and its terminator, which it writes). Filling it before anyone
        // else sees it is how CPython's own constructors build strings.
        unsafe {
            let object = ffi::PyUnicode_New(length, 127);
            if !object.is_null() {
                std::ptr::copy_nonoverlapping(
                    value.as_ptr(),
                    ffi::PyUnicode_DATA(object).cast::<u8>(),
                    value.len(),
                );
            }
            return object;
        }
    }
    // SAFETY: `value` is valid UTF-8 for its whole length.
    unsafe { ffi::PyUnicode_FromStringAndSize(value.as_ptr().cast(), length) }
}

/// Parse JSON straight into Python objects in a single pass.
pub fn parse_python<'py>(py: Python<'py>, input: &[u8]) -> Result<Bound<'py, PyAny>, ParseError> {
    parse(input, PySink { py })
}

struct PySink<'py> {
    py: Python<'py>,
}

impl<'py> PySink<'py> {
    #[inline]
    fn owned(&self, object: *mut ffi::PyObject) -> Result<Bound<'py, PyAny>, ParseError> {
        // SAFETY: `object` is a new reference or null from a C API constructor.
        unsafe { Bound::from_owned_ptr_or_opt(self.py, object) }.ok_or_else(|| {
            // Only allocation can fail here; report it as a parse failure and
            // drop the pending Python error.
            let _ = PyErr::take(self.py);
            ParseError
        })
    }

    fn text(&self, value: &str) -> Result<Bound<'py, PyAny>, ParseError> {
        self.owned(new_str(value))
    }
}

type CachedKey = (Box<str>, Py<PyAny>);

const KEY_CACHE_SLOTS: usize = 1024;
const KEY_CACHE_MAX_LENGTH: usize = 64;

thread_local! {
    // Recently seen short object keys, interned. API payloads repeat the same
    // keys across objects and requests, so most keys skip string creation and
    // dict lookups compare interned pointers.
    static KEYS: RefCell<Vec<Option<CachedKey>>> =
        RefCell::new((0..KEY_CACHE_SLOTS).map(|_| None).collect());
}

#[inline]
fn key_slot(key: &[u8]) -> usize {
    // FNV-1a: short keys, no adversarial concern beyond cache misses.
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for &byte in key {
        hash = (hash ^ u64::from(byte)).wrapping_mul(0x0100_0000_01b3);
    }
    (hash as usize) & (KEY_CACHE_SLOTS - 1)
}

impl<'py> PySink<'py> {
    fn key(&self, key: &str) -> Result<Bound<'py, PyAny>, ParseError> {
        if key.len() > KEY_CACHE_MAX_LENGTH {
            return self.text(key);
        }
        let slot = key_slot(key.as_bytes());
        let cached = KEYS.with(|cell| {
            let cache = cell.try_borrow().ok()?;
            match &cache[slot] {
                Some((text, object)) if &**text == key => Some(object.bind(self.py).clone()),
                _ => None,
            }
        });
        if let Some(object) = cached {
            return Ok(object);
        }
        let object = self.text(key)?;
        // SAFETY: `object` is an exact str with one owned reference held here;
        // interning may replace it with the canonical equal string.
        let object = unsafe {
            let mut pointer = object.into_ptr();
            ffi::PyUnicode_InternInPlace(&mut pointer);
            Bound::from_owned_ptr(self.py, pointer)
        };
        KEYS.with(|cell| {
            if let Ok(mut cache) = cell.try_borrow_mut() {
                cache[slot] = Some((key.into(), object.clone().unbind()));
            }
        });
        Ok(object)
    }
}

impl<'py> Sink for PySink<'py> {
    type Out = Bound<'py, PyAny>;
    fn null(&mut self) -> Result<Self::Out, ParseError> {
        Ok(self.py.None().into_bound(self.py))
    }
    fn boolean(&mut self, value: bool) -> Result<Self::Out, ParseError> {
        // SAFETY: the bool singletons are immortal; take a new reference.
        Ok(unsafe {
            Bound::from_borrowed_ptr(
                self.py,
                if value {
                    ffi::Py_True()
                } else {
                    ffi::Py_False()
                },
            )
        })
    }
    fn int(&mut self, value: i64) -> Result<Self::Out, ParseError> {
        // SAFETY: plain constructor call with the GIL held.
        self.owned(unsafe { ffi::PyLong_FromLongLong(value) })
    }
    fn uint(&mut self, value: u64) -> Result<Self::Out, ParseError> {
        // SAFETY: plain constructor call with the GIL held.
        self.owned(unsafe { ffi::PyLong_FromUnsignedLongLong(value) })
    }
    fn big_int(&mut self, text: &str) -> Result<Self::Out, ParseError> {
        let text = std::ffi::CString::new(text).map_err(|_| ParseError)?;
        // SAFETY: `text` is a NUL-terminated decimal integer of at most 4096 digits.
        self.owned(unsafe { ffi::PyLong_FromString(text.as_ptr(), std::ptr::null_mut(), 10) })
    }
    fn float(&mut self, value: f64) -> Result<Self::Out, ParseError> {
        // SAFETY: plain constructor call with the GIL held.
        self.owned(unsafe { ffi::PyFloat_FromDouble(value) })
    }
    fn string(&mut self, value: &str) -> Result<Self::Out, ParseError> {
        self.text(value)
    }
    fn array(&mut self, items: Vec<Self::Out>) -> Result<Self::Out, ParseError> {
        // SAFETY: PyList_New returns a list with `len` NULL slots; each
        // PyList_SET_ITEM steals one owned reference into its slot.
        unsafe {
            let list = self.owned(ffi::PyList_New(items.len() as ffi::Py_ssize_t))?;
            for (index, item) in items.into_iter().enumerate() {
                ffi::PyList_SET_ITEM(list.as_ptr(), index as ffi::Py_ssize_t, item.into_ptr());
            }
            Ok(list)
        }
    }
    fn object(&mut self, members: Vec<Member<'_, Self::Out>>) -> Result<Self::Out, ParseError> {
        // SAFETY: new dict; PyDict_SetItem borrows key and value.
        unsafe {
            let dict = self.owned(ffi::PyDict_New())?;
            for Member { key, value, .. } in members {
                let key = self.key(&key)?;
                if ffi::PyDict_SetItem(dict.as_ptr(), key.as_ptr(), value.as_ptr()) != 0 {
                    let _ = PyErr::take(self.py);
                    return Err(ParseError);
                }
            }
            Ok(dict)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_float_cache_is_bounded_and_repeat_encodes_are_identical() {
        let scratch = Scratch::default();
        // Exactly the fixed table: no growth on any path.
        assert_eq!(scratch.floats.len(), FLOAT_SLOTS);
        assert_eq!(std::mem::size_of::<FloatSlot>(), 48);
        Python::attach(|py| {
            // One miss and several hits in the same and later encodes; the
            // repeated encodes produce the same bytes and re-fill cold slots.
            let values = py
                .eval(c"[2.0, 0.1, 2.0, -0.0, 0.0, 2.0, 1.5e300, 2.0]", None, None)?
                .cast_into::<pyo3::types::PyList>()
                .map_err(PyErr::from)?;
            let first = encode_plain(&values, usize::MAX, <[u8]>::to_vec).unwrap();
            assert_eq!(
                first,
                // zmij's own formatting, as every encode (cached or not).
                &b"[2.0,0.1,2.0,-0.0,0.0,2.0,1.5e+300,2.0]"[..]
            );
            for _ in 0..3 {
                let repeat = encode_plain(&values, usize::MAX, <[u8]>::to_vec).unwrap();
                assert_eq!(repeat, first);
            }
            Ok::<_, PyErr>(())
        })
        .unwrap();
    }

    fn retained() -> (usize, usize) {
        SCRATCH.with(|cell| {
            let scratch = cell.borrow();
            (scratch.out.capacity(), scratch.entries.capacity())
        })
    }

    #[test]
    fn escaped_output_never_outgrows_the_limit() {
        Python::attach(|py| {
            let limit = 1 << 20;
            for (expression, fits) in [
                (format!("'\\x00' * {}", limit - 2), false),
                (format!("{{'\\x01' * {}: 1}}", limit), false),
                (format!("'\u{e9}' * {}", limit / 2), false),
                (format!("'\\x00' * {}", limit / 6 - 1), true),
            ] {
                SCRATCH.with(|cell| *cell.borrow_mut() = Scratch::default());
                let code = std::ffi::CString::new(expression).unwrap();
                let value = py.eval(&code, None, None).unwrap();
                let mut peak = 0;
                let encoded = encode_plain(&value, limit, |bytes| bytes.len());
                SCRATCH.with(|cell| peak = cell.borrow().out.len());
                assert_eq!(encoded.is_some(), fits);
                // At most one six-byte escape past the limit is ever written.
                assert!(
                    peak <= limit + 6,
                    "wrote {peak} bytes for a {limit}-byte limit"
                );
            }
        });
    }

    #[test]
    fn large_scratch_buffers_are_released() {
        Python::attach(|py| {
            let value = py
                .eval(c"{f'k{i}': 'x' * 8 for i in range(20000)}", None, None)
                .unwrap();
            assert!(encode_plain(&value, 1 << 24, |bytes| bytes.len()).is_some());
            let (out, entries) = retained();
            assert!(out <= RETAINED_CAPACITY && entries <= RETAINED_ENTRIES);
        });
    }
}
