//! The few operating-system differences the extension needs to paper over,
//! so the rest of the crate builds unchanged on Linux, macOS and Windows.

use std::fs::{File, OpenOptions};
use std::io;
use std::path::Path;

/// The connected, non-blocking socket pair that wakes the asyncio loop when
/// work is queued. A Unix domain socket pair on POSIX; on Windows, where
/// asyncio's selector loop can only watch sockets, a loopback TCP pair.
#[cfg(unix)]
pub(crate) type WakeStream = std::os::unix::net::UnixStream;
#[cfg(windows)]
pub(crate) type WakeStream = std::net::TcpStream;

#[cfg(unix)]
pub(crate) fn wake_pair() -> io::Result<(WakeStream, WakeStream)> {
    let (sender, receiver) = WakeStream::pair()?;
    sender.set_nonblocking(true)?;
    receiver.set_nonblocking(true)?;
    Ok((sender, receiver))
}

#[cfg(windows)]
pub(crate) fn wake_pair() -> io::Result<(WakeStream, WakeStream)> {
    use std::net::{Ipv4Addr, SocketAddr, TcpListener};
    let listener = TcpListener::bind(SocketAddr::from((Ipv4Addr::LOCALHOST, 0)))?;
    let sender = WakeStream::connect(listener.local_addr()?)?;
    // Accept until the connection is the one just made, so another local
    // process racing to connect cannot become the wake socket.
    let receiver = loop {
        let (stream, peer) = listener.accept()?;
        if peer == sender.local_addr()? {
            break stream;
        }
    };
    sender.set_nodelay(true)?;
    sender.set_nonblocking(true)?;
    receiver.set_nonblocking(true)?;
    Ok((sender, receiver))
}

/// The descriptor (POSIX) or socket handle (Windows) asyncio watches.
#[cfg(unix)]
pub(crate) fn wake_handle(stream: &WakeStream) -> i64 {
    use std::os::fd::AsRawFd;
    i64::from(stream.as_raw_fd())
}

#[cfg(windows)]
pub(crate) fn wake_handle(stream: &WakeStream) -> i64 {
    use std::os::windows::io::AsRawSocket;
    stream.as_raw_socket() as i64
}

/// Fill `buffer` from `file` at `offset` without moving a shared cursor.
#[cfg(unix)]
pub(crate) fn read_exact_at(file: &File, buffer: &mut [u8], offset: u64) -> io::Result<()> {
    use std::os::unix::fs::FileExt;
    file.read_exact_at(buffer, offset)
}

/// Fill `buffer` from `file` at `offset`. `seek_read` moves the handle's
/// cursor, but every reader of a spool file passes its own offset.
#[cfg(windows)]
pub(crate) fn read_exact_at(file: &File, mut buffer: &mut [u8], mut offset: u64) -> io::Result<()> {
    use std::os::windows::fs::FileExt;
    while !buffer.is_empty() {
        match file.seek_read(buffer, offset) {
            Ok(0) => {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "failed to fill whole buffer",
                ));
            }
            Ok(count) => {
                buffer = &mut buffer[count..];
                offset += count as u64;
            }
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

/// Create a private temporary file at `path` that disappears when its last
/// handle closes. POSIX unlinks it at once; Windows cannot delete an open
/// file, so it is opened delete-on-close instead.
#[cfg(unix)]
pub(crate) fn anonymous_file(path: &Path) -> io::Result<File> {
    use std::os::unix::fs::OpenOptionsExt;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    std::fs::remove_file(path)?;
    Ok(file)
}

#[cfg(windows)]
pub(crate) fn anonymous_file(path: &Path) -> io::Result<File> {
    use std::os::windows::fs::OpenOptionsExt;
    const FILE_SHARE_READ: u32 = 0x1;
    const FILE_SHARE_WRITE: u32 = 0x2;
    const FILE_SHARE_DELETE: u32 = 0x4;
    const FILE_ATTRIBUTE_TEMPORARY: u32 = 0x100;
    const FILE_FLAG_DELETE_ON_CLOSE: u32 = 0x0400_0000;
    OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        .attributes(FILE_ATTRIBUTE_TEMPORARY)
        .custom_flags(FILE_FLAG_DELETE_ON_CLOSE)
        .open(path)
}
