//! An asyncio event loop with its scheduler and reactor in Rust.
//!
//! See `README.md` in this crate for the design, what is and is not covered,
//! and how it was tested and measured. The classes are registered into the
//! server's extension module; `bessemer._loop.Loop` combines `LoopCore` with
//! asyncio's Unix selector loop.

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
compile_error!("bessemer-loop needs epoll (Linux) or kqueue (macOS)");

mod core;
mod driver;
mod handle;
mod poller;
mod sock;
#[cfg(target_os = "linux")]
pub mod uring;

use pyo3::prelude::*;

pub use core::LoopCore;

/// Add the loop's classes to `module`.
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<core::LoopCore>()?;
    module.add_class::<handle::Handle>()?;
    module.add_class::<sock::SockRecv>()?;
    module.add_class::<sock::SockSendall>()?;
    module.add_class::<sock::SockDone>()?;
    Ok(())
}
