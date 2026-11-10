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
    /// PEM certificate revocation lists checked against client certificates.
    /// Every client certificate's issuer must have a current CRL here.
    pub(crate) client_crl: Option<String>,
    /// A DER OCSP response for `cert`, stapled to every handshake as it is.
    pub(crate) ocsp: Option<String>,
    /// Refuse clients that present no certificate; otherwise one is verified
    /// when presented but not required.
    pub(crate) client_required: bool,
    /// How often the files are checked for changes; zero never checks.
    pub(crate) reload_interval: std::time::Duration,
}

/// A TLS configuration that follows its files. New connections use the
/// current configuration; a check that finds the files changed builds a new
/// one and swaps it in, keeping the old one if the new files are not usable.
pub(crate) struct Reloadable {
    settings: Settings,
    current: std::sync::RwLock<Arc<rustls::ServerConfig>>,
    state: std::sync::Mutex<ReloadState>,
}

struct ReloadState {
    /// The digest of the files the current configuration was built from.
    applied: Vec<u8>,
    /// The last failure reported, so the same one is not logged every check.
    reported: Option<String>,
}

impl Reloadable {
    pub(crate) fn new(settings: Settings) -> Result<Arc<Self>, String> {
        let files = Files::read(&settings)?;
        let config = build(&settings, &files)?;
        Ok(Arc::new(Self {
            current: std::sync::RwLock::new(config),
            state: std::sync::Mutex::new(ReloadState {
                applied: files.fingerprint(),
                reported: None,
            }),
            settings,
        }))
    }

    pub(crate) fn current(&self) -> Arc<rustls::ServerConfig> {
        self.current
            .read()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }

    pub(crate) fn reload_interval(&self) -> std::time::Duration {
        self.settings.reload_interval
    }

    /// Re-read the files. `Ok(true)` means a new configuration is in use;
    /// `Ok(false)` that nothing changed. An error leaves the current
    /// configuration in place and is returned once; the check keeps retrying
    /// and reports again only when the failure changes.
    ///
    /// The digest and the configuration come from one read of the files, so
    /// a file that is briefly missing mid-rotation cannot mark contents as
    /// applied that were never loaded.
    pub(crate) fn reload_if_changed(&self) -> Result<bool, String> {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let failure = match Files::read(&self.settings) {
            Ok(files) => {
                let now = files.fingerprint();
                if now == state.applied {
                    state.reported = None;
                    return Ok(false);
                }
                match build(&self.settings, &files) {
                    Ok(config) => {
                        *self
                            .current
                            .write()
                            .unwrap_or_else(std::sync::PoisonError::into_inner) = config;
                        state.applied = now;
                        state.reported = None;
                        return Ok(true);
                    }
                    // The digest tells apart two different broken versions
                    // that fail with the same message.
                    Err(error) => (error, Some(now)),
                }
            }
            Err(error) => (error, None),
        };
        let key = match &failure.1 {
            Some(digest) => format!("{}\n{digest:?}", failure.0),
            None => failure.0.clone(),
        };
        if state.reported.as_ref() == Some(&key) {
            return Ok(false);
        }
        state.reported = Some(key);
        Err(failure.0)
    }
}

/// One read of every configured file.
struct Files {
    cert: Vec<u8>,
    key: Vec<u8>,
    client_ca: Option<Vec<u8>>,
    client_crl: Option<Vec<u8>>,
    ocsp: Option<Vec<u8>>,
}

impl Files {
    fn read(settings: &Settings) -> Result<Self, String> {
        let read = |name: &str, path: &str| {
            std::fs::read(path).map_err(|error| format!("cannot read {name} {path}: {error}"))
        };
        Ok(Self {
            cert: read("tls_cert", &settings.cert)?,
            key: read("tls_key", &settings.key)?,
            client_ca: match &settings.client_ca {
                Some(path) => Some(read("tls_client_ca", path)?),
                None => None,
            },
            client_crl: match &settings.client_crl {
                Some(path) => Some(read("tls_client_crl", path)?),
                None => None,
            },
            ocsp: match &settings.ocsp {
                Some(path) => Some(read("tls_ocsp_response", path)?),
                None => None,
            },
        })
    }

    /// A digest of every file's contents, so any change is noticed whatever
    /// the file system's timestamp resolution or how the files were
    /// replaced, without keeping a second copy of the private key.
    fn fingerprint(&self) -> Vec<u8> {
        let mut digest = ring::digest::Context::new(&ring::digest::SHA256);
        let files = [
            Some(&self.cert),
            Some(&self.key),
            self.client_ca.as_ref(),
            self.client_crl.as_ref(),
            self.ocsp.as_ref(),
        ];
        for bytes in files.into_iter().flatten() {
            digest.update(&(bytes.len() as u64).to_le_bytes());
            digest.update(bytes);
        }
        digest.finish().as_ref().to_vec()
    }
}

/// Build the server configuration from one read of the PEM files.
///
/// Errors name the file and the problem, never key material.
fn build(settings: &Settings, files: &Files) -> Result<Arc<rustls::ServerConfig>, String> {
    let versions: &[&'static rustls::SupportedProtocolVersion] = match settings.minimum.as_str() {
        "1.3" => &[&rustls::version::TLS13],
        "1.2" => &[&rustls::version::TLS13, &rustls::version::TLS12],
        _ => return Err("tls_min must be '1.2' or '1.3'".to_owned()),
    };
    let certificates = parse_certificates(&files.cert, &settings.cert, "tls_cert")?;
    let key = PrivateKeyDer::from_pem_slice(&files.key).map_err(|error| {
        format!(
            "cannot read a private key from tls_key {}: {error}",
            settings.key
        )
    })?;
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let builder = rustls::ServerConfig::builder_with_provider(provider.clone())
        .with_protocol_versions(versions)
        .map_err(|error| format!("TLS configuration failed: {error}"))?;
    let builder = match (&settings.client_ca, &files.client_ca) {
        (Some(path), Some(bundle)) => {
            let mut roots = rustls::RootCertStore::empty();
            for certificate in parse_certificates(bundle, path, "tls_client_ca")? {
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
            let verifier = match (&settings.client_crl, &files.client_crl) {
                (Some(path), Some(pem)) => {
                    let crls = parse_crls(pem, path)?;
                    // The leaf's issuer must have a CRL, and one past its
                    // next update is refused, as OpenSSL does; intermediates
                    // are trusted by being in tls_client_ca.
                    verifier
                        .with_crls(crls)
                        .only_check_end_entity_revocation()
                        .enforce_revocation_expiration()
                }
                _ => verifier,
            };
            let verifier = verifier
                .build()
                .map_err(|error| format!("tls_client_ca {path} cannot verify clients: {error}"))?;
            builder.with_client_cert_verifier(verifier)
        }
        _ => builder.with_no_client_auth(),
    };
    let ocsp = match (&settings.ocsp, &files.ocsp) {
        (Some(path), Some(response)) => {
            // rustls staples the bytes as they are. Checking the outer
            // DER SEQUENCE catches a PEM or empty file; freshness is up to
            // whatever refreshes the file, which reloading then picks up.
            if response.first() != Some(&0x30) || response.len() < 3 {
                return Err(format!(
                    "tls_ocsp_response {path} is not a DER OCSP response"
                ));
            }
            response.clone()
        }
        _ => Vec::new(),
    };
    let mut config = builder
        .with_single_cert_with_ocsp(certificates, key, ocsp)
        .map_err(|error| format!("tls_cert and tls_key do not form a usable pair: {error}"))?;
    config.alpn_protocols = if settings.http2 {
        vec![b"h2".to_vec(), b"http/1.1".to_vec()]
    } else {
        vec![b"http/1.1".to_vec()]
    };
    Ok(Arc::new(config))
}

fn parse_certificates(
    pem: &[u8],
    path: &str,
    name: &str,
) -> Result<Vec<CertificateDer<'static>>, String> {
    let certificates: Vec<CertificateDer<'static>> = CertificateDer::pem_slice_iter(pem)
        .collect::<Result<_, _>>()
        .map_err(|error| format!("invalid certificate in {name} {path}: {error}"))?;
    if certificates.is_empty() {
        return Err(format!("{name} {path} contains no certificates"));
    }
    Ok(certificates)
}

fn parse_crls(
    pem: &[u8],
    path: &str,
) -> Result<Vec<rustls::pki_types::CertificateRevocationListDer<'static>>, String> {
    let crls: Vec<_> = rustls::pki_types::CertificateRevocationListDer::pem_slice_iter(pem)
        .collect::<Result<_, _>>()
        .map_err(|error| format!("invalid CRL in tls_client_crl {path}: {error}"))?;
    if crls.is_empty() {
        return Err(format!("tls_client_crl {path} contains no CRLs"));
    }
    Ok(crls)
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

    fn server_config(settings: &Settings) -> Result<Arc<rustls::ServerConfig>, String> {
        build(settings, &Files::read(settings)?)
    }

    fn certificate_files(name: &str) -> (std::path::PathBuf, String, String, String) {
        let certified = rcgen::generate_simple_self_signed(vec!["localhost".into()]).unwrap();
        // Unique per call: tests run in parallel and some share a name.
        static CALLS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let call = CALLS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let directory =
            std::env::temp_dir().join(format!("bessemer-tls-{name}-{}-{call}", std::process::id()));
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
            client_crl: None,
            ocsp: None,
            client_required: true,
            reload_interval: std::time::Duration::ZERO,
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
        mutual_handshake_with(required, identity, client_ca, None).await
    }

    async fn mutual_handshake_with(
        required: bool,
        identity: Option<(Vec<CertificateDer<'static>>, PrivateKeyDer<'static>)>,
        client_ca: &str,
        client_crl: Option<&str>,
    ) -> Option<Option<Arc<[u8]>>> {
        let (directory, cert, key, ca) = certificate_files("mutual");
        let ca_file = directory.join(format!("client-ca-{required}.pem"));
        std::fs::write(&ca_file, client_ca).unwrap();
        let mut configured = settings(&cert, &key, "1.3", false);
        configured.client_ca = Some(ca_file.to_str().unwrap().to_owned());
        configured.client_required = required;
        if let Some(crl) = client_crl {
            let crl_file = directory.join(format!("client-crl-{required}.pem"));
            std::fs::write(&crl_file, crl).unwrap();
            configured.client_crl = Some(crl_file.to_str().unwrap().to_owned());
        }
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

    /// A CA that signs CRLs and a client certificate with serial 7.
    struct Revocation {
        authority: rcgen::Certificate,
        authority_key: rcgen::KeyPair,
        chain: Vec<CertificateDer<'static>>,
        key: PrivateKeyDer<'static>,
    }

    impl Revocation {
        fn new() -> Self {
            use rcgen::{
                BasicConstraints, CertificateParams, ExtendedKeyUsagePurpose, IsCa, KeyPair,
                KeyUsagePurpose, SerialNumber,
            };
            let mut authority = CertificateParams::new(Vec::<String>::new()).unwrap();
            authority.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
            authority.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
            let authority_key = KeyPair::generate().unwrap();
            let authority = authority.self_signed(&authority_key).unwrap();
            let mut client = CertificateParams::new(vec!["client.test".to_owned()]).unwrap();
            client.extended_key_usages = vec![ExtendedKeyUsagePurpose::ClientAuth];
            client.serial_number = Some(SerialNumber::from(7u64));
            let client_key = KeyPair::generate().unwrap();
            let client = client
                .signed_by(&client_key, &authority, &authority_key)
                .unwrap();
            Self {
                chain: vec![client.der().clone()],
                key: PrivateKeyDer::try_from(client_key.serialize_der()).unwrap(),
                authority,
                authority_key,
            }
        }

        fn identity(&self) -> Option<(Vec<CertificateDer<'static>>, PrivateKeyDer<'static>)> {
            Some((self.chain.clone(), self.key.clone_key()))
        }

        /// A CRL from this CA revoking `serials`, current until `next_year`.
        fn crl(&self, serials: &[u64], next_year: i32) -> String {
            use rcgen::{
                CertificateRevocationListParams, KeyIdMethod, RevokedCertParams, SerialNumber,
                date_time_ymd,
            };
            CertificateRevocationListParams {
                this_update: date_time_ymd(2020, 1, 1),
                next_update: date_time_ymd(next_year, 1, 1),
                crl_number: SerialNumber::from(1u64),
                issuing_distribution_point: None,
                revoked_certs: serials
                    .iter()
                    .map(|serial| RevokedCertParams {
                        serial_number: SerialNumber::from(*serial),
                        revocation_time: date_time_ymd(2020, 6, 1),
                        reason_code: None,
                        invalidity_date: None,
                    })
                    .collect(),
                key_identifier_method: KeyIdMethod::Sha256,
            }
            .signed_by(&self.authority, &self.authority_key)
            .unwrap()
            .pem()
            .unwrap()
        }
    }

    #[tokio::test]
    async fn revoked_client_certificates_are_refused() {
        let ca = Revocation::new();
        let authority = ca.authority.pem();
        let leaf = ca.chain[0].as_ref().to_vec();
        // A current CRL that does not list the client: accepted.
        let current = ca.crl(&[3], 2099);
        let accepted = mutual_handshake_with(true, ca.identity(), &authority, Some(&current)).await;
        assert_eq!(accepted.flatten().as_deref(), Some(&leaf[..]));
        // Listed as revoked: refused, even when certificates are optional.
        let revoked = ca.crl(&[3, 7], 2099);
        for required in [true, false] {
            let outcome =
                mutual_handshake_with(required, ca.identity(), &authority, Some(&revoked)).await;
            assert_eq!(outcome, None, "required={required}");
        }
        // A client without a certificate is unaffected in optional mode.
        assert_eq!(
            mutual_handshake_with(false, None, &authority, Some(&revoked)).await,
            Some(None)
        );
        // A CRL past its next update is not trusted.
        let expired = ca.crl(&[], 2021);
        assert_eq!(
            mutual_handshake_with(true, ca.identity(), &authority, Some(&expired)).await,
            None
        );
        // No CRL for the client's issuer: its status is unknown, so refused.
        let other = Revocation::new().crl(&[], 2099);
        assert_eq!(
            mutual_handshake_with(true, ca.identity(), &authority, Some(&other)).await,
            None
        );
        // Unusable CRL files are reported at startup.
        let (directory, cert, key, _) = certificate_files("bad-crl");
        let ca_file = directory.join("ca.pem");
        std::fs::write(&ca_file, &authority).unwrap();
        let crl_file = directory.join("crl.pem");
        std::fs::write(&crl_file, &authority).unwrap();
        let mut configured = settings(&cert, &key, "1.3", false);
        configured.client_ca = Some(ca_file.to_str().unwrap().to_owned());
        configured.client_crl = Some(crl_file.to_str().unwrap().to_owned());
        let error = server_config(&configured).unwrap_err();
        assert!(
            error.contains("tls_client_crl") && error.contains("no CRLs"),
            "{error}"
        );
        std::fs::write(
            &crl_file,
            "-----BEGIN X509 CRL-----\nnot base64!\n-----END X509 CRL-----\n",
        )
        .unwrap();
        let error = server_config(&configured).unwrap_err();
        assert!(error.contains("tls_client_crl"), "{error}");
    }

    /// Verifies normally and records the OCSP response the server stapled.
    #[derive(Debug)]
    struct Stapled {
        inner: Arc<rustls::client::WebPkiServerVerifier>,
        seen: Arc<std::sync::Mutex<Option<Vec<u8>>>>,
    }

    impl rustls::client::danger::ServerCertVerifier for Stapled {
        fn verify_server_cert(
            &self,
            end_entity: &CertificateDer<'_>,
            intermediates: &[CertificateDer<'_>],
            server_name: &rustls::pki_types::ServerName<'_>,
            ocsp_response: &[u8],
            now: rustls::pki_types::UnixTime,
        ) -> Result<rustls::client::danger::ServerCertVerified, rustls::Error> {
            *self.seen.lock().unwrap() = Some(ocsp_response.to_vec());
            self.inner
                .verify_server_cert(end_entity, intermediates, server_name, &[], now)
        }

        fn verify_tls12_signature(
            &self,
            message: &[u8],
            certificate: &CertificateDer<'_>,
            signature: &rustls::DigitallySignedStruct,
        ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
            self.inner
                .verify_tls12_signature(message, certificate, signature)
        }

        fn verify_tls13_signature(
            &self,
            message: &[u8],
            certificate: &CertificateDer<'_>,
            signature: &rustls::DigitallySignedStruct,
        ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
            self.inner
                .verify_tls13_signature(message, certificate, signature)
        }

        fn supported_verify_schemes(&self) -> Vec<rustls::SignatureScheme> {
            self.inner.supported_verify_schemes()
        }
    }

    /// The OCSP response a client is stapled during a handshake.
    async fn stapled(configured: &Settings, ca: &str) -> Vec<u8> {
        let acceptor = tokio_rustls::TlsAcceptor::from(server_config(configured).unwrap());
        let (client_end, server_end) = tokio::io::duplex(64 * 1024);
        let server = tokio::spawn(async move { acceptor.accept(server_end).await.is_ok() });
        let mut roots = rustls::RootCertStore::empty();
        roots
            .add(CertificateDer::from_pem_slice(ca.as_bytes()).unwrap())
            .unwrap();
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let seen = Arc::new(std::sync::Mutex::new(None));
        let verifier = Stapled {
            inner: rustls::client::WebPkiServerVerifier::builder_with_provider(
                Arc::new(roots),
                provider.clone(),
            )
            .build()
            .unwrap(),
            seen: seen.clone(),
        };
        let config = rustls::ClientConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .unwrap()
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(verifier))
            .with_no_client_auth();
        let name = rustls::pki_types::ServerName::try_from("localhost").unwrap();
        let _stream = tokio_rustls::TlsConnector::from(Arc::new(config))
            .connect(name, client_end)
            .await
            .unwrap();
        // The client stays open until the server has read its Finished.
        assert!(server.await.unwrap());
        seen.lock().unwrap().take().unwrap()
    }

    #[tokio::test]
    async fn an_ocsp_response_is_stapled_and_reloaded() {
        let (directory, cert, key, ca) = certificate_files("ocsp");
        let mut configured = settings(&cert, &key, "1.3", false);
        assert_eq!(stapled(&configured, &ca).await, b"");
        // Any DER SEQUENCE is stapled as it is.
        let response = directory.join("ocsp.der");
        std::fs::write(&response, [0x30, 0x03, 0x0a, 0x01, 0x00]).unwrap();
        configured.ocsp = Some(response.to_str().unwrap().to_owned());
        assert_eq!(
            stapled(&configured, &ca).await,
            [0x30, 0x03, 0x0a, 0x01, 0x00]
        );
        // A refreshed response is picked up by a reload.
        let reloadable = Reloadable::new(configured.clone()).unwrap();
        std::fs::write(&response, [0x30, 0x03, 0x0a, 0x01, 0x01]).unwrap();
        assert_eq!(reloadable.reload_if_changed(), Ok(true));
        // A PEM or empty file is refused rather than stapled.
        for bad in [&b""[..], b"-----BEGIN OCSP RESPONSE-----", b"\x30"] {
            std::fs::write(&response, bad).unwrap();
            let error = server_config(&configured).unwrap_err();
            assert!(error.contains("tls_ocsp_response"), "{error}");
        }
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

    #[test]
    fn a_failed_reload_is_reported_once_and_retried_until_it_succeeds() {
        let (_directory, cert, key, _) = certificate_files("retry-first");
        let (second, _, _, _) = certificate_files("retry-second");
        let config = Reloadable::new(settings(&cert, &key, "1.3", false)).unwrap();
        assert_eq!(config.reload_if_changed(), Ok(false));
        // The key vanishes mid-rotation: reported once, then retried quietly.
        let original_key = std::fs::read(&key).unwrap();
        std::fs::remove_file(&key).unwrap();
        let error = config.reload_if_changed().unwrap_err();
        assert!(error.contains("tls_key"), "{error}");
        assert_eq!(config.reload_if_changed(), Ok(false));
        // The certificate is replaced while the old key is still in place.
        std::fs::write(&key, &original_key).unwrap();
        std::fs::copy(second.join("cert.pem"), &cert).unwrap();
        let error = config.reload_if_changed().unwrap_err();
        assert!(error.contains("usable pair"), "{error}");
        assert_eq!(config.reload_if_changed(), Ok(false));
        // The matching key arrives: the new pair is loaded.
        std::fs::copy(second.join("key.pem"), &key).unwrap();
        assert_eq!(config.reload_if_changed(), Ok(true));
        assert_eq!(config.reload_if_changed(), Ok(false));
        // A file briefly missing that comes back unchanged is still not
        // treated as new, and a later failure is reported afresh.
        std::fs::remove_file(&cert).unwrap();
        assert!(config.reload_if_changed().is_err());
        std::fs::copy(second.join("cert.pem"), &cert).unwrap();
        assert_eq!(config.reload_if_changed(), Ok(false));
        std::fs::remove_file(&cert).unwrap();
        assert!(config.reload_if_changed().is_err());
    }

    #[tokio::test]
    async fn changed_certificate_files_are_picked_up_without_a_restart() {
        let (_first_directory, cert, key, first_ca) = certificate_files("reload-first");
        let (second_directory, _, _, second_ca) = certificate_files("reload-second");
        let mut configured = settings(&cert, &key, "1.3", false);
        configured.reload_interval = Duration::from_millis(100);
        let config = Reloadable::new(configured).unwrap();
        let app = Router::new().route("/", get(|| async { "ok" }));
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
            send_timeout: Duration::from_secs(30),
            min_send_rate: 0,
            max_requests: 0,
            http2: false,
        };
        let server = tokio::spawn(run(
            crate::transport::Listener::Epoll(listener),
            Application::Router(app),
            limits,
            None,
            Stop::Socket(server_end),
            Some(config),
        ));
        // The certificate a fresh connection is shown.
        let presented = || {
            let first_ca = first_ca.clone();
            let second_ca = second_ca.clone();
            async move {
                let mut roots = rustls::RootCertStore::empty();
                for pem in [&first_ca, &second_ca] {
                    roots
                        .add(CertificateDer::from_pem_slice(pem.as_bytes()).unwrap())
                        .unwrap();
                }
                let config = rustls::ClientConfig::builder_with_provider(Arc::new(
                    rustls::crypto::ring::default_provider(),
                ))
                .with_safe_default_protocol_versions()
                .unwrap()
                .with_root_certificates(roots)
                .with_no_client_auth();
                let tcp = tokio::net::TcpStream::connect(address).await.unwrap();
                let name = rustls::pki_types::ServerName::try_from("localhost").unwrap();
                let stream = tokio_rustls::TlsConnector::from(Arc::new(config))
                    .connect(name, tcp)
                    .await
                    .unwrap();
                let leaf = stream.get_ref().1.peer_certificates().unwrap()[0].clone();
                leaf.as_ref().to_vec()
            }
        };
        let der = |pem: &str| {
            CertificateDer::from_pem_slice(pem.as_bytes())
                .unwrap()
                .as_ref()
                .to_vec()
        };
        assert_eq!(presented().await, der(&first_ca));
        // Rotate both files in place, as a secret mount or certbot would.
        std::fs::copy(second_directory.join("cert.pem"), &cert).unwrap();
        std::fs::copy(second_directory.join("key.pem"), &key).unwrap();
        tokio::time::sleep(Duration::from_millis(400)).await;
        assert_eq!(presented().await, der(&second_ca));
        // A broken key is reported and the working certificate stays in use.
        std::fs::write(&key, "not a key").unwrap();
        tokio::time::sleep(Duration::from_millis(400)).await;
        assert_eq!(presented().await, der(&second_ca));
        drop(stop);
        server.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn requests_are_served_over_tls_and_marked_secure() {
        let (_directory, cert, key, ca) = certificate_files("serve");
        let config = Reloadable::new(settings(&cert, &key, "1.3", true)).unwrap();
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
            send_timeout: Duration::from_secs(30),
            min_send_rate: 0,
            max_requests: 0,
            http2: true,
        };
        let server = tokio::spawn(run(
            crate::transport::Listener::Epoll(listener),
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
