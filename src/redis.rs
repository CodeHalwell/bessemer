//! A shared rate limiter backed by Redis (or a RESP-compatible server such
//! as Valkey). It speaks just enough RESP2 over TCP or TLS for one Lua
//! script, through a bounded pool of connections. Every connect, command and
//! reply shares one deadline per request.

use axum::http::StatusCode;
use serde_json::{Map, Value};
use std::{
    net::IpAddr,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader},
    net::TcpStream,
    sync::Semaphore,
};

/// The longest reply line or bulk string accepted from the server.
const MAX_REPLY: usize = 64 * 1024;

/// A sliding-window log per key, timed by the Redis clock so every worker and
/// replica agrees. Members are request-unique; the score is the admission
/// time in microseconds. Returns 0 when admitted, otherwise the microseconds
/// until the oldest admission leaves the window.
const SCRIPT: &str = r"local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local time = redis.call('TIME')
local now = tonumber(time[1]) * 1000000 + tonumber(time[2])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
if redis.call('ZCARD', key) < limit then
  redis.call('ZADD', key, now, ARGV[3])
  redis.call('PEXPIRE', key, math.ceil(window / 1000))
  return 0
end
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
return math.max(1, tonumber(oldest[2]) + window - now)
";

/// Where the Redis server lives, parsed from a `redis://` or `rediss://` URL.
#[derive(Clone, PartialEq, Eq)]
pub(crate) struct Target {
    pub(crate) tls: bool,
    pub(crate) host: String,
    pub(crate) port: u16,
    username: Option<String>,
    password: Option<String>,
    db: u32,
}

impl std::fmt::Debug for Target {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Target")
            .field("tls", &self.tls)
            .field("host", &self.host)
            .field("port", &self.port)
            .field("username", &self.username)
            .field("password", &self.password.as_ref().map(|_| "<redacted>"))
            .field("db", &self.db)
            .finish()
    }
}

impl Target {
    /// Parse `redis[s]://[[user]:password@]host[:port][/db]`. Errors never
    /// repeat the URL, which can hold a password.
    pub(crate) fn parse(url: &str, invalid: &str) -> Result<Self, String> {
        if url.len() > 2048 || url.bytes().any(|byte| !byte.is_ascii_graphic()) {
            return Err(invalid.into());
        }
        let (tls, rest) = if let Some(rest) = url.strip_prefix("rediss://") {
            (true, rest)
        } else if let Some(rest) = url.strip_prefix("redis://") {
            (false, rest)
        } else {
            return Err(invalid.into());
        };
        if rest.contains(['?', '#']) {
            return Err(invalid.into());
        }
        let (authority, path) = rest.split_at(rest.find('/').unwrap_or(rest.len()));
        let (userinfo, hostport) = match authority.rsplit_once('@') {
            Some((userinfo, hostport)) => (Some(userinfo), hostport),
            None => (None, authority),
        };
        let decode = |value: &str| {
            percent_encoding::percent_decode_str(value)
                .decode_utf8()
                .map(|value| value.into_owned())
                .map_err(|_| invalid.to_owned())
        };
        let (username, password) = match userinfo {
            None => (None, None),
            Some(userinfo) => {
                // A bare `user@` is ambiguous between clients, so require `:`.
                let (user, password) = userinfo.split_once(':').ok_or(invalid)?;
                if password.is_empty() {
                    return Err(invalid.into());
                }
                let user = (!user.is_empty()).then(|| decode(user)).transpose()?;
                (user, Some(decode(password)?))
            }
        };
        let (host, port) = if let Some(bracketed) = hostport.strip_prefix('[') {
            let (host, after) = bracketed.split_once(']').ok_or(invalid)?;
            let port = match after {
                "" => None,
                _ => Some(after.strip_prefix(':').ok_or(invalid)?),
            };
            (host, port)
        } else {
            match hostport.rsplit_once(':') {
                Some((host, port)) => (host, Some(port)),
                None => (hostport, None),
            }
        };
        if host.is_empty() || host.contains(['@', '[', ']']) {
            return Err(invalid.into());
        }
        let port = match port {
            None => 6379,
            Some(port) => port.parse().ok().filter(|port| *port > 0).ok_or(invalid)?,
        };
        let db = match path {
            "" | "/" => 0,
            path => path[1..]
                .parse()
                .ok()
                .filter(|db: &u32| *db <= 1_000_000 && !path[1..].starts_with('+'))
                .ok_or(invalid)?,
        };
        Ok(Self {
            tls,
            host: host.to_ascii_lowercase(),
            port,
            username,
            password,
            db,
        })
    }
}

#[derive(Debug, PartialEq, Eq)]
enum Reply {
    Simple(String),
    Error(String),
    Integer(i64),
    Bulk(Option<Vec<u8>>),
}

trait Stream: AsyncRead + AsyncWrite + Send + Unpin {}
impl<T: AsyncRead + AsyncWrite + Send + Unpin> Stream for T {}

struct Connection {
    stream: BufReader<Box<dyn Stream>>,
    line: Vec<u8>,
}

impl Connection {
    async fn command(&mut self, arguments: &[&[u8]]) -> Result<Reply, String> {
        let mut frame = Vec::with_capacity(64);
        frame.push(b'*');
        frame.extend_from_slice(arguments.len().to_string().as_bytes());
        frame.extend_from_slice(b"\r\n");
        for argument in arguments {
            frame.push(b'$');
            frame.extend_from_slice(argument.len().to_string().as_bytes());
            frame.extend_from_slice(b"\r\n");
            frame.extend_from_slice(argument);
            frame.extend_from_slice(b"\r\n");
        }
        let stream = self.stream.get_mut();
        stream.write_all(&frame).await.map_err(io_error)?;
        stream.flush().await.map_err(io_error)?;
        self.reply().await
    }

    async fn read_line(&mut self) -> Result<(), String> {
        self.line.clear();
        loop {
            let available = self.stream.fill_buf().await.map_err(io_error)?;
            if available.is_empty() {
                return Err("the connection closed".into());
            }
            let (taken, done) = match memchr::memchr(b'\n', available) {
                Some(index) => (index + 1, true),
                None => (available.len(), false),
            };
            self.line.extend_from_slice(&available[..taken]);
            self.stream.consume(taken);
            if self.line.len() > MAX_REPLY + 2 {
                return Err("a reply is too large".into());
            }
            if done {
                break;
            }
        }
        if !self.line.ends_with(b"\r\n") || self.line.len() < 3 {
            return Err("a reply is malformed".into());
        }
        self.line.truncate(self.line.len() - 2);
        Ok(())
    }

    async fn reply(&mut self) -> Result<Reply, String> {
        self.read_line().await?;
        let text = || String::from_utf8_lossy(&self.line[1..]).into_owned();
        let number = || {
            std::str::from_utf8(&self.line[1..])
                .ok()
                .and_then(|value| value.parse::<i64>().ok())
                .ok_or_else(|| "a reply is malformed".to_owned())
        };
        Ok(match self.line[0] {
            b'+' => Reply::Simple(text()),
            b'-' => Reply::Error(text()),
            b':' => Reply::Integer(number()?),
            b'$' => {
                let length = number()?;
                if length == -1 {
                    return Ok(Reply::Bulk(None));
                }
                let length = usize::try_from(length)
                    .ok()
                    .filter(|length| *length <= MAX_REPLY)
                    .ok_or("a reply is malformed")?;
                let mut body = vec![0; length + 2];
                tokio::io::AsyncReadExt::read_exact(&mut self.stream, &mut body)
                    .await
                    .map_err(io_error)?;
                if !body.ends_with(b"\r\n") {
                    return Err("a reply is malformed".into());
                }
                body.truncate(length);
                Reply::Bulk(Some(body))
            }
            _ => return Err("the server sent an unexpected reply".into()),
        })
    }
}

fn io_error(error: std::io::Error) -> String {
    format!("I/O failed: {error}")
}

/// A bounded pool of connections to one server.
struct Client {
    target: Target,
    tls: Option<Arc<rustls::ClientConfig>>,
    idle: Mutex<Vec<Connection>>,
    permits: Semaphore,
}

impl Client {
    async fn connect(&self) -> Result<Connection, String> {
        let tcp = TcpStream::connect((self.target.host.as_str(), self.target.port))
            .await
            .map_err(|error| format!("cannot connect: {error}"))?;
        let _ = tcp.set_nodelay(true);
        let stream: Box<dyn Stream> = match &self.tls {
            Some(config) => {
                let name = rustls::pki_types::ServerName::try_from(self.target.host.clone())
                    .map_err(|_| "invalid TLS server name".to_owned())?;
                Box::new(
                    tokio_rustls::TlsConnector::from(config.clone())
                        .connect(name, tcp)
                        .await
                        .map_err(|error| format!("TLS handshake failed: {error}"))?,
                )
            }
            None => Box::new(tcp),
        };
        let mut connection = Connection {
            stream: BufReader::new(stream),
            line: Vec::new(),
        };
        if let Some(password) = &self.target.password {
            let reply = match &self.target.username {
                Some(user) => {
                    connection
                        .command(&[b"AUTH", user.as_bytes(), password.as_bytes()])
                        .await?
                }
                None => connection.command(&[b"AUTH", password.as_bytes()]).await?,
            };
            expect_ok(reply, "AUTH")?;
        }
        if self.target.db != 0 {
            let db = self.target.db.to_string();
            expect_ok(
                connection.command(&[b"SELECT", db.as_bytes()]).await?,
                "SELECT",
            )?;
        }
        Ok(connection)
    }

    /// Run the script, loading it on a `NOSCRIPT` reply. The connection goes
    /// back to the pool only after a complete reply.
    async fn run(
        &self,
        connection: &mut Connection,
        sha: &[u8],
        arguments: &[&[u8]],
    ) -> Result<Reply, String> {
        let mut command: Vec<&[u8]> = vec![b"EVALSHA", sha, b"1"];
        command.extend_from_slice(arguments);
        let reply = connection.command(&command).await?;
        if let Reply::Error(message) = &reply
            && message.starts_with("NOSCRIPT")
        {
            command[0] = b"EVAL";
            command[1] = SCRIPT.as_bytes();
            return connection.command(&command).await;
        }
        Ok(reply)
    }

    async fn eval(&self, sha: &[u8], arguments: &[&[u8]]) -> Result<Reply, String> {
        let _permit = self
            .permits
            .acquire()
            .await
            .map_err(|_| "the pool is closed".to_owned())?;
        let pooled = self.idle.lock().unwrap().pop();
        let (mut connection, reused) = match pooled {
            Some(connection) => (connection, true),
            None => (self.connect().await?, false),
        };
        let reply = match self.run(&mut connection, sha, arguments).await {
            Ok(reply) => reply,
            // The server may have closed an idle connection; try once more on
            // a fresh one.
            Err(_) if reused => {
                connection = self.connect().await?;
                self.run(&mut connection, sha, arguments).await?
            }
            Err(error) => return Err(error),
        };
        self.idle.lock().unwrap().push(connection);
        Ok(reply)
    }
}

fn expect_ok(reply: Reply, command: &str) -> Result<(), String> {
    match reply {
        Reply::Simple(value) if value == "OK" => Ok(()),
        Reply::Error(message) => Err(format!(
            "{command} failed: {}",
            message.chars().take(200).collect::<String>()
        )),
        _ => Err(format!("{command} failed")),
    }
}

/// Rate limiting decisions shared through Redis.
pub(crate) struct SharedLimiter {
    client: Client,
    sha: String,
    prefix: String,
    timeout: Duration,
    fail_open: bool,
    instance: u64,
    sequence: AtomicU64,
    /// Decisions the store could not make, for the metrics endpoint.
    pub(crate) errors: Arc<AtomicU64>,
    last_report: Mutex<Option<std::time::Instant>>,
    /// Set while a report is being written.
    reporting: Arc<AtomicBool>,
}

pub(crate) enum Decision {
    Admit,
    Limited(u64),
    Unavailable,
}

impl SharedLimiter {
    /// Settings: `url_env` (required), `key_prefix`, `timeout_seconds`,
    /// `pool_size`, `ca_file` and `fail_open`.
    /// `env` looks up the environment variable holding the URL.
    pub(crate) fn parse(
        config: &Map<String, Value>,
        env: impl Fn(&str) -> Option<String>,
    ) -> Result<Self, String> {
        if config.keys().any(|key| {
            ![
                "url_env",
                "key_prefix",
                "timeout_seconds",
                "pool_size",
                "ca_file",
                "fail_open",
            ]
            .contains(&key.as_str())
        }) {
            return Err("rate_limit redis contains unsupported settings".into());
        }
        let name = config
            .get("url_env")
            .and_then(Value::as_str)
            .ok_or("rate_limit redis url_env is required")?;
        let mut characters = name.chars();
        if name.len() > 128
            || !characters
                .next()
                .is_some_and(|value| value == '_' || value.is_ascii_alphabetic())
            || !characters.all(|value| value == '_' || value.is_ascii_alphanumeric())
        {
            return Err("rate_limit redis url_env must be an environment variable name".into());
        }
        let url = env(name).ok_or_else(|| {
            format!("rate_limit redis environment variable {name} is unavailable")
        })?;
        let target = Target::parse(
            &url,
            &format!(
                "rate_limit redis environment variable {name} must hold a redis:// or rediss:// URL"
            ),
        )?;
        let prefix = match config.get("key_prefix") {
            None => "bessemer:rate:".to_owned(),
            Some(Value::String(prefix))
                if (1..=128).contains(&prefix.len())
                    && prefix.bytes().all(|byte| byte.is_ascii_graphic()) =>
            {
                prefix.clone()
            }
            _ => {
                return Err(
                    "rate_limit redis key_prefix must have 1 to 128 visible ASCII characters"
                        .into(),
                );
            }
        };
        let timeout = match config.get("timeout_seconds") {
            None => 0.25,
            Some(value) => value
                .as_f64()
                .filter(|seconds| seconds.is_finite() && (0.001..=10.0).contains(seconds))
                .ok_or("rate_limit redis timeout_seconds must be between 0.001 and 10")?,
        };
        let pool_size = match config.get("pool_size") {
            None => 16,
            Some(value) => value
                .as_u64()
                .filter(|size| (1..=1024).contains(size))
                .ok_or("rate_limit redis pool_size must be an integer between 1 and 1024")?
                as usize,
        };
        let fail_open = match config.get("fail_open") {
            None => false,
            Some(Value::Bool(value)) => *value,
            _ => return Err("rate_limit redis fail_open must be a bool".into()),
        };
        let ca_file = match config.get("ca_file") {
            None | Some(Value::Null) => None,
            Some(Value::String(path)) if !path.is_empty() => Some(path.as_str()),
            _ => return Err("rate_limit redis ca_file must be a path".into()),
        };
        let tls = if target.tls {
            let mut config = crate::jwks::tls_client_config(
                ca_file,
                "rate_limit redis ca_file",
                "rate_limit redis",
            )?;
            config.alpn_protocols.clear();
            Some(Arc::new(config))
        } else if ca_file.is_some() {
            return Err("rate_limit redis ca_file needs a rediss:// URL".into());
        } else {
            None
        };
        let digest =
            ring::digest::digest(&ring::digest::SHA1_FOR_LEGACY_USE_ONLY, SCRIPT.as_bytes());
        let sha = digest
            .as_ref()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        let mut instance = [0; 8];
        ring::rand::SecureRandom::fill(&ring::rand::SystemRandom::new(), &mut instance)
            .map_err(|_| "cannot seed the rate limiter".to_owned())?;
        Ok(Self {
            client: Client {
                target,
                tls,
                idle: Mutex::new(Vec::with_capacity(pool_size)),
                permits: Semaphore::new(pool_size),
            },
            sha,
            prefix,
            timeout: Duration::from_secs_f64(timeout),
            fail_open,
            instance: u64::from_ne_bytes(instance),
            sequence: AtomicU64::new(0),
            errors: Arc::new(AtomicU64::new(0)),
            last_report: Mutex::new(None),
            reporting: Arc::new(AtomicBool::new(false)),
        })
    }

    pub(crate) async fn admit(&self, ip: IpAddr, requests: usize, window: Duration) -> Decision {
        let key = format!("{}{ip}", self.prefix);
        let member = format!(
            "{:016x}-{:x}",
            self.instance,
            self.sequence.fetch_add(1, Ordering::Relaxed)
        );
        let limit = requests.to_string();
        let micros = window.as_micros().max(1).to_string();
        let result = tokio::time::timeout(
            self.timeout,
            self.client.eval(
                self.sha.as_bytes(),
                &[
                    key.as_bytes(),
                    limit.as_bytes(),
                    micros.as_bytes(),
                    member.as_bytes(),
                ],
            ),
        )
        .await
        .unwrap_or_else(|_| Err("timed out".into()));
        let error = match result {
            Ok(Reply::Integer(0)) => return Decision::Admit,
            Ok(Reply::Integer(wait)) if wait > 0 => {
                return Decision::Limited((wait as u64).div_ceil(1_000_000).max(1));
            }
            Ok(Reply::Error(message)) => format!(
                "the server returned an error: {}",
                message.chars().take(200).collect::<String>()
            ),
            Ok(_) => "the server sent an unexpected reply".to_owned(),
            Err(error) => error,
        };
        self.errors.fetch_add(1, Ordering::Relaxed);
        self.report(&error);
        if self.fail_open {
            Decision::Admit
        } else {
            Decision::Unavailable
        }
    }

    /// Report a store failure on stderr at most once a minute. The write runs
    /// on a blocking thread, at most one at a time, so a stalled stderr never
    /// holds a request or a Tokio worker.
    fn report(&self, error: &str) {
        let now = std::time::Instant::now();
        let mut last = self.last_report.lock().unwrap();
        if last.is_some_and(|last| now.duration_since(last) < Duration::from_secs(60)) {
            return;
        }
        *last = Some(now);
        drop(last);
        if self.reporting.swap(true, Ordering::AcqRel) {
            return;
        }
        let message = format!(
            "bessemer: the Redis rate limiter at {}:{} failed ({error}); requests are {}",
            self.client.target.host,
            self.client.target.port,
            if self.fail_open {
                "admitted"
            } else {
                "rejected with 503"
            }
        );
        let reporting = self.reporting.clone();
        tokio::task::spawn_blocking(move || {
            eprintln!("{message}");
            reporting.store(false, Ordering::Release);
        });
    }
}

/// The status and `Retry-After` a decision maps to, if it rejects.
pub(crate) fn rejection(decision: Decision) -> Option<(StatusCode, u64)> {
    match decision {
        Decision::Admit => None,
        Decision::Limited(retry) => Some((StatusCode::TOO_MANY_REQUESTS, retry)),
        Decision::Unavailable => Some((StatusCode::SERVICE_UNAVAILABLE, 1)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::{io::AsyncReadExt, net::TcpListener};

    const INVALID: &str = "invalid";

    #[test]
    fn urls_parse_credentials_hosts_ports_and_databases() {
        let target = Target::parse("rediss://app:p%40ss@Cache.Example:6380/2", INVALID).unwrap();
        assert_eq!(
            target,
            Target {
                tls: true,
                host: "cache.example".into(),
                port: 6380,
                username: Some("app".into()),
                password: Some("p@ss".into()),
                db: 2,
            }
        );
        assert!(!format!("{target:?}").contains("p@ss"));
        let target = Target::parse("redis://:secret@[::1]", INVALID).unwrap();
        assert_eq!(
            (
                target.host.as_str(),
                target.port,
                target.username,
                target.db
            ),
            ("::1", 6379, None, 0)
        );
        assert_eq!(target.password.as_deref(), Some("secret"));
        for url in [
            "http://cache",
            "redis://",
            "redis://user@cache",
            "redis://user:@cache",
            "redis://cache:0",
            "redis://cache/x",
            "redis://cache/+1",
            "redis://cache?db=1",
            "redis://cache/1#x",
            "redis://[::1",
            "redis://ca che",
        ] {
            assert_eq!(Target::parse(url, INVALID), Err(INVALID.into()), "{url}");
        }
    }

    /// A scripted RESP server: each connection reads commands and answers
    /// with the next canned reply.
    async fn server(replies: Vec<&'static str>) -> (u16, Arc<Mutex<Vec<String>>>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let seen = Arc::new(Mutex::new(Vec::new()));
        let log = seen.clone();
        tokio::spawn(async move {
            let mut replies = replies.into_iter();
            loop {
                let (mut socket, _) = listener.accept().await.unwrap();
                let mut buffer = vec![0; 65536];
                while let Ok(read) = socket.read(&mut buffer).await {
                    if read == 0 {
                        break;
                    }
                    let text = String::from_utf8_lossy(&buffer[..read]).into_owned();
                    let name = text.split("\r\n").nth(2).unwrap_or_default().to_owned();
                    log.lock().unwrap().push(name);
                    match replies.next() {
                        Some("close") => break,
                        Some("hang") => tokio::time::sleep(Duration::from_secs(60)).await,
                        Some(reply) => socket.write_all(reply.as_bytes()).await.unwrap(),
                        None => break,
                    }
                }
            }
        });
        (port, seen)
    }

    fn limiter(port: u16, extra: &str) -> SharedLimiter {
        let config: Value = serde_json::from_str(&format!(
            r#"{{"url_env":"REDIS_URL","timeout_seconds":0.5{extra}}}"#
        ))
        .unwrap();
        let url = format!("redis://:secret@127.0.0.1:{port}/3");
        SharedLimiter::parse(config.as_object().unwrap(), |_| Some(url.clone())).unwrap()
    }

    #[tokio::test]
    async fn the_client_authenticates_selects_and_loads_the_script_once() {
        let (port, seen) = server(vec![
            "+OK\r\n",
            "+OK\r\n",
            "-NOSCRIPT No matching script.\r\n",
            ":0\r\n",
            ":2500000\r\n",
        ])
        .await;
        let limiter = limiter(port, "");
        let ip: IpAddr = "192.0.2.1".parse().unwrap();
        let window = Duration::from_secs(10);
        assert!(matches!(
            limiter.admit(ip, 1, window).await,
            Decision::Admit
        ));
        assert!(matches!(
            limiter.admit(ip, 1, window).await,
            Decision::Limited(3)
        ));
        assert_eq!(
            *seen.lock().unwrap(),
            ["AUTH", "SELECT", "EVALSHA", "EVAL", "EVALSHA"]
        );
        assert_eq!(limiter.errors.load(Ordering::Relaxed), 0);
    }

    #[tokio::test]
    async fn store_failures_fail_closed_or_open_and_are_counted() {
        let (port, _) = server(vec!["+OK\r\n", "+OK\r\n", "hang"]).await;
        let closed = limiter(port, "");
        let ip: IpAddr = "192.0.2.1".parse().unwrap();
        let started = std::time::Instant::now();
        assert!(matches!(
            closed.admit(ip, 1, Duration::from_secs(1)).await,
            Decision::Unavailable
        ));
        assert!(started.elapsed() < Duration::from_secs(5));
        assert_eq!(closed.errors.load(Ordering::Relaxed), 1);

        let (port, _) = server(vec!["-WRONGPASS invalid username-password pair\r\n"]).await;
        let open = limiter(port, r#","fail_open":true"#);
        assert!(matches!(
            open.admit(ip, 1, Duration::from_secs(1)).await,
            Decision::Admit
        ));
        assert_eq!(open.errors.load(Ordering::Relaxed), 1);
    }

    #[tokio::test]
    async fn a_closed_idle_connection_is_replaced() {
        let (port, seen) = server(vec![
            "+OK\r\n", "+OK\r\n", ":0\r\n", "close", "+OK\r\n", "+OK\r\n", ":0\r\n",
        ])
        .await;
        let limiter = limiter(port, "");
        let ip: IpAddr = "192.0.2.1".parse().unwrap();
        for _ in 0..2 {
            assert!(matches!(
                limiter.admit(ip, 5, Duration::from_secs(1)).await,
                Decision::Admit
            ));
        }
        assert_eq!(seen.lock().unwrap().len(), 7);
    }

    #[tokio::test]
    async fn rediss_verifies_the_server_against_the_ca_file() {
        let certified = rcgen::generate_simple_self_signed(vec!["localhost".into()]).unwrap();
        let directory =
            std::env::temp_dir().join(format!("bessemer-redis-tls-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let ca = directory.join("ca.pem");
        std::fs::write(&ca, certified.cert.pem()).unwrap();
        let other = rcgen::generate_simple_self_signed(vec!["localhost".into()]).unwrap();
        let untrusted = directory.join("other.pem");
        std::fs::write(&untrusted, other.cert.pem()).unwrap();
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
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            loop {
                let (stream, _) = listener.accept().await.unwrap();
                let acceptor = acceptor.clone();
                tokio::spawn(async move {
                    let Ok(mut stream) = acceptor.accept(stream).await else {
                        return;
                    };
                    let mut buffer = [0u8; 4096];
                    while let Ok(read) = stream.read(&mut buffer).await {
                        if read == 0 {
                            break;
                        }
                        let _ = stream.write_all(b":0\r\n").await;
                    }
                });
            }
        });
        let url = format!("rediss://localhost:{port}");
        let limiter = |ca: &std::path::Path| {
            let config = serde_json::json!({"url_env":"URL","ca_file":ca.to_str().unwrap()});
            SharedLimiter::parse(config.as_object().unwrap(), |_| Some(url.clone())).unwrap()
        };
        let ip: IpAddr = "192.0.2.1".parse().unwrap();
        let trusted = limiter(&ca);
        assert!(matches!(
            trusted.admit(ip, 1, Duration::from_secs(1)).await,
            Decision::Admit
        ));
        assert_eq!(trusted.errors.load(Ordering::Relaxed), 0);
        let refused = limiter(&untrusted);
        assert!(matches!(
            refused.admit(ip, 1, Duration::from_secs(1)).await,
            Decision::Unavailable
        ));
        let _ = std::fs::remove_dir_all(&directory);
    }

    #[test]
    fn settings_are_validated() {
        let parse = |value: Value| {
            SharedLimiter::parse(value.as_object().unwrap(), |name| {
                (name == "BESSEMER_TEST_REDIS_PLAIN").then(|| "redis://127.0.0.1".to_owned())
            })
            .map(|_| ())
        };
        for (config, message) in [
            (serde_json::json!({}), "url_env is required"),
            (
                serde_json::json!({"url_env":"BESSEMER_TEST_REDIS_MISSING"}),
                "is unavailable",
            ),
            (
                serde_json::json!({"url_env":"BESSEMER_TEST_REDIS_PLAIN","extra":1}),
                "unsupported",
            ),
            (
                serde_json::json!({"url_env":"BESSEMER_TEST_REDIS_PLAIN","pool_size":0}),
                "pool_size",
            ),
            (
                serde_json::json!({"url_env":"BESSEMER_TEST_REDIS_PLAIN","timeout_seconds":11}),
                "timeout_seconds",
            ),
            (
                serde_json::json!({"url_env":"BESSEMER_TEST_REDIS_PLAIN","key_prefix":""}),
                "key_prefix",
            ),
            (
                serde_json::json!({"url_env":"BESSEMER_TEST_REDIS_PLAIN","ca_file":"/ca.pem"}),
                "rediss://",
            ),
        ] {
            let error = parse(config).err().unwrap();
            assert!(error.contains(message), "{error}");
        }
        assert!(parse(serde_json::json!({"url_env":"BESSEMER_TEST_REDIS_PLAIN"})).is_ok());
    }
}
