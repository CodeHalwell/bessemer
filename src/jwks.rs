//! Remote JSON Web Key Sets.
//!
//! A dedicated thread fetches the key set over HTTPS (or plain HTTP to a
//! loopback host), parses it with the same strict JWK rules as static key sets
//! and publishes it atomically. Verification never waits on the network: it
//! reads the current set, and a token naming an unknown `kid` only asks the
//! refresher to fetch early, so a rotated signing key is picked up within
//! moments while unknown-kid floods cannot turn into a fetch storm.

use crate::jwt::{Algorithm, Key, jwk_keys};
use http_body_util::{BodyExt, Empty, Limited};
use hyper::{Request, StatusCode, body::Bytes};
use hyper_util::rt::TokioIo;
use serde_json::Value;
use std::{
    collections::HashSet,
    sync::{
        Arc, RwLock, Weak,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncRead, AsyncWrite},
    net::TcpStream,
    sync::Notify,
};

/// The largest key set document accepted from the network.
const MAX_DOCUMENT: usize = 1_048_576;
/// Connect, handshake and download must all finish within this.
const FETCH_TIMEOUT: Duration = Duration::from_secs(5);
/// Unknown-kid refetches are spaced at least this far apart.
const KID_MISS_INTERVAL: Duration = Duration::from_secs(5);

/// Where the key set lives.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Target {
    pub(crate) tls: bool,
    pub(crate) host: String,
    pub(crate) port: u16,
    /// Path and query, always starting with `/`.
    pub(crate) path: String,
}

impl Target {
    /// Parse an `https://` URL, or an `http://` URL whose host is loopback.
    pub(crate) fn parse(url: &str) -> Result<Self, String> {
        Self::parse_url(
            url,
            "jwt jwks_url must be an https URL (http only for localhost)",
            false,
        )
    }

    /// Parse an `https://` or `http://` URL; plain HTTP to a non-loopback
    /// host only when `http_anywhere` is set. `invalid` is the error message.
    pub(crate) fn parse_url(url: &str, invalid: &str, http_anywhere: bool) -> Result<Self, String> {
        if url.len() > 2048 || url.bytes().any(|byte| !byte.is_ascii_graphic()) {
            return Err(invalid.into());
        }
        let (tls, rest) = if let Some(rest) = url.strip_prefix("https://") {
            (true, rest)
        } else if let Some(rest) = url.strip_prefix("http://") {
            (false, rest)
        } else {
            return Err(invalid.into());
        };
        let split = rest.find(['/', '?', '#']).unwrap_or(rest.len());
        let (authority, tail) = rest.split_at(split);
        if authority.is_empty() || authority.contains('@') || tail.contains('#') {
            return Err(invalid.into());
        }
        let (host, port) = if let Some(bracketed) = authority.strip_prefix('[') {
            let (host, after) = bracketed.split_once(']').ok_or(invalid)?;
            let port = match after {
                "" => None,
                _ => Some(after.strip_prefix(':').ok_or(invalid)?),
            };
            (host.to_owned(), port)
        } else {
            match authority.rsplit_once(':') {
                Some((host, port)) => (host.to_owned(), Some(port)),
                None => (authority.to_owned(), None),
            }
        };
        let port = match port {
            None => {
                if tls {
                    443
                } else {
                    80
                }
            }
            Some(port) => port.parse().ok().filter(|port| *port > 0).ok_or(invalid)?,
        };
        if host.is_empty() {
            return Err(invalid.into());
        }
        let host = host.to_ascii_lowercase();
        let loopback = host == "localhost"
            || host
                .parse::<std::net::IpAddr>()
                .is_ok_and(|address| address.is_loopback());
        if !tls && !loopback && !http_anywhere {
            return Err(invalid.into());
        }
        let path = match tail {
            "" => "/".to_owned(),
            tail if tail.starts_with('?') => format!("/{tail}"),
            tail => tail.to_owned(),
        };
        Ok(Self {
            tls,
            host,
            port,
            path,
        })
    }

    pub(crate) fn authority(&self) -> String {
        let host = if self.host.contains(':') {
            format!("[{}]", self.host)
        } else {
            self.host.clone()
        };
        let default = if self.tls { 443 } else { 80 };
        if self.port == default {
            host
        } else {
            format!("{host}:{}", self.port)
        }
    }
}

/// A remote key set kept fresh by a background thread.
pub(crate) struct Remote {
    keys: RwLock<Arc<Vec<Key>>>,
    /// Bumped whenever the published key set changes, so cached verifications
    /// made with a retired key stop being served.
    generation: AtomicU64,
    wake: Notify,
    last_kid_miss: std::sync::Mutex<Option<Instant>>,
}

impl Remote {
    /// Start fetching. The first fetch is awaited up to the fetch timeout; if
    /// it fails the server still starts and the refresher keeps retrying.
    pub(crate) fn start(
        target: Target,
        ca_file: Option<String>,
        refresh: Duration,
        algorithms: HashSet<Algorithm>,
    ) -> Result<Arc<Self>, String> {
        let tls = if target.tls {
            Some(Arc::new(client_config(ca_file.as_deref())?))
        } else {
            if ca_file.is_some() {
                return Err("jwt jwks_ca_file requires an https jwks_url".into());
            }
            None
        };
        let remote = Arc::new(Self {
            keys: RwLock::new(Arc::new(Vec::new())),
            generation: AtomicU64::new(0),
            wake: Notify::new(),
            last_kid_miss: std::sync::Mutex::new(None),
        });
        let (first, ready) = std::sync::mpsc::channel();
        let weak = Arc::downgrade(&remote);
        std::thread::Builder::new()
            .name("bessemer-jwks".into())
            .spawn(move || {
                let runtime = match tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                {
                    Ok(runtime) => runtime,
                    Err(_) => {
                        let _ = first.send(Err("cannot start the JWKS refresher".into()));
                        return;
                    }
                };
                runtime.block_on(refresher(weak, target, tls, refresh, algorithms, first));
            })
            .map_err(|_| "cannot start the JWKS refresher")?;
        match ready.recv_timeout(FETCH_TIMEOUT + Duration::from_secs(1)) {
            Ok(Ok(())) => {}
            Ok(Err(error)) => {
                diagnostic!(
                    "Bessemer JWKS: initial fetch failed ({error}); retrying in the background"
                );
            }
            Err(_) => {
                diagnostic!("Bessemer JWKS: initial fetch timed out; retrying in the background")
            }
        }
        Ok(remote)
    }

    pub(crate) fn keys(&self) -> Arc<Vec<Key>> {
        self.keys.read().unwrap().clone()
    }

    pub(crate) fn generation(&self) -> u64 {
        self.generation.load(Ordering::Acquire)
    }

    /// A token named a key we do not have: fetch early, at most once per
    /// `KID_MISS_INTERVAL`.
    pub(crate) fn kid_miss(&self) {
        let now = Instant::now();
        let mut last = self.last_kid_miss.lock().unwrap();
        if last.is_some_and(|last| now.duration_since(last) < KID_MISS_INTERVAL) {
            return;
        }
        *last = Some(now);
        drop(last);
        self.wake.notify_one();
    }

    fn publish(&self, keys: Vec<Key>) {
        *self.keys.write().unwrap() = Arc::new(keys);
        self.generation.fetch_add(1, Ordering::AcqRel);
    }
}

fn client_config(ca_file: Option<&str>) -> Result<rustls::ClientConfig, String> {
    tls_client_config(ca_file, "jwt jwks_ca_file", "jwt jwks_url")
}

/// A TLS client configuration trusting `ca_file` (PEM) or the system roots.
/// `file_label` and `url_label` name the settings in error messages.
pub(crate) fn tls_client_config(
    ca_file: Option<&str>,
    file_label: &str,
    url_label: &str,
) -> Result<rustls::ClientConfig, String> {
    let mut roots = rustls::RootCertStore::empty();
    match ca_file {
        Some(path) => {
            use rustls::pki_types::{CertificateDer, pem::PemObject};
            for certificate in CertificateDer::pem_file_iter(path)
                .map_err(|_| format!("cannot read {file_label} {path}"))?
            {
                let certificate =
                    certificate.map_err(|_| format!("{file_label} {path} is not PEM"))?;
                roots
                    .add(certificate)
                    .map_err(|_| format!("{file_label} {path} has an invalid certificate"))?;
            }
            if roots.is_empty() {
                return Err(format!("{file_label} {path} contains no certificates"));
            }
        }
        None => {
            for certificate in rustls_native_certs::load_native_certs().certs {
                let _ = roots.add(certificate);
            }
            if roots.is_empty() {
                return Err(format!(
                    "no system CA certificates are available for {url_label}"
                ));
            }
        }
    }
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let mut config = rustls::ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .map_err(|_| format!("cannot configure TLS for {url_label}"))?
        .with_root_certificates(roots)
        .with_no_client_auth();
    config.alpn_protocols = vec![b"http/1.1".to_vec()];
    Ok(config)
}

async fn refresher(
    remote: Weak<Remote>,
    target: Target,
    tls: Option<Arc<rustls::ClientConfig>>,
    refresh: Duration,
    algorithms: HashSet<Algorithm>,
    first: std::sync::mpsc::Sender<Result<(), String>>,
) {
    let mut first = Some(first);
    let mut failures = 0u32;
    // The last published document: an unchanged set is not republished, so
    // routine refreshes keep the verified-token cache warm.
    let mut current: Option<Vec<u8>> = None;
    loop {
        let outcome = fetch(&target, tls.as_ref()).await.and_then(|body| {
            if current.as_ref() == Some(&body) {
                return Ok(None);
            }
            let document: Value = serde_json::from_slice(&body)
                .map_err(|_| "the JWKS is not valid JSON".to_owned())?;
            let keys = jwk_keys(&document, &algorithms)?;
            if keys.is_empty() {
                // A valid set with no usable key is published as such: the
                // issuer has revoked every key, so verification fails closed.
                diagnostic!(
                    "Bessemer JWKS: the key set has no signing key for the allowed algorithms; tokens will be rejected"
                );
            }
            Ok(Some((keys, body)))
        });
        let Some(strong) = remote.upgrade() else {
            return;
        };
        let wait = match outcome {
            Ok(update) => {
                if let Some((keys, body)) = update {
                    strong.publish(keys);
                    current = Some(body);
                }
                failures = 0;
                if let Some(first) = first.take() {
                    let _ = first.send(Ok(()));
                }
                refresh
            }
            Err(error) => {
                failures = failures.saturating_add(1);
                match first.take() {
                    Some(first) => {
                        let _ = first.send(Err(error));
                    }
                    None => diagnostic!(
                        "Bessemer JWKS: refresh failed ({error}); keeping the current keys"
                    ),
                }
                // Back off 1 s, 2 s, 4 s ... up to the refresh interval.
                Duration::from_secs(1u64 << failures.min(16).saturating_sub(1)).min(refresh)
            }
        };
        // A miss while fetching leaves a stored permit, so it is not lost.
        tokio::select! {
            () = tokio::time::sleep(wait) => {}
            () = strong.wake.notified() => {}
        }
        // Only this thread still refers to the key set: its owner is gone.
        if Arc::strong_count(&strong) == 1 {
            return;
        }
    }
}

async fn fetch(
    target: &Target,
    tls: Option<&Arc<rustls::ClientConfig>>,
) -> Result<Vec<u8>, String> {
    tokio::time::timeout(FETCH_TIMEOUT, async {
        let stream = TcpStream::connect((target.host.as_str(), target.port))
            .await
            .map_err(|error| format!("cannot connect: {error}"))?;
        let _ = stream.set_nodelay(true);
        match tls {
            Some(config) => {
                let name = rustls::pki_types::ServerName::try_from(target.host.clone())
                    .map_err(|_| "invalid TLS server name".to_owned())?;
                let stream = tokio_rustls::TlsConnector::from(config.clone())
                    .connect(name, stream)
                    .await
                    .map_err(|error| format!("TLS handshake failed: {error}"))?;
                exchange(stream, target).await
            }
            None => exchange(stream, target).await,
        }
    })
    .await
    .map_err(|_| "timed out".to_owned())?
}

async fn exchange<S>(stream: S, target: &Target) -> Result<Vec<u8>, String>
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let (mut sender, connection) = hyper::client::conn::http1::handshake(TokioIo::new(stream))
        .await
        .map_err(|error| format!("HTTP handshake failed: {error}"))?;
    let driver = tokio::spawn(connection);
    let request = Request::get(target.path.as_str())
        .header(hyper::header::HOST, target.authority())
        .header(hyper::header::ACCEPT, "application/json")
        .header(hyper::header::USER_AGENT, "bessemer-jwks")
        .header(hyper::header::CONNECTION, "close")
        .body(Empty::<Bytes>::new())
        .map_err(|_| "invalid JWKS request".to_owned())?;
    let result = async {
        let response = sender
            .send_request(request)
            .await
            .map_err(|error| format!("request failed: {error}"))?;
        if response.status() != StatusCode::OK {
            return Err(format!("unexpected status {}", response.status()));
        }
        let body = Limited::new(response.into_body(), MAX_DOCUMENT)
            .collect()
            .await
            .map_err(|_| "the JWKS response is too large or was cut off".to_owned())?;
        Ok(body.to_bytes().to_vec())
    }
    .await;
    driver.abort();
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[test]
    fn urls_require_https_except_on_loopback() {
        let target = Target::parse("https://login.example.com/tenant/keys?v=2").unwrap();
        assert_eq!(
            target,
            Target {
                tls: true,
                host: "login.example.com".into(),
                port: 443,
                path: "/tenant/keys?v=2".into(),
            }
        );
        assert_eq!(target.authority(), "login.example.com");
        let local = Target::parse("http://127.0.0.1:8080").unwrap();
        assert_eq!((local.port, local.path.as_str()), (8080, "/"));
        assert_eq!(local.authority(), "127.0.0.1:8080");
        let six = Target::parse("http://[::1]:9/k").unwrap();
        assert_eq!(
            (six.host.as_str(), six.authority()),
            ("::1", "[::1]:9".into())
        );
        assert!(Target::parse("http://localhost/keys").is_ok());
        for bad in [
            "http://login.example.com/keys",
            "ftp://x/keys",
            "https://user@x/keys",
            "https:///keys",
            "https://x:0/keys",
            "https://x:99999/keys",
            "https://x/keys#frag",
            "https://x/ke ys",
        ] {
            assert!(Target::parse(bad).is_err(), "{bad}");
        }
    }

    const RSA_JWKS: &str = include_str!("../tests/fixtures/jwt_vectors.json");

    fn jwks_document() -> String {
        let vectors: Value = serde_json::from_str(RSA_JWKS).unwrap();
        vectors["jwks"].to_string()
    }

    /// Serve `body` for every request on a loopback port, counting requests.
    async fn serve(body: String, hits: Arc<AtomicUsize>) -> u16 {
        serve_shared(Arc::new(std::sync::Mutex::new(body)), hits).await
    }

    /// Serve whatever `body` currently holds, so tests can rotate the set.
    async fn serve_shared(body: Arc<std::sync::Mutex<String>>, hits: Arc<AtomicUsize>) -> u16 {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            loop {
                let (mut stream, _) = listener.accept().await.unwrap();
                let body = body.lock().unwrap().clone();
                hits.fetch_add(1, Ordering::SeqCst);
                tokio::spawn(async move {
                    let mut buffer = [0u8; 4096];
                    let _ = stream.read(&mut buffer).await;
                    let response = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    );
                    let _ = stream.write_all(response.as_bytes()).await;
                });
            }
        });
        port
    }

    #[tokio::test]
    async fn plain_loopback_fetch_returns_the_document() {
        let hits = Arc::new(AtomicUsize::new(0));
        let document = jwks_document();
        let port = serve(document.clone(), hits.clone()).await;
        let target = Target::parse(&format!("http://127.0.0.1:{port}/keys")).unwrap();
        let body = fetch(&target, None).await.unwrap();
        assert_eq!(body, document.as_bytes());
        assert_eq!(hits.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn https_fetch_verifies_the_server_against_the_ca_file() {
        let certified = rcgen::generate_simple_self_signed(vec!["localhost".into()]).unwrap();
        let directory =
            std::env::temp_dir().join(format!("bessemer-jwks-tls-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let ca = directory.join("ca.pem");
        std::fs::write(&ca, certified.cert.pem()).unwrap();
        let server = rustls::ServerConfig::builder_with_provider(Arc::new(
            rustls::crypto::ring::default_provider(),
        ))
        .with_safe_default_protocol_versions()
        .unwrap()
        .with_no_client_auth()
        .with_single_cert(
            vec![certified.cert.der().clone()],
            rustls::pki_types::PrivateKeyDer::Pkcs8(certified.key_pair.serialize_der().into()),
        )
        .unwrap();
        let acceptor = tokio_rustls::TlsAcceptor::from(Arc::new(server));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let document = jwks_document();
        let served = document.clone();
        tokio::spawn(async move {
            loop {
                let (stream, _) = listener.accept().await.unwrap();
                let acceptor = acceptor.clone();
                let body = served.clone();
                tokio::spawn(async move {
                    let Ok(mut stream) = acceptor.accept(stream).await else {
                        return;
                    };
                    let mut buffer = [0u8; 4096];
                    let _ = stream.read(&mut buffer).await;
                    let response = format!(
                        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    );
                    let _ = stream.write_all(response.as_bytes()).await;
                    let _ = stream.shutdown().await;
                });
            }
        });
        let target = Target::parse(&format!("https://localhost:{port}/keys")).unwrap();
        let trusted = Arc::new(client_config(Some(ca.to_str().unwrap())).unwrap());
        assert_eq!(
            fetch(&target, Some(&trusted)).await.unwrap(),
            document.as_bytes()
        );
        // A client that does not trust this CA refuses the connection.
        let other = rcgen::generate_simple_self_signed(vec!["localhost".into()]).unwrap();
        let untrusted_ca = directory.join("other.pem");
        std::fs::write(&untrusted_ca, other.cert.pem()).unwrap();
        let untrusted = Arc::new(client_config(Some(untrusted_ca.to_str().unwrap())).unwrap());
        let error = fetch(&target, Some(&untrusted)).await.unwrap_err();
        assert!(error.contains("TLS"), "{error}");
        let _ = std::fs::remove_dir_all(&directory);
    }

    #[test]
    fn refresher_publishes_keys_and_refetches_on_kid_miss() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let hits = Arc::new(AtomicUsize::new(0));
        let port = runtime.block_on(serve(jwks_document(), hits.clone()));
        let target = Target::parse(&format!("http://127.0.0.1:{port}/keys")).unwrap();
        let algorithms: HashSet<_> = [Algorithm::RS256, Algorithm::ES256].into();
        let remote = Remote::start(target, None, Duration::from_secs(3600), algorithms).unwrap();
        assert!(!remote.keys().is_empty());
        let generation = remote.generation();
        assert_eq!(hits.load(Ordering::SeqCst), 1);
        remote.kid_miss();
        // A second miss inside the interval does not fetch again.
        remote.kid_miss();
        // The same document was served again, so nothing was republished.
        let deadline = Instant::now() + Duration::from_secs(5);
        while hits.load(Ordering::SeqCst) < 2 && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        std::thread::sleep(Duration::from_millis(100));
        assert_eq!(hits.load(Ordering::SeqCst), 2);
        assert_eq!(remote.generation(), generation);
    }

    #[test]
    fn an_empty_replacement_set_revokes_every_key() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let hits = Arc::new(AtomicUsize::new(0));
        let body = Arc::new(std::sync::Mutex::new(jwks_document()));
        let port = runtime.block_on(serve_shared(body.clone(), hits.clone()));
        let target = Target::parse(&format!("http://127.0.0.1:{port}/keys")).unwrap();
        let remote = Remote::start(
            target,
            None,
            Duration::from_secs(3600),
            [Algorithm::RS256, Algorithm::ES256].into(),
        )
        .unwrap();
        assert!(!remote.keys().is_empty());
        let generation = remote.generation();
        // The issuer removes its last key.
        *body.lock().unwrap() = r#"{"keys":[]}"#.to_owned();
        remote.kid_miss();
        let deadline = Instant::now() + Duration::from_secs(5);
        while remote.generation() == generation && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(remote.generation() > generation);
        assert!(remote.keys().is_empty());
    }

    #[test]
    fn an_unreachable_url_still_starts_with_no_keys() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let target = Target::parse(&format!("http://127.0.0.1:{port}/keys")).unwrap();
        let remote = Remote::start(
            target,
            None,
            Duration::from_secs(60),
            [Algorithm::RS256].into(),
        )
        .unwrap();
        assert!(remote.keys().is_empty());
        assert_eq!(remote.generation(), 0);
    }
}
