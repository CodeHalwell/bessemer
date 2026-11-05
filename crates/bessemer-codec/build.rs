fn main() {
    // Exposes `cfg(Py_GIL_DISABLED)` for free-threaded CPython builds.
    pyo3_build_config::use_pyo3_cfgs();
}
