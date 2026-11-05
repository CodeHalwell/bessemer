//! An asyncio event loop with its scheduler and reactor in Rust.
//!
//! See `README.md` in this crate for the design, what is and is not covered,
//! and how it was tested and measured. The classes are registered into the
//! server's extension module; `bessemer._loop.Loop` combines `LoopCore` with
//! asyncio's Unix selector loop.

#[cfg(not(target_os = "linux"))]
compile_error!("bessemer-loop is Linux-only (it uses epoll directly)");

mod core;
mod handle;
mod sock;

use pyo3::prelude::*;

/// Add the loop's classes to `module`.
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<core::LoopCore>()?;
    module.add_class::<handle::Handle>()?;
    module.add_class::<sock::SockRecv>()?;
    module.add_class::<sock::SockSendall>()?;
    module.add_class::<sock::SockDone>()?;
    Ok(())
}
