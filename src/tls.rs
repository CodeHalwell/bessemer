//! Server-side TLS termination with rustls.

use rustls::pki_types::{CertificateDer, PrivateKeyDer, pem::PemObject};
use std::sync::Arc;

/// What the server's TLS endpoint is configured with.
#[derive(Clone, Debug)]
pub(crate) struct Settings {
    pub(crate) cert: String,
    pub(crate) key: String,
    /// The lowest protocol version accepted: `"1.3"` (the default) or `"1.2"`.
    pub(crate) minimum: String,
    /// Offer HTTP/2 by ALPN, ahead of HTTP/1.1.
    pub(crate) http2: bool,
    /// PEM bundle of the CAs that client certificates must chain to. Without
    /// it no client certificate is requested.
    pub(crate) client_ca: Option<String>,
    /// Refuse clients that present no certificate; otherwise one is verified
    /// when presented but not required.
    pub(crate) client_required: bool,
}

/// Build the server configuration from PEM files, read once at startup.
///
/// Errors name the file and the problem, never key material.
pub(crate) fn server_config(settings: &Settings) -> Result<Arc<rustls::ServerConfig>, String> {
    let versions: &[&'static rustls::SupportedProtocolVersion] = match settings.minimum.as_str() {
        "1.3" => &[&rustls::version::TLS13],
        "1.2" => &[&rustls::version::TLS13, &rustls::version::TLS12],
        _ => return Err("tls_min must be '1.2' or '1.3'".to_owned()),
    };
    let certificates = read_certificates(&settings.cert, "tls_cert")?;
    let key = PrivateKeyDer::from_pem_file(&settings.key).map_err(|error| {
        format!(
            "cannot read a private key from tls_key {}: {error}",
            settings.key
        )
    })?;
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let builder = rustls::ServerConfig::builder_with_provider(provider.clone())
        .with_protocol_versions(versions)
        .map_err(|error| format!("TLS configuration failed: {error}"))?;
    let builder = match &settings.client_ca {
        None => builder.with_no_client_auth(),
        Some(path) => {
            let mut roots = rustls::RootCertStore::empty();
            for certificate in read_certificates(path, "tls_client_ca")? {
                roots.add(certificate).map_err(|error| {
                    format!("invalid CA certificate in tls_client_ca {path}: {error}")
                })?;
            }
            let verifier = rustls::server::WebPkiClientVerifier::builder_with_provider(
                Arc::new(roots),
                provider,
            );
            let verifier = if settings.client_required {
                verifier
            } else {
                verifier.allow_unauthenticated()
            };
            let verifier = verifier
                .build()
                .map_err(|error| format!("tls_client_ca {path} cannot verify clients: {error}"))?;
            builder.with_client_cert_verifier(verifier)
        }
    };
    let mut config = builder
        .with_single_cert(certificates, key)
        .map_err(|error| format!("tls_cert and tls_key do not form a usable pair: {error}"))?;
    config.alpn_protocols = if settings.http2 {
        vec![b"h2".to_vec(), b"http/1.1".to_vec()]
    } else {
        vec![b"http/1.1".to_vec()]
    };
    Ok(Arc::new(config))
}

fn read_certificates(path: &str, name: &str) -> Result<Vec<CertificateDer<'static>>, String> {
    let certificates: Vec<CertificateDer<'static>> = CertificateDer::pem_file_iter(path)
        .map_err(|error| format!("cannot read {name} {path}: {error}"))?
        .collect::<Result<_, _>>()
        .map_err(|error| format!("invalid certificate in {name} {path}: {error}"))?;
    if certificates.is_empty() {
        return Err(format!("{name} {path} contains no certificates"));
    }
    Ok(certificates)
}

/// The verified client certificate (leaf, DER) of a TLS connection, if any.
pub(crate) fn client_certificate(connection: &rustls::ServerConnection) -> Option<Arc<[u8]>> {
    connection
        .peer_certificates()
        .and_then(|chain| chain.first())
        .map(|leaf| Arc::from(leaf.as_ref()))
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

    fn settings(cert: &str, key: &str, minimum: &str, http2: bool) -> Settings {
        Settings {
            cert: cert.to_owned(),
            key: key.to_owned(),
            minimum: minimum.to_owned(),
            http2,
            client_ca: None,
            client_required: true,
        }
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
        let error =
            server_config(&settings(missing.to_str().unwrap(), &key, "1.3", false)).unwrap_err();
        assert!(error.contains("tls_cert"), "{error}");
        let error = server_config(&settings(&cert, &cert, "1.3", false)).unwrap_err();
        assert!(error.contains("tls_key"), "{error}");
        let (_, _, other_key, _) = certificate_files("other");
        let error = server_config(&settings(&cert, &other_key, "1.3", false)).unwrap_err();
        assert!(error.contains("usable pair"), "{error}");
        assert!(!error.contains("PRIVATE KEY"), "{error}");
        assert!(server_config(&settings(&cert, &key, "1.3", false)).is_ok());
        let error = server_config(&settings(&cert, &key, "1.1", false)).unwrap_err();
        assert!(error.contains("tls_min"), "{error}");
    }

    async fn handshake(
        minimum: &str,
        client_versions: &[&'static rustls::SupportedProtocolVersion],
    ) -> Option<rustls::ProtocolVersion> {
        let (_directory, cert, key, ca) =
            certificate_files(&format!("min-{minimum}-{}", client_versions.len()));
        let acceptor = tokio_rustls::TlsAcceptor::from(
            server_config(&settings(&cert, &key, minimum, false)).unwrap(),
        );
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

    /// A CA, and a client certificate it signed, as (CA PEM, client chain, client key).
    fn client_identity() -> (String, Vec<CertificateDer<'static>>, PrivateKeyDer<'static>) {
        use rcgen::{BasicConstraints, CertificateParams, ExtendedKeyUsagePurpose, IsCa, KeyPair};
        let mut authority = CertificateParams::new(Vec::<String>::new()).unwrap();
        authority.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
        let authority_key = KeyPair::generate().unwrap();
        let authority = authority.self_signed(&authority_key).unwrap();
        let mut client = CertificateParams::new(vec!["client.test".to_owned()]).unwrap();
        client.extended_key_usages = vec![ExtendedKeyUsagePurpose::ClientAuth];
        let client_key = KeyPair::generate().unwrap();
        let client = client
            .signed_by(&client_key, &authority, &authority_key)
            .unwrap();
        (
            authority.pem(),
            vec![client.der().clone()],
            PrivateKeyDer::try_from(client_key.serialize_der()).unwrap(),
        )
    }

    /// The server's view of a handshake: `None` if it failed, otherwise the
    /// client certificate it verified.
    async fn mutual_handshake(
        required: bool,
        identity: Option<(Vec<CertificateDer<'static>>, PrivateKeyDer<'static>)>,
        client_ca: &str,
    ) -> Option<Option<Arc<[u8]>>> {
        let (directory, cert, key, ca) = certificate_files("mutual");
        let ca_file = directory.join(format!("client-ca-{required}.pem"));
        std::fs::write(&ca_file, client_ca).unwrap();
        let mut configured = settings(&cert, &key, "1.3", false);
        configured.client_ca = Some(ca_file.to_str().unwrap().to_owned());
        configured.client_required = required;
        let acceptor = tokio_rustls::TlsAcceptor::from(server_config(&configured).unwrap());
        let (client_end, server_end) = tokio::io::duplex(64 * 1024);
        let server = tokio::spawn(async move {
            let mut stream = acceptor.accept(server_end).await.ok()?;
            // Complete the exchange so the client sees the outcome too.
            stream.write_all(b"ok").await.ok()?;
            stream.shutdown().await.ok()?;
            Some(client_certificate(stream.get_ref().1))
        });
        let mut roots = rustls::RootCertStore::empty();
        roots
            .add(CertificateDer::from_pem_slice(ca.as_bytes()).unwrap())
            .unwrap();
        let builder = rustls::ClientConfig::builder_with_provider(Arc::new(
            rustls::crypto::ring::default_provider(),
        ))
        .with_safe_default_protocol_versions()
        .unwrap()
        .with_root_certificates(roots);
        let config = match identity {
            Some((chain, key)) => builder.with_client_auth_cert(chain, key).unwrap(),
            None => builder.with_no_client_auth(),
        };
        let name = rustls::pki_types::ServerName::try_from("localhost").unwrap();
        if let Ok(mut stream) = tokio_rustls::TlsConnector::from(Arc::new(config))
            .connect(name, client_end)
            .await
        {
            let mut sink = Vec::new();
            let _ = stream.read_to_end(&mut sink).await;
        }
        server.await.unwrap()
    }

    #[tokio::test]
    async fn client_certificates_are_verified_against_the_client_ca() {
        let (authority, chain, key) = client_identity();
        let leaf = chain[0].as_ref().to_vec();
        let identity = || Some((chain.clone(), key.clone_key()));
        // Required: a verified certificate is exposed, a missing one refused.
        let verified = mutual_handshake(true, identity(), &authority).await;
        assert_eq!(verified.flatten().as_deref(), Some(&leaf[..]));
        assert_eq!(mutual_handshake(true, None, &authority).await, None);
        // A certificate from another CA is refused even when optional.
        let (other_authority, _, _) = client_identity();
        assert_eq!(
            mutual_handshake(false, identity(), &other_authority).await,
            None
        );
        // Optional: no certificate is fine and exposes none.
        assert_eq!(mutual_handshake(false, None, &authority).await, Some(None));
        let optional = mutual_handshake(false, identity(), &authority).await;
        assert_eq!(optional.flatten().as_deref(), Some(&leaf[..]));
        // A bad CA file is reported at startup.
        let (directory, cert, key, _) = certificate_files("bad-ca");
        let mut configured = settings(&cert, &key, "1.3", false);
        configured.client_ca = Some(directory.join("missing.pem").to_str().unwrap().to_owned());
        let error = server_config(&configured).unwrap_err();
        assert!(error.contains("tls_client_ca"), "{error}");
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
        let config = server_config(&settings(&cert, &key, "1.3", true)).unwrap();
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
