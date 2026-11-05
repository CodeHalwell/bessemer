//! Server-side TLS termination with rustls.

use rustls::pki_types::{CertificateDer, PrivateKeyDer, pem::PemObject};
use std::sync::Arc;

/// Build the server configuration from PEM files, read once at startup.
///
/// `minimum` is the lowest protocol version accepted: `"1.3"` (the default)
/// or `"1.2"`. Errors name the file and the problem, never key material.
/// ALPN offers HTTP/2 first when `http2` is set, then HTTP/1.1.
pub(crate) fn server_config(
    certfile: &str,
    keyfile: &str,
    minimum: &str,
    http2: bool,
) -> Result<Arc<rustls::ServerConfig>, String> {
    let versions: &[&'static rustls::SupportedProtocolVersion] = match minimum {
        "1.3" => &[&rustls::version::TLS13],
        "1.2" => &[&rustls::version::TLS13, &rustls::version::TLS12],
        _ => return Err("tls_min must be '1.2' or '1.3'".to_owned()),
    };
    let certificates: Vec<CertificateDer<'static>> = CertificateDer::pem_file_iter(certfile)
        .map_err(|error| format!("cannot read tls_cert {certfile}: {error}"))?
        .collect::<Result<_, _>>()
        .map_err(|error| format!("invalid certificate in tls_cert {certfile}: {error}"))?;
    if certificates.is_empty() {
        return Err(format!("tls_cert {certfile} contains no certificates"));
    }
    let key = PrivateKeyDer::from_pem_file(keyfile)
        .map_err(|error| format!("cannot read a private key from tls_key {keyfile}: {error}"))?;
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let mut config = rustls::ServerConfig::builder_with_provider(provider)
        .with_protocol_versions(versions)
        .map_err(|error| format!("TLS configuration failed: {error}"))?
        .with_no_client_auth()
        .with_single_cert(certificates, key)
        .map_err(|error| format!("tls_cert and tls_key do not form a usable pair: {error}"))?;
    config.alpn_protocols = if http2 {
        vec![b"h2".to_vec(), b"http/1.1".to_vec()]
    } else {
        vec![b"http/1.1".to_vec()]
    };
    Ok(Arc::new(config))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::transport::{Application, Limits, Secure, Stop, run};
    use axum::{Router, extract::Request, routing::get};
    use std::time::Duration;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    fn certificate_files(name: &str) -> (std::path::PathBuf, String, String, String) {
        let certified = rcgen::generate_simple_self_signed(vec!["localhost".into()]).unwrap();
        let directory =
            std::env::temp_dir().join(format!("bessemer-tls-{name}-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let cert = directory.join("cert.pem");
        let key = directory.join("key.pem");
        std::fs::write(&cert, certified.cert.pem()).unwrap();
        std::fs::write(&key, certified.key_pair.serialize_pem()).unwrap();
        (
            directory,
            cert.to_str().unwrap().to_owned(),
            key.to_str().unwrap().to_owned(),
            certified.cert.pem(),
        )
    }

    fn client(ca_pem: &str) -> tokio_rustls::TlsConnector {
        tokio_rustls::TlsConnector::from(client_config(ca_pem))
    }

    fn client_config(ca_pem: &str) -> Arc<rustls::ClientConfig> {
        client_config_with(ca_pem, rustls::ALL_VERSIONS)
    }

    fn client_with(
        ca_pem: &str,
        versions: &[&'static rustls::SupportedProtocolVersion],
    ) -> tokio_rustls::TlsConnector {
        tokio_rustls::TlsConnector::from(client_config_with(ca_pem, versions))
    }

    fn client_config_with(
        ca_pem: &str,
        versions: &[&'static rustls::SupportedProtocolVersion],
    ) -> Arc<rustls::ClientConfig> {
        let mut roots = rustls::RootCertStore::empty();
        roots
            .add(CertificateDer::from_pem_slice(ca_pem.as_bytes()).unwrap())
            .unwrap();
        let config = rustls::ClientConfig::builder_with_provider(Arc::new(
            rustls::crypto::ring::default_provider(),
        ))
        .with_protocol_versions(versions)
        .unwrap()
        .with_root_certificates(roots)
        .with_no_client_auth();
        Arc::new(config)
    }

    #[test]
    fn invalid_files_are_reported_without_key_material() {
        let (directory, cert, key, _) = certificate_files("invalid");
        let missing = directory.join("missing.pem");
        let error = server_config(missing.to_str().unwrap(), &key, "1.3", false).unwrap_err();
        assert!(error.contains("tls_cert"), "{error}");
        let error = server_config(&cert, &cert, "1.3", false).unwrap_err();
        assert!(error.contains("tls_key"), "{error}");
        let (_, _, other_key, _) = certificate_files("other");
        let error = server_config(&cert, &other_key, "1.3", false).unwrap_err();
        assert!(error.contains("usable pair"), "{error}");
        assert!(!error.contains("PRIVATE KEY"), "{error}");
        assert!(server_config(&cert, &key, "1.3", false).is_ok());
        let error = server_config(&cert, &key, "1.1", false).unwrap_err();
        assert!(error.contains("tls_min"), "{error}");
    }

    async fn handshake(
        minimum: &str,
        client_versions: &[&'static rustls::SupportedProtocolVersion],
    ) -> Option<rustls::ProtocolVersion> {
        let (_directory, cert, key, ca) =
            certificate_files(&format!("min-{minimum}-{}", client_versions.len()));
        let acceptor =
            tokio_rustls::TlsAcceptor::from(server_config(&cert, &key, minimum, false).unwrap());
        let (client_end, server_end) = tokio::io::duplex(64 * 1024);
        let server = tokio::spawn(async move { acceptor.accept(server_end).await.is_ok() });
        let name = rustls::pki_types::ServerName::try_from("localhost").unwrap();
        let connected = client_with(&ca, client_versions)
            .connect(name, client_end)
            .await;
        let version = connected
            .ok()
            .and_then(|stream| stream.get_ref().1.protocol_version());
        let _ = server.await;
        version
    }

    #[tokio::test]
    async fn tls_1_2_is_refused_unless_opted_in() {
        let tls12: &[&'static rustls::SupportedProtocolVersion] = &[&rustls::version::TLS12];
        assert_eq!(handshake("1.3", tls12).await, None);
        assert_eq!(
            handshake("1.2", tls12).await,
            Some(rustls::ProtocolVersion::TLSv1_2)
        );
        assert_eq!(
            handshake("1.3", rustls::ALL_VERSIONS).await,
            Some(rustls::ProtocolVersion::TLSv1_3)
        );
    }

    #[tokio::test]
    async fn requests_are_served_over_tls_and_marked_secure() {
        let (_directory, cert, key, ca) = certificate_files("serve");
        let config = server_config(&cert, &key, "1.3", true).unwrap();
        let app = Router::new().route(
            "/",
            get(|request: Request| async move {
                if request.extensions().get::<Secure>().is_some() {
                    "secure"
                } else {
                    "plain"
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let (stop, server_end) = std::os::unix::net::UnixStream::pair().unwrap();
        server_end.set_nonblocking(true).unwrap();
        let limits = Limits {
            max_connections: 4,
            header_timeout: Duration::from_millis(500),
            max_headers: 64,
            max_header_size: 8192,
            max_request_target_size: 8192,
            graceful_timeout: Duration::from_secs(5),
            max_requests: 0,
            http2: true,
        };
        let server = tokio::spawn(run(
            listener,
            Application::Router(app),
            limits,
            None,
            Stop::Socket(server_end),
            Some(config),
        ));
        let name = rustls::pki_types::ServerName::try_from("localhost").unwrap();
        let tcp = tokio::net::TcpStream::connect(address).await.unwrap();
        let mut stream = client(&ca).connect(name.clone(), tcp).await.unwrap();
        assert_eq!(stream.get_ref().1.alpn_protocol(), None);
        stream
            .write_all(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            .await
            .unwrap();
        let mut response = Vec::new();
        stream.read_to_end(&mut response).await.unwrap();
        let response = String::from_utf8(response).unwrap();
        assert!(response.starts_with("HTTP/1.1 200"), "{response}");
        assert!(response.ends_with("secure"), "{response}");

        // A client offering h2 by ALPN is served HTTP/2, still marked secure.
        let mut connector_config = (*client_config(&ca)).clone();
        connector_config.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
        let tcp = tokio::net::TcpStream::connect(address).await.unwrap();
        let stream = tokio_rustls::TlsConnector::from(Arc::new(connector_config))
            .connect(name.clone(), tcp)
            .await
            .unwrap();
        assert_eq!(stream.get_ref().1.alpn_protocol(), Some(&b"h2"[..]));
        let (mut sender, connection) = hyper::client::conn::http2::handshake(
            hyper_util::rt::TokioExecutor::new(),
            hyper_util::rt::TokioIo::new(stream),
        )
        .await
        .unwrap();
        let connection = tokio::spawn(connection);
        let response = sender
            .send_request(
                axum::http::Request::get("https://localhost/")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.version(), axum::http::Version::HTTP_2);
        let body = http_body_util::BodyExt::collect(response.into_body())
            .await
            .unwrap()
            .to_bytes();
        assert_eq!(&body[..], b"secure");
        drop(sender);
        let _ = connection.await;

        // Plaintext on the TLS port fails the handshake and is closed.
        let mut plain = tokio::net::TcpStream::connect(address).await.unwrap();
        plain
            .write_all(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            .await
            .unwrap();
        let mut answer = Vec::new();
        let _ = plain.read_to_end(&mut answer).await;
        assert!(!String::from_utf8_lossy(&answer).contains("secure"));

        // A client that never starts the handshake is dropped at the header
        // deadline rather than holding its connection slot.
        let mut silent = tokio::net::TcpStream::connect(address).await.unwrap();
        let mut byte = [0u8; 1];
        let closed = tokio::time::timeout(Duration::from_secs(3), silent.read(&mut byte)).await;
        assert!(matches!(closed, Ok(Ok(0)) | Ok(Err(_))), "{closed:?}");

        drop(stop);
        server.await.unwrap().unwrap();
    }
}
