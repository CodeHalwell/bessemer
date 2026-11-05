//! A spike of an asyncio event loop with its scheduler and reactor in Rust.
//!
//! See `README.md` in this crate for the design, what is and is not covered,
//! and how it was tested and measured.

#[cfg(not(target_os = "linux"))]
compile_error!("bessemer-loop is a Linux-only spike (it uses epoll directly)");

mod core;
mod handle;
mod sock;

use pyo3::prelude::*;

#[pymodule]
fn _loop(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<core::LoopCore>()?;
    m.add_class::<handle::Handle>()?;
    m.add_class::<sock::SockRecv>()?;
    m.add_class::<sock::SockSendall>()?;
    m.add_class::<sock::SockDone>()?;
    Ok(())
}
