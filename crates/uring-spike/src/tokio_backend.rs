//! tokio current_thread runtime, one task per connection.

use crate::http::{Served, serve};
use std::io;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

pub fn run(listener: std::net::TcpListener) -> io::Result<()> {
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_io()
        .build()?;
    let local = tokio::task::LocalSet::new();
    local.block_on(&rt, async move {
        let listener = tokio::net::TcpListener::from_std(listener)?;
        loop {
            let (stream, _) = listener.accept().await?;
            tokio::task::spawn_local(connection(stream));
        }
    })
}

async fn connection(mut stream: tokio::net::TcpStream) {
    let mut input: Vec<u8> = Vec::with_capacity(4096);
    let mut out: Vec<u8> = Vec::with_capacity(4096);
    loop {
        match stream.read_buf(&mut input).await {
            Ok(0) | Err(_) => return,
            Ok(_) => {}
        }
        let Served::Ok { consumed, close } = serve(&input, &mut out) else {
            return;
        };
        input.drain(..consumed);
        if !out.is_empty() {
            if stream.write_all(&out).await.is_err() {
                return;
            }
            out.clear();
        }
        if close {
            return;
        }
    }
}
