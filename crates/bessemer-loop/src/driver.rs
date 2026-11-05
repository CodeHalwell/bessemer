//! An embedded Tokio runtime driven by the loop's own thread.
//!
//! With a driver, the loop waits for its epoll descriptor inside a Tokio
//! current-thread runtime instead of blocking in `epoll_wait`, so futures
//! spawned on that runtime (the HTTP server's connections) run on the loop's
//! thread between its callbacks, without another thread or a hand-off.

use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::time::Duration;

use tokio::io::unix::AsyncFd;
use tokio::runtime::{Builder, Handle, Runtime};

/// A duplicate of the loop's epoll descriptor, owned by the driver, so it
/// stays open while registered with Tokio even after the loop closes its own.
struct Epoll(OwnedFd);

impl AsRawFd for Epoll {
    fn as_raw_fd(&self) -> RawFd {
        self.0.as_raw_fd()
    }
}

pub(crate) struct Driver {
    /// Dropped before the runtime, whose reactor it is registered with.
    epoll: Option<AsyncFd<Epoll>>,
    /// Ends a wait early for work queued on the loop without a descriptor.
    notify: tokio::sync::Notify,
    /// Always set until the driver is dropped.
    runtime: Option<Runtime>,
}

impl Drop for Driver {
    fn drop(&mut self) {
        drop(self.epoll.take());
        if let Some(runtime) = self.runtime.take() {
            // Drop the spawned tasks without waiting for blocking work, which
            // may itself be waiting on the loop.
            runtime.shutdown_background();
        }
    }
}

impl Driver {
    pub(crate) fn new(epfd: RawFd) -> std::io::Result<Self> {
        let runtime = Builder::new_current_thread().enable_all().build()?;
        let duplicate = unsafe { libc::fcntl(epfd, libc::F_DUPFD_CLOEXEC, 0) };
        if duplicate < 0 {
            return Err(std::io::Error::last_os_error());
        }
        // SAFETY: `duplicate` is a new descriptor that nothing else owns.
        let owned = unsafe { OwnedFd::from_raw_fd(duplicate) };
        let epoll = {
            let _context = runtime.enter();
            // SAFETY: the `Epoll` owns the descriptor for the AsyncFd's life.
            unsafe { AsyncFd::register_with_interest(Epoll(owned), tokio::io::Interest::READABLE) }
                .map_err(|error| error.into_parts().1)?
        };
        Ok(Self {
            epoll: Some(epoll),
            notify: tokio::sync::Notify::new(),
            runtime: Some(runtime),
        })
    }

    fn runtime(&self) -> &Runtime {
        self.runtime
            .as_ref()
            .expect("the runtime lives as long as the driver")
    }

    fn epoll(&self) -> &AsyncFd<Epoll> {
        self.epoll
            .as_ref()
            .expect("the descriptor lives as long as the driver")
    }

    pub(crate) fn handle(&self) -> Handle {
        self.runtime().handle().clone()
    }

    /// Run the runtime's ready work, then wait up to `timeout_ms` (-1 for no
    /// limit, 0 for none) for the loop's epoll descriptor to become readable.
    pub(crate) fn wait(&self, timeout_ms: i32) {
        self.runtime().block_on(self.wait_async(timeout_ms));
    }

    /// End the current or next wait.
    pub(crate) fn notify(&self) {
        self.notify.notify_one();
    }

    /// Run `future` on the runtime from the calling thread.
    pub(crate) fn block_on<F: std::future::Future>(&self, future: F) -> F::Output {
        self.runtime().block_on(future)
    }

    /// As `wait`, from inside `block_on`.
    pub(crate) async fn wait_async(&self, timeout_ms: i32) -> bool {
        if timeout_ms == 0 {
            // Let spawned tasks that are ready run once, without the
            // scheduler's extra driver poll that `yield_now` asks for.
            let mut yielded = false;
            std::future::poll_fn(|cx| {
                if yielded {
                    return std::task::Poll::Ready(());
                }
                yielded = true;
                cx.waker().wake_by_ref();
                std::task::Poll::Pending
            })
            .await;
        } else {
            let wait = async {
                tokio::select! {
                    biased;
                    _ = self.notify.notified() => {}
                    _ = self.epoll().readable() => {}
                }
            };
            if timeout_ms < 0 {
                wait.await;
            } else {
                let _ = tokio::time::timeout(Duration::from_millis(timeout_ms as u64), wait).await;
            }
        }
        self.take_readable()
    }

    /// Whether the epoll descriptor has reported events since the last call;
    /// the caller then drains it. A later event makes it readable again.
    fn take_readable(&self) -> bool {
        let waker = std::task::Waker::noop();
        let mut cx = std::task::Context::from_waker(waker);
        match self.epoll().poll_read_ready(&mut cx) {
            std::task::Poll::Ready(Ok(mut guard)) => {
                guard.clear_ready();
                true
            }
            std::task::Poll::Ready(Err(_)) => true,
            std::task::Poll::Pending => false,
        }
    }
}
