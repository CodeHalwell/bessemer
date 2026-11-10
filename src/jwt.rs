//! Native JWT bearer verification.
//!
//! Token framing, base64url, header/claim validation and the verified-token
//! cache are implemented here; only the signature primitives come from `ring`.
//! Every key is bound to an explicit algorithm allow-list, `alg: none` and
//! critical header extensions are rejected, and time claims use a bounded leeway.

use ring::{hmac, signature};
use serde_json::{Map, Value};
use std::{
    collections::{HashMap, HashSet},
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};

type Config = Map<String, Value>;

/// Tokens larger than this are rejected before any decoding.
pub const MAX_TOKEN_BYTES: usize = 8192;
const MAX_CACHE_SECONDS: f64 = 300.0;
const MAX_KIDLESS_KEYS: usize = 8;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Algorithm {
    HS256,
    HS384,
    HS512,
    RS256,
    RS384,
    RS512,
    PS256,
    PS384,
    PS512,
    ES256,
    ES384,
    EdDSA,
}

impl Algorithm {
    fn parse(value: &str) -> Option<Self> {
        Some(match value {
            "HS256" => Self::HS256,
            "HS384" => Self::HS384,
            "HS512" => Self::HS512,
            "RS256" => Self::RS256,
            "RS384" => Self::RS384,
            "RS512" => Self::RS512,
            "PS256" => Self::PS256,
            "PS384" => Self::PS384,
            "PS512" => Self::PS512,
            "ES256" => Self::ES256,
            "ES384" => Self::ES384,
            "EdDSA" => Self::EdDSA,
            _ => return None,
        })
    }

    fn hmac(self) -> Option<hmac::Algorithm> {
        match self {
            Self::HS256 => Some(hmac::HMAC_SHA256),
            Self::HS384 => Some(hmac::HMAC_SHA384),
            Self::HS512 => Some(hmac::HMAC_SHA512),
            _ => None,
        }
    }

    fn rsa(self) -> Option<&'static signature::RsaParameters> {
        Some(match self {
            Self::RS256 => &signature::RSA_PKCS1_2048_8192_SHA256,
            Self::RS384 => &signature::RSA_PKCS1_2048_8192_SHA384,
            Self::RS512 => &signature::RSA_PKCS1_2048_8192_SHA512,
            Self::PS256 => &signature::RSA_PSS_2048_8192_SHA256,
            Self::PS384 => &signature::RSA_PSS_2048_8192_SHA384,
            Self::PS512 => &signature::RSA_PSS_2048_8192_SHA512,
            _ => return None,
        })
    }
}

enum Material {
    Hmac(hmac::Key),
    Rsa { n: Vec<u8>, e: Vec<u8> },
    Point(&'static signature::EcdsaVerificationAlgorithm, Vec<u8>),
    Ed25519(Vec<u8>),
}

pub(crate) struct Key {
    kid: Option<String>,
    algorithms: Vec<Algorithm>,
    material: Material,
}

impl Key {
    fn verify(&self, algorithm: Algorithm, message: &[u8], signature_bytes: &[u8]) -> bool {
        if !self.algorithms.contains(&algorithm) {
            return false;
        }
        match &self.material {
            Material::Hmac(key) => hmac::verify(key, message, signature_bytes).is_ok(),
            Material::Rsa { n, e } => algorithm.rsa().is_some_and(|parameters| {
                signature::RsaPublicKeyComponents { n, e }
                    .verify(parameters, message, signature_bytes)
                    .is_ok()
            }),
            Material::Point(verifier, point) => signature::UnparsedPublicKey::new(*verifier, point)
                .verify(message, signature_bytes)
                .is_ok(),
            Material::Ed25519(point) => {
                signature::UnparsedPublicKey::new(&signature::ED25519, point)
                    .verify(message, signature_bytes)
                    .is_ok()
            }
        }
    }
}

struct Cached {
    claims: Arc<Value>,
    /// The remote key set generation this was verified against.
    generation: u64,
    expires: Option<f64>,
    valid_until: f64,
}

struct Cache {
    entries: HashMap<Box<str>, Cached>,
    capacity: usize,
}

pub struct JwtAuth {
    algorithms: HashSet<Algorithm>,
    keys: Vec<Key>,
    // A multi-tenant API may trust many tenant issuers; lookups stay O(1).
    issuers: HashSet<String>,
    audiences: Vec<String>,
    leeway: f64,
    require_exp: bool,
    entra_access_token: bool,
    pub exclude_paths: HashSet<String>,
    cache: Option<Mutex<Cache>>,
    remote: Option<Arc<crate::jwks::Remote>>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Rejection {
    /// No bearer credential was supplied.
    Missing,
    /// The credential is malformed, unverifiable, expired or not for this service.
    Invalid,
}

/// Strict unpadded base64url (RFC 7515 section 2) with canonical trailing bits.
pub fn base64url(input: &[u8]) -> Option<Vec<u8>> {
    fn value(byte: u8) -> Option<u32> {
        Some(match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'-' => 62,
            b'_' => 63,
            _ => return None,
        } as u32)
    }
    if input.len() % 4 == 1 {
        return None;
    }
    let mut output = Vec::with_capacity(input.len() / 4 * 3 + 2);
    let (chunks, remainder) = input.as_chunks::<4>();
    for chunk in chunks {
        let word = value(chunk[0])? << 18
            | value(chunk[1])? << 12
            | value(chunk[2])? << 6
            | value(chunk[3])?;
        output.extend_from_slice(&[(word >> 16) as u8, (word >> 8) as u8, word as u8]);
    }
    match *remainder {
        [] => {}
        [a, b] => {
            let word = value(a)? << 18 | value(b)? << 12;
            if word & 0xffff != 0 {
                return None;
            }
            output.push((word >> 16) as u8);
        }
        [a, b, c] => {
            let word = value(a)? << 18 | value(b)? << 12 | value(c)? << 6;
            if word & 0xff != 0 {
                return None;
            }
            output.extend_from_slice(&[(word >> 16) as u8, (word >> 8) as u8]);
        }
        _ => return None,
    }
    Some(output)
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |value| value.as_secs_f64())
}

fn text_list(config: &Config, name: &str) -> Result<Vec<String>, String> {
    text_list_up_to(config, name, 64)
}

fn text_list_up_to(config: &Config, name: &str, maximum: usize) -> Result<Vec<String>, String> {
    match config.get(name) {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(Value::String(value)) if !value.is_empty() && value.len() <= 2048 => {
            Ok(vec![value.clone()])
        }
        Some(Value::Array(values)) if !values.is_empty() && values.len() <= maximum => values
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .filter(|value| !value.is_empty() && value.len() <= 2048)
                    .map(str::to_owned)
                    .ok_or_else(|| format!("jwt {name} must contain nonempty strings"))
            })
            .collect(),
        _ => Err(format!(
            "jwt {name} must be a nonempty string or a list of 1 to {maximum} strings"
        )),
    }
}

fn jwk_bytes(jwk: &Config, name: &str) -> Result<Vec<u8>, String> {
    jwk.get(name)
        .and_then(Value::as_str)
        .and_then(|value| base64url(value.as_bytes()))
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("JWK member {name} must be nonempty base64url"))
}

/// The signing keys of a key set the operator wrote into the configuration.
/// Any key that cannot be used as written is an error.
pub(crate) fn jwk_keys(document: &Value, allowed: &HashSet<Algorithm>) -> Result<Vec<Key>, String> {
    collect_jwk_keys(document, allowed, false).map(|(keys, _)| keys)
}

/// The signing keys of a key set fetched from an issuer, and how many of its
/// keys were left out because they cannot be used (too small a modulus, a
/// malformed member). One such key must not stop the rest of the set from
/// rotating: a stale set keeps retired keys trusted and never learns new ones.
pub(crate) fn usable_jwk_keys(
    document: &Value,
    allowed: &HashSet<Algorithm>,
) -> Result<(Vec<Key>, usize), String> {
    collect_jwk_keys(document, allowed, true)
}

fn collect_jwk_keys(
    document: &Value,
    allowed: &HashSet<Algorithm>,
    skip_unusable: bool,
) -> Result<(Vec<Key>, usize), String> {
    let keys = document
        .get("keys")
        .and_then(Value::as_array)
        .ok_or("jwks must be a JSON Web Key Set object with a keys array")?;
    if keys.len() > 256 {
        return Err("jwks cannot contain more than 256 keys".into());
    }
    let mut output = Vec::new();
    let mut skipped = 0;
    for jwk in keys {
        match jwk_key(jwk, allowed) {
            Ok(Some(key)) => output.push(key),
            Ok(None) => {}
            Err(_) if skip_unusable => skipped += 1,
            Err(error) => return Err(error),
        }
    }
    if output.iter().filter(|key| key.kid.is_none()).count() > MAX_KIDLESS_KEYS {
        return Err(format!(
            "jwks cannot contain more than {MAX_KIDLESS_KEYS} signing keys without kid"
        ));
    }
    Ok((output, skipped))
}

/// One member of a key set: its key, `None` when it is not a signing key for
/// an allowed algorithm, or why it cannot be used.
fn jwk_key(jwk: &Value, allowed: &HashSet<Algorithm>) -> Result<Option<Key>, String> {
    let jwk = jwk.as_object().ok_or("jwks keys must be objects")?;
    // Encryption keys and unknown key types never verify signatures.
    if jwk
        .get("use")
        .and_then(Value::as_str)
        .is_some_and(|value| value != "sig")
    {
        return Ok(None);
    }
    if let Some(operations) = jwk.get("key_ops") {
        let verifies = operations
            .as_array()
            .is_some_and(|items| items.iter().any(|item| item == "verify"));
        if !verifies {
            return Ok(None);
        }
    }
    let kid = match jwk.get("kid") {
        None => None,
        Some(Value::String(value)) if value.len() <= 256 => Some(value.clone()),
        _ => return Err("JWK kid must be a string of at most 256 bytes".into()),
    };
    let declared = match jwk.get("alg") {
        None => None,
        Some(Value::String(value)) => match Algorithm::parse(value) {
            Some(value) => Some(value),
            None => return Ok(None),
        },
        _ => return Err("JWK alg must be a string".into()),
    };
    let (family, material): (Vec<Algorithm>, Material) =
        match jwk.get("kty").and_then(Value::as_str) {
            Some("RSA") => {
                let n = jwk_bytes(jwk, "n")?;
                let e = jwk_bytes(jwk, "e")?;
                let bits = (n.len() - n.iter().take_while(|byte| **byte == 0).count()) * 8;
                if bits < 2048 {
                    return Err("RSA JWKs must have at least a 2048-bit modulus".into());
                }
                use Algorithm::*;
                (
                    vec![RS256, RS384, RS512, PS256, PS384, PS512],
                    Material::Rsa { n, e },
                )
            }
            Some("EC") => {
                let (algorithm, verifier, size): (
                    _,
                    &'static signature::EcdsaVerificationAlgorithm,
                    _,
                ) = match jwk.get("crv").and_then(Value::as_str) {
                    Some("P-256") => (Algorithm::ES256, &signature::ECDSA_P256_SHA256_FIXED, 32),
                    Some("P-384") => (Algorithm::ES384, &signature::ECDSA_P384_SHA384_FIXED, 48),
                    _ => return Ok(None),
                };
                let x = jwk_bytes(jwk, "x")?;
                let y = jwk_bytes(jwk, "y")?;
                if x.len() != size || y.len() != size {
                    return Err("EC JWK coordinates have the wrong length".into());
                }
                let mut point = Vec::with_capacity(1 + 2 * size);
                point.push(4);
                point.extend_from_slice(&x);
                point.extend_from_slice(&y);
                (vec![algorithm], Material::Point(verifier, point))
            }
            Some("OKP") => {
                if jwk.get("crv").and_then(Value::as_str) != Some("Ed25519") {
                    return Ok(None);
                }
                let x = jwk_bytes(jwk, "x")?;
                if x.len() != 32 {
                    return Err("Ed25519 JWK x must be 32 bytes".into());
                }
                (vec![Algorithm::EdDSA], Material::Ed25519(x))
            }
            // Shared secrets belong in secret_env, never a published key set.
            _ => return Ok(None),
        };
    let algorithms: Vec<_> = family
        .into_iter()
        .filter(|algorithm| {
            allowed.contains(algorithm) && declared.is_none_or(|value| value == *algorithm)
        })
        .collect();
    Ok((!algorithms.is_empty()).then_some(Key {
        kid,
        algorithms,
        material,
    }))
}

fn numeric(claims: &Map<String, Value>, name: &str) -> Result<Option<f64>, Rejection> {
    match claims.get(name) {
        None => Ok(None),
        Some(Value::Number(value)) => value
            .as_f64()
            .filter(|value| value.is_finite())
            .map(Some)
            .ok_or(Rejection::Invalid),
        Some(_) => Err(Rejection::Invalid),
    }
}

fn entra_access_claims(claims: &Map<String, Value>) -> bool {
    let scopes = claims
        .get("scp")
        .and_then(Value::as_str)
        .is_some_and(|value| value.split_whitespace().next().is_some());
    match claims.get("idtyp") {
        Some(Value::String(value)) if value == "app" => {
            claims.get("scp").is_none()
                && claims
                    .get("roles")
                    .and_then(Value::as_array)
                    .is_some_and(|roles| {
                        !roles.is_empty()
                            && roles.iter().all(|role| {
                                role.as_str().is_some_and(|value| !value.trim().is_empty())
                            })
                    })
        }
        None => scopes,
        Some(Value::String(value)) if value == "user" || value == "device" => scopes,
        _ => false,
    }
}

impl JwtAuth {
    pub fn parse(
        value: &Value,
        secret: impl Fn(&str) -> Result<Vec<u8>, String>,
    ) -> Result<Self, String> {
        let config = value.as_object().ok_or("jwt must be an object")?;
        const SETTINGS: [&str; 14] = [
            "algorithms",
            "secret_env",
            "jwks_env",
            "jwks_file",
            "jwks_url",
            "jwks_ca_file",
            "jwks_refresh_seconds",
            "issuer",
            "audience",
            "leeway_seconds",
            "require_exp",
            "exclude_paths",
            "cache_size",
            "entra_access_token",
        ];
        if config.keys().any(|key| !SETTINGS.contains(&key.as_str())) {
            return Err("jwt contains unsupported settings".into());
        }
        let names = config
            .get("algorithms")
            .and_then(Value::as_array)
            .filter(|values| !values.is_empty() && values.len() <= 12)
            .ok_or("jwt algorithms must be a nonempty list")?;
        let mut algorithms = HashSet::new();
        for name in names {
            let algorithm = name.as_str().and_then(Algorithm::parse).ok_or(
                "jwt algorithms must be supported JWS algorithm names; none is never accepted",
            )?;
            algorithms.insert(algorithm);
        }
        let symmetric: Vec<_> = algorithms
            .iter()
            .copied()
            .filter(|a| a.hmac().is_some())
            .collect();
        let asymmetric = algorithms.len() > symmetric.len();
        let mut keys = Vec::new();
        match config.get("secret_env") {
            None | Some(Value::Null) => {
                if !symmetric.is_empty() {
                    return Err("HMAC jwt algorithms require secret_env".into());
                }
            }
            Some(Value::String(name)) => {
                if symmetric.is_empty() {
                    return Err("jwt secret_env requires an HS256, HS384 or HS512 algorithm".into());
                }
                let secret = secret(name)?;
                if secret.len() < 32 {
                    return Err("jwt HMAC secrets must contain at least 32 bytes".into());
                }
                for algorithm in symmetric {
                    keys.push(Key {
                        kid: None,
                        algorithms: vec![algorithm],
                        material: Material::Hmac(hmac::Key::new(
                            algorithm.hmac().unwrap(),
                            &secret,
                        )),
                    });
                }
            }
            _ => return Err("jwt secret_env must be a string".into()),
        }
        let present = |name: &str| config.get(name).is_some_and(|value| !value.is_null());
        if ["jwks_env", "jwks_file", "jwks_url"]
            .iter()
            .filter(|name| present(name))
            .count()
            > 1
        {
            return Err("jwt accepts only one of jwks_env, jwks_file and jwks_url".into());
        }
        let remote = match config.get("jwks_url") {
            None | Some(Value::Null) => {
                if present("jwks_ca_file") || present("jwks_refresh_seconds") {
                    return Err("jwt jwks_ca_file and jwks_refresh_seconds require jwks_url".into());
                }
                None
            }
            Some(Value::String(url)) => {
                if !asymmetric {
                    return Err("a JWKS requires an asymmetric jwt algorithm".into());
                }
                let target = crate::jwks::Target::parse(url)?;
                let ca_file = match config.get("jwks_ca_file") {
                    None | Some(Value::Null) => None,
                    Some(Value::String(path)) if !path.is_empty() => Some(path.clone()),
                    _ => return Err("jwt jwks_ca_file must be a file path".into()),
                };
                let refresh = match config.get("jwks_refresh_seconds") {
                    None | Some(Value::Null) => 300,
                    Some(value) => value
                        .as_u64()
                        .filter(|value| (30..=86_400).contains(value))
                        .ok_or(
                            "jwt jwks_refresh_seconds must be an integer between 30 and 86400",
                        )?,
                };
                Some((target, ca_file, std::time::Duration::from_secs(refresh)))
            }
            _ => return Err("jwt jwks_url must be a string".into()),
        };
        let jwks = match (config.get("jwks_env"), config.get("jwks_file")) {
            (Some(Value::String(name)), _) => Some(std::env::var(name).map_err(|_| {
                format!("required JWKS environment variable {name} is unavailable")
            })?),
            (_, Some(Value::String(path))) => {
                let metadata = std::fs::metadata(path)
                    .map_err(|_| format!("cannot read jwt jwks_file {path}"))?;
                if metadata.len() > 1_048_576 {
                    return Err("jwt jwks_file cannot exceed 1 MiB".into());
                }
                Some(
                    std::fs::read_to_string(path)
                        .map_err(|_| format!("cannot read jwt jwks_file {path}"))?,
                )
            }
            (None | Some(Value::Null), None | Some(Value::Null)) => None,
            _ => return Err("jwt jwks_env and jwks_file must be strings".into()),
        };
        match jwks {
            Some(text) => {
                if !asymmetric {
                    return Err("a JWKS requires an asymmetric jwt algorithm".into());
                }
                if text.len() > 1_048_576 {
                    return Err("a JWKS cannot exceed 1 MiB".into());
                }
                let document: Value =
                    serde_json::from_str(&text).map_err(|_| "the JWKS is not valid JSON")?;
                let found = jwk_keys(&document, &algorithms)?;
                if found.is_empty() {
                    return Err(
                        "the JWKS contains no signing key for the allowed algorithms".into(),
                    );
                }
                keys.extend(found);
            }
            None if asymmetric && remote.is_none() => {
                return Err(
                    "asymmetric jwt algorithms require jwks_env, jwks_file or jwks_url".into(),
                );
            }
            None => {}
        }
        let issuers: HashSet<String> = text_list_up_to(config, "issuer", 1024)?
            .into_iter()
            .collect();
        let audiences = text_list(config, "audience")?;
        let leeway = match config.get("leeway_seconds") {
            None => 30,
            Some(value) => value
                .as_u64()
                .filter(|value| *value <= 300)
                .ok_or("jwt leeway_seconds must be an integer between 0 and 300")?,
        } as f64;
        let require_exp = match config.get("require_exp") {
            None => true,
            Some(Value::Bool(value)) => *value,
            _ => return Err("jwt require_exp must be a bool".into()),
        };
        let entra_access_token = match config.get("entra_access_token") {
            None => false,
            Some(Value::Bool(value)) => *value,
            _ => return Err("jwt entra_access_token must be a bool".into()),
        };
        if entra_access_token && (issuers.is_empty() || audiences.is_empty() || !require_exp) {
            return Err("jwt entra_access_token requires issuer, audience and exp".into());
        }
        let paths = match config.get("exclude_paths") {
            None => Vec::new(),
            Some(Value::Array(values)) if values.len() <= 1024 => values
                .iter()
                .map(|value| {
                    value
                        .as_str()
                        .filter(|path| crate::controls::exact_path(path))
                        .map(str::to_owned)
                        .ok_or_else(|| {
                            "jwt exclude_paths must contain exact absolute paths".to_owned()
                        })
                })
                .collect::<Result<_, _>>()?,
            _ => {
                return Err(
                    "jwt exclude_paths must contain at most 1024 exact absolute paths".into(),
                );
            }
        };
        let cache_size = match config.get("cache_size") {
            None => 4096,
            Some(value) => value
                .as_u64()
                .filter(|value| *value <= 1_000_000)
                .ok_or("jwt cache_size must be an integer between 0 and 1000000")?
                as usize,
        };
        // Started last, once every other setting is known to be valid.
        let remote = remote
            .map(|(target, ca_file, refresh)| {
                crate::jwks::Remote::start(target, ca_file, refresh, algorithms.clone())
            })
            .transpose()?;
        Ok(Self {
            algorithms,
            keys,
            issuers,
            audiences,
            leeway,
            require_exp,
            entra_access_token,
            exclude_paths: paths.into_iter().collect(),
            cache: (cache_size > 0).then(|| {
                Mutex::new(Cache {
                    entries: HashMap::with_capacity(cache_size.min(4096)),
                    capacity: cache_size,
                })
            }),
            remote,
        })
    }

    /// Verify an `Authorization` header value and return the token's claims.
    pub fn authenticate(&self, authorization: Option<&[u8]>) -> Result<Arc<Value>, Rejection> {
        let value = authorization.ok_or(Rejection::Missing)?;
        if value.len() < 7 || !value[..7].eq_ignore_ascii_case(b"bearer ") {
            return Err(Rejection::Missing);
        }
        let token = value[7..].trim_ascii();
        self.verify(token, now())
    }

    pub fn verify(&self, token: &[u8], now: f64) -> Result<Arc<Value>, Rejection> {
        if token.is_empty() || token.len() > MAX_TOKEN_BYTES {
            return Err(Rejection::Invalid);
        }
        let token = std::str::from_utf8(token).map_err(|_| Rejection::Invalid)?;
        let generation = self.remote.as_ref().map_or(0, |remote| remote.generation());
        if let Some(cache) = &self.cache {
            let mut cache = cache.lock().unwrap();
            if let Some(entry) = cache.entries.get(token) {
                if entry.generation == generation
                    && now < entry.valid_until
                    && entry
                        .expires
                        .is_none_or(|expires| now < expires + self.leeway)
                {
                    return Ok(entry.claims.clone());
                }
                cache.entries.remove(token);
            }
        }
        let (claims, expires) = self.verify_uncached(token, now)?;
        let claims = Arc::new(claims);
        if let Some(cache) = &self.cache {
            let valid_until = now
                + expires
                    .map_or(MAX_CACHE_SECONDS, |expires| expires + self.leeway - now)
                    .min(MAX_CACHE_SECONDS);
            let mut cache = cache.lock().unwrap();
            if cache.entries.len() >= cache.capacity {
                cache.entries.retain(|_, entry| now < entry.valid_until);
                if cache.entries.len() >= cache.capacity {
                    // Bounded memory under token churn: drop the whole generation.
                    cache.entries.clear();
                }
            }
            cache.entries.insert(
                token.into(),
                Cached {
                    claims: claims.clone(),
                    generation,
                    expires,
                    valid_until,
                },
            );
        }
        Ok(claims)
    }

    fn verify_uncached(&self, token: &str, now: f64) -> Result<(Value, Option<f64>), Rejection> {
        let mut parts = token.splitn(4, '.');
        let (Some(header), Some(payload), Some(signature_text), None) =
            (parts.next(), parts.next(), parts.next(), parts.next())
        else {
            return Err(Rejection::Invalid);
        };
        let header_bytes = base64url(header.as_bytes()).ok_or(Rejection::Invalid)?;
        let header: Value =
            serde_json::from_slice(&header_bytes).map_err(|_| Rejection::Invalid)?;
        let header = header.as_object().ok_or(Rejection::Invalid)?;
        if header.contains_key("crit") {
            return Err(Rejection::Invalid);
        }
        let algorithm = header
            .get("alg")
            .and_then(Value::as_str)
            .and_then(Algorithm::parse)
            .filter(|algorithm| self.algorithms.contains(algorithm))
            .ok_or(Rejection::Invalid)?;
        let kid = match header.get("kid") {
            None => None,
            Some(Value::String(value)) => Some(value.as_str()),
            Some(_) => return Err(Rejection::Invalid),
        };
        let signature_bytes = base64url(signature_text.as_bytes()).ok_or(Rejection::Invalid)?;
        let message = &token.as_bytes()[..header_len(token, payload)];
        let remote_keys = self.remote.as_ref().map(|remote| remote.keys());
        let keys = || {
            self.keys
                .iter()
                .chain(remote_keys.iter().flat_map(|keys| keys.iter()))
        };
        let mut candidates = keys().filter(|key| key.algorithms.contains(&algorithm));
        // Require a kid match only when keys for this algorithm carry kids, so
        // an unrelated JWKS never shadows the HMAC secret.
        let identified = keys().any(|key| key.kid.is_some() && key.algorithms.contains(&algorithm));
        // A kid we have never seen may be a newly rotated remote key: ask for
        // an early refresh (rate limited) and judge this token on what we have.
        if let (Some(kid), Some(remote)) = (kid, &self.remote)
            && !keys().any(|key| key.kid.as_deref() == Some(kid))
        {
            remote.kid_miss();
        }
        let verified = match kid {
            Some(kid) if identified => candidates
                .find(|key| key.kid.as_deref() == Some(kid))
                .is_some_and(|key| key.verify(algorithm, message, &signature_bytes)),
            None if identified => false,
            // Unidentified keys are tried in order; JWKS limits these to eight.
            _ => candidates.any(|key| key.verify(algorithm, message, &signature_bytes)),
        };
        if !verified {
            return Err(Rejection::Invalid);
        }
        let payload = base64url(payload.as_bytes()).ok_or(Rejection::Invalid)?;
        let claims = bessemer_codec::parse_value(&payload).map_err(|_| Rejection::Invalid)?;
        let object = claims.as_object().ok_or(Rejection::Invalid)?;
        let expires = numeric(object, "exp")?;
        if expires.is_none() && self.require_exp {
            return Err(Rejection::Invalid);
        }
        if expires.is_some_and(|expires| now >= expires + self.leeway) {
            return Err(Rejection::Invalid);
        }
        if numeric(object, "nbf")?.is_some_and(|not_before| now + self.leeway < not_before) {
            return Err(Rejection::Invalid);
        }
        numeric(object, "iat")?;
        if !self.issuers.is_empty() {
            let issuer = object.get("iss").and_then(Value::as_str);
            if !issuer.is_some_and(|issuer| self.issuers.contains(issuer)) {
                return Err(Rejection::Invalid);
            }
        }
        if !self.audiences.is_empty() {
            let accepted = |value: &Value| {
                value
                    .as_str()
                    .is_some_and(|value| self.audiences.iter().any(|audience| audience == value))
            };
            let matches = match object.get("aud") {
                Some(Value::Array(values)) => values.iter().any(accepted),
                Some(value) => accepted(value),
                None => false,
            };
            if !matches {
                return Err(Rejection::Invalid);
            }
        }
        if self.entra_access_token && !entra_access_claims(object) {
            return Err(Rejection::Invalid);
        }
        Ok((claims, expires))
    }
}

/// Length of the signing input `header.payload`.
fn header_len(token: &str, payload: &str) -> usize {
    payload.as_ptr() as usize - token.as_ptr() as usize + payload.len()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn encode(input: &[u8]) -> String {
        const ALPHABET: &[u8; 64] =
            b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
        let mut output = String::new();
        for chunk in input.chunks(3) {
            let word = (chunk[0] as u32) << 16
                | (*chunk.get(1).unwrap_or(&0) as u32) << 8
                | *chunk.get(2).unwrap_or(&0) as u32;
            for index in 0..=chunk.len() {
                output.push(ALPHABET[(word >> (18 - 6 * index) & 63) as usize] as char);
            }
        }
        output
    }

    const SECRET: &[u8] = b"0123456789abcdef0123456789abcdef";

    fn hs256(header: &Value, claims: &Value) -> String {
        let signing = format!(
            "{}.{}",
            encode(header.to_string().as_bytes()),
            encode(claims.to_string().as_bytes())
        );
        let tag = hmac::sign(
            &hmac::Key::new(hmac::HMAC_SHA256, SECRET),
            signing.as_bytes(),
        );
        format!("{signing}.{}", encode(tag.as_ref()))
    }

    fn auth(extra: Value) -> JwtAuth {
        let mut config = json!({"algorithms": ["HS256"], "secret_env": "S"});
        config
            .as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        JwtAuth::parse(&config, |_| Ok(SECRET.to_vec())).unwrap()
    }

    #[test]
    fn base64url_is_strict_and_round_trips() {
        for length in 0..40 {
            let data: Vec<u8> = (0..length).map(|value| (value * 37 + 11) as u8).collect();
            assert_eq!(base64url(encode(&data).as_bytes()).unwrap(), data);
        }
        for invalid in ["A", "AB=", "AB+/", "A B C", "QR", "QUF"] {
            assert!(base64url(invalid.as_bytes()).is_none(), "{invalid}");
        }
        assert_eq!(base64url(b"QQ").unwrap(), b"A");
    }

    #[test]
    fn a_large_issuer_list_accepts_only_listed_tenants() {
        let issuers: Vec<String> = (0..1024)
            .map(|tenant| format!("https://login.microsoftonline.com/{tenant:08}/v2.0"))
            .collect();
        let jwt = auth(json!({"issuer": issuers, "audience": "api"}));
        let header = json!({"alg": "HS256", "typ": "JWT"});
        for (issuer, accepted) in [
            ("https://login.microsoftonline.com/00000000/v2.0", true),
            ("https://login.microsoftonline.com/00001023/v2.0", true),
            ("https://login.microsoftonline.com/00001024/v2.0", false),
        ] {
            let claims = json!({"sub": "u1", "iss": issuer, "aud": "api", "exp": 2000});
            let result = jwt.verify(hs256(&header, &claims).as_bytes(), 950.0);
            assert_eq!(result.is_ok(), accepted, "{issuer}");
        }
        let too_many: Vec<String> = (0..1025).map(|tenant| tenant.to_string()).collect();
        let audiences: Vec<String> = (0..65).map(|audience| audience.to_string()).collect();
        for extra in [json!({"issuer": too_many}), json!({"audience": audiences})] {
            let mut config = json!({"algorithms": ["HS256"], "secret_env": "S"});
            config
                .as_object_mut()
                .unwrap()
                .extend(extra.as_object().unwrap().clone());
            assert!(JwtAuth::parse(&config, |_| Ok(SECRET.to_vec())).is_err());
        }
    }

    #[test]
    fn hmac_tokens_verify_claims_with_leeway() {
        let jwt = auth(json!({"issuer": "https://issuer", "audience": ["api", "other"]}));
        let header = json!({"alg": "HS256", "typ": "JWT"});
        let good =
            json!({"sub": "u1", "iss": "https://issuer", "aud": "api", "exp": 1000, "nbf": 900});
        assert_eq!(
            jwt.verify(hs256(&header, &good).as_bytes(), 950.0).unwrap()["sub"],
            "u1"
        );
        // Leeway accepts recently expired and almost-valid tokens.
        assert!(jwt.verify(hs256(&header, &good).as_bytes(), 1029.0).is_ok());
        assert!(jwt.verify(hs256(&header, &good).as_bytes(), 871.0).is_ok());
        let cases = [
            json!({"sub": "u1", "iss": "https://issuer", "aud": "api", "exp": 1000, "nbf": 900}),
            json!({"iss": "https://evil", "aud": "api", "exp": 2000}),
            json!({"iss": "https://issuer", "aud": ["nope"], "exp": 2000}),
            json!({"iss": "https://issuer", "aud": "api"}),
            json!({"iss": "https://issuer", "aud": "api", "exp": "2000"}),
        ];
        for (index, claims) in cases.iter().enumerate() {
            let now = if index == 0 { 1031.0 } else { 950.0 };
            assert_eq!(
                jwt.verify(hs256(&header, claims).as_bytes(), now),
                Err(Rejection::Invalid),
                "{claims}"
            );
        }
    }

    #[test]
    fn entra_access_profile_requires_delegated_scopes_or_app_roles() {
        let config =
            json!({"issuer": "https://issuer", "audience": "api", "entra_access_token": true});
        let jwt = auth(config);
        let header = json!({"alg": "HS256", "typ": "JWT"});
        let base = json!({"iss": "https://issuer", "aud": "api", "exp": 2000});
        for extra in [
            json!({"scp": "items.read"}),
            json!({"scp": "items.read items.write", "idtyp": "user"}),
            json!({"idtyp": "app", "roles": ["Items.Read"]}),
        ] {
            let mut claims = base.clone();
            claims
                .as_object_mut()
                .unwrap()
                .extend(extra.as_object().unwrap().clone());
            assert!(
                jwt.verify(hs256(&header, &claims).as_bytes(), 1.0).is_ok(),
                "{claims}"
            );
        }
        for extra in [
            json!({"nonce": "browser-login"}),
            json!({"roles": ["Items.Read"]}),
            json!({"scp": "  "}),
            json!({"scp": ["items.read"]}),
            json!({"scp": "items.read", "idtyp": "app", "roles": ["Items.Read"]}),
            json!({"idtyp": "app", "roles": []}),
            json!({"idtyp": "app", "roles": [""]}),
            json!({"idtyp": 1, "scp": "items.read"}),
            json!({"idtyp": "other", "scp": "items.read"}),
        ] {
            let mut claims = base.clone();
            claims
                .as_object_mut()
                .unwrap()
                .extend(extra.as_object().unwrap().clone());
            assert_eq!(
                jwt.verify(hs256(&header, &claims).as_bytes(), 1.0),
                Err(Rejection::Invalid),
                "{claims}"
            );
        }
        assert!(
            auth(json!({"issuer": "https://issuer", "audience": "api"}))
                .verify(hs256(&header, &base).as_bytes(), 1.0)
                .is_ok()
        );
    }

    #[test]
    fn algorithm_confusion_none_and_tampering_are_rejected() {
        let jwt = auth(json!({}));
        let claims = json!({"exp": 2000});
        for header in [
            json!({"alg": "none"}),
            json!({"alg": "HS384"}),
            json!({"alg": "RS256"}),
            json!({"alg": "HS256", "crit": ["b64"]}),
            json!({"alg": "HS256", "kid": 1}),
        ] {
            assert!(
                jwt.verify(hs256(&header, &claims).as_bytes(), 1.0).is_err(),
                "{header}"
            );
        }
        let token = hs256(&json!({"alg": "HS256"}), &claims);
        let (signing, signature_text) = token.rsplit_once('.').unwrap();
        let tampered = format!(
            "{}.{}",
            signing.replace(&encode(b"{\"exp\":2000}"), &encode(b"{\"exp\":9999}")),
            signature_text
        );
        assert!(jwt.verify(tampered.as_bytes(), 1.0).is_err());
        let unsigned = format!("{signing}.");
        assert!(jwt.verify(unsigned.as_bytes(), 1.0).is_err());
        assert!(jwt.verify(format!("{token}.x").as_bytes(), 1.0).is_err());
        assert!(jwt.verify(&vec![b'a'; MAX_TOKEN_BYTES + 1], 1.0).is_err());
        let array_claims = hs256(&json!({"alg": "HS256"}), &json!([1]));
        assert!(jwt.verify(array_claims.as_bytes(), 1.0).is_err());
        // Leading JSON whitespace in a signed payload is still an object.
        let signing = format!(
            "{}.{}",
            encode(br#"{"alg":"HS256"}"#),
            encode(b"\n {\"exp\":2000}")
        );
        let tag = hmac::sign(
            &hmac::Key::new(hmac::HMAC_SHA256, SECRET),
            signing.as_bytes(),
        );
        let spaced = format!("{signing}.{}", encode(tag.as_ref()));
        assert!(jwt.verify(spaced.as_bytes(), 1.0).is_ok());
    }

    #[test]
    fn cache_honours_expiry_and_stays_bounded() {
        let jwt = auth(json!({"cache_size": 2}));
        let header = json!({"alg": "HS256"});
        let token = hs256(&header, &json!({"exp": 100}));
        assert!(jwt.verify(token.as_bytes(), 10.0).is_ok());
        assert!(jwt.verify(token.as_bytes(), 129.0).is_ok());
        assert_eq!(jwt.verify(token.as_bytes(), 131.0), Err(Rejection::Invalid));
        for exp in 0..10 {
            let token = hs256(&header, &json!({"exp": 1000 + exp}));
            assert!(jwt.verify(token.as_bytes(), 10.0).is_ok());
        }
        assert!(jwt.cache.as_ref().unwrap().lock().unwrap().entries.len() <= 2);
    }

    #[test]
    fn bearer_header_parsing() {
        let jwt = auth(json!({}));
        let token = hs256(&json!({"alg": "HS256"}), &json!({"exp": 4_000_000_000u64}));
        assert!(
            jwt.authenticate(Some(format!("Bearer {token}").as_bytes()))
                .is_ok()
        );
        assert!(
            jwt.authenticate(Some(format!("bearer  {token} ").as_bytes()))
                .is_ok()
        );
        assert_eq!(jwt.authenticate(None), Err(Rejection::Missing));
        assert_eq!(
            jwt.authenticate(Some(b"Basic abc")),
            Err(Rejection::Missing)
        );
        assert_eq!(
            jwt.authenticate(Some(b"Bearer abc")),
            Err(Rejection::Invalid)
        );
    }

    #[test]
    fn configuration_fails_closed() {
        let secret = |_: &str| Ok(SECRET.to_vec());
        for config in [
            json!({"algorithms": []}),
            json!({"algorithms": ["none"]}),
            json!({"algorithms": ["HS256"]}),
            json!({"algorithms": ["RS256"]}),
            json!({"algorithms": ["RS256"], "secret_env": "S"}),
            json!({"algorithms": ["HS256"], "secret_env": "S", "jwks_url": "https://x"}),
            json!({"algorithms": ["HS256"], "secret_env": "S", "leeway_seconds": 301}),
            json!({"algorithms": ["HS256"], "secret_env": "S", "audience": []}),
            json!({"algorithms": ["HS256"], "secret_env": "S", "entra_access_token": true}),
            json!({"algorithms": ["HS256"], "secret_env": "S", "issuer": "issuer", "audience": "api", "entra_access_token": true, "require_exp": false}),
        ] {
            assert!(JwtAuth::parse(&config, secret).is_err(), "{config}");
        }
        assert!(
            JwtAuth::parse(
                &json!({"algorithms": ["HS256"], "secret_env": "S"}),
                |_| Ok(b"short".to_vec())
            )
            .is_err()
        );
    }

    #[test]
    fn jwks_keys_are_bound_to_their_algorithm() {
        let ed = signature::Ed25519KeyPair::from_pkcs8(
            signature::Ed25519KeyPair::generate_pkcs8(&ring::rand::SystemRandom::new())
                .unwrap()
                .as_ref(),
        )
        .unwrap();
        use signature::KeyPair;
        let jwks = json!({"keys": [
            {"kty": "OKP", "crv": "Ed25519", "kid": "ed", "x": encode(ed.public_key().as_ref())},
            {"kty": "oct", "k": encode(SECRET)},
            {"kty": "OKP", "crv": "Ed25519", "use": "enc", "kid": "enc", "x": encode(ed.public_key().as_ref())},
        ]});
        let path = std::env::temp_dir().join(format!("bessemer-jwks-{}.json", std::process::id()));
        std::fs::write(&path, jwks.to_string()).unwrap();
        let jwt = JwtAuth::parse(
            &json!({"algorithms": ["EdDSA"], "jwks_file": path.to_str().unwrap()}),
            |_| unreachable!(),
        )
        .unwrap();
        std::fs::remove_file(&path).unwrap();
        assert_eq!(jwt.keys.len(), 1);
        let sign = |header: Value| {
            let signing = format!(
                "{}.{}",
                encode(header.to_string().as_bytes()),
                encode(json!({"exp": 2000}).to_string().as_bytes())
            );
            format!("{signing}.{}", encode(ed.sign(signing.as_bytes()).as_ref()))
        };
        assert!(
            jwt.verify(sign(json!({"alg": "EdDSA", "kid": "ed"})).as_bytes(), 1.0)
                .is_ok()
        );
        assert_eq!(
            jwt.verify(sign(json!({"alg": "EdDSA"})).as_bytes(), 1.0),
            Err(Rejection::Invalid)
        );
        assert!(
            jwt.verify(
                sign(json!({"alg": "EdDSA", "kid": "other"})).as_bytes(),
                1.0
            )
            .is_err()
        );
        // The public key bytes cannot be used as an HMAC secret.
        let forged = {
            let signing = format!(
                "{}.{}",
                encode(json!({"alg": "HS256", "kid": "ed"}).to_string().as_bytes()),
                encode(json!({"exp": 2000}).to_string().as_bytes())
            );
            let tag = hmac::sign(
                &hmac::Key::new(hmac::HMAC_SHA256, ed.public_key().as_ref()),
                signing.as_bytes(),
            );
            format!("{signing}.{}", encode(tag.as_ref()))
        };
        assert!(jwt.verify(forged.as_bytes(), 1.0).is_err());
    }

    #[test]
    fn jwks_limits_kidless_keys_before_accepting_requests() {
        let ed = signature::Ed25519KeyPair::from_pkcs8(
            signature::Ed25519KeyPair::generate_pkcs8(&ring::rand::SystemRandom::new())
                .unwrap()
                .as_ref(),
        )
        .unwrap();
        use signature::KeyPair;
        let key = json!({"kty": "OKP", "crv": "Ed25519", "x": encode(ed.public_key().as_ref())});
        let algorithms = [Algorithm::EdDSA].into_iter().collect();
        assert!(
            jwk_keys(
                &json!({"keys": vec![key.clone(); MAX_KIDLESS_KEYS]}),
                &algorithms
            )
            .is_ok()
        );
        assert!(
            jwk_keys(
                &json!({"keys": vec![key; MAX_KIDLESS_KEYS + 1]}),
                &algorithms
            )
            .is_err()
        );
    }

    #[test]
    fn a_fetched_key_set_keeps_its_usable_keys_when_one_cannot_be_used() {
        let random = ring::rand::SystemRandom::new();
        let pkcs8 = signature::Ed25519KeyPair::generate_pkcs8(&random).unwrap();
        let ed = signature::Ed25519KeyPair::from_pkcs8(pkcs8.as_ref()).unwrap();
        use signature::KeyPair;
        let good = json!({"kty": "OKP", "crv": "Ed25519", "kid": "good", "x": encode(ed.public_key().as_ref())});
        // A 1024-bit RSA modulus, a truncated Ed25519 key and an oversized kid.
        let weak = json!({"kty": "RSA", "kid": "weak", "n": encode(&[0xff; 128]), "e": "AQAB"});
        let short = json!({"kty": "OKP", "crv": "Ed25519", "kid": "short", "x": encode(&[1; 8])});
        let long_kid = json!({"kty": "OKP", "crv": "Ed25519", "kid": "k".repeat(300), "x": encode(ed.public_key().as_ref())});
        let algorithms = [Algorithm::EdDSA, Algorithm::RS256].into_iter().collect();
        let document = json!({"keys": [weak, good, short, long_kid]});
        // Written into the configuration, any of them is the operator's error.
        assert!(jwk_keys(&document, &algorithms).is_err());
        // Fetched from an issuer, the usable key still rotates in.
        let (keys, skipped) = usable_jwk_keys(&document, &algorithms).unwrap();
        assert_eq!(skipped, 3);
        assert_eq!(keys.len(), 1);
        assert_eq!(keys[0].kid.as_deref(), Some("good"));
        // The limits on the set as a whole still apply.
        let many = json!({"keys": vec![json!({"kty": "oct"}); 257]});
        assert!(usable_jwk_keys(&many, &algorithms).is_err());
        assert!(usable_jwk_keys(&json!({"keys": "none"}), &algorithms).is_err());
    }

    /// Serve whatever `body` holds on a loopback port from a plain thread.
    fn serve_jwks(body: Arc<Mutex<String>>) -> u16 {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { continue };
                let mut buffer = [0u8; 4096];
                let _ = stream.read(&mut buffer);
                let body = body.lock().unwrap().clone();
                let _ = write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
            }
        });
        port
    }

    #[test]
    fn remote_jwks_picks_up_a_rotated_key_after_a_kid_miss() {
        let vectors: Value =
            serde_json::from_str(include_str!("../tests/fixtures/jwt_vectors.json")).unwrap();
        let token = |name: &str| {
            vectors[name]
                .as_array()
                .unwrap()
                .iter()
                .map(|part| part.as_str().unwrap())
                .collect::<Vec<_>>()
                .join(".")
                .into_bytes()
        };
        // Start with only the EC key published.
        let ec_only = json!({"keys": vectors["jwks"]["keys"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|key| key["kid"] == "ec1")
            .collect::<Vec<_>>()});
        let body = Arc::new(Mutex::new(ec_only.to_string()));
        let port = serve_jwks(body.clone());
        let jwt = JwtAuth::parse(
            &json!({
                "algorithms": ["RS256", "ES256"],
                "jwks_url": format!("http://127.0.0.1:{port}/.well-known/jwks.json"),
                "issuer": "https://issuer.test",
                "audience": "api",
            }),
            |_| unreachable!(),
        )
        .unwrap();
        assert_eq!(jwt.verify(&token("es256"), 1.0).unwrap()["sub"], "vector");
        // The issuer rotates in the RSA key; the first token naming it misses
        // and prompts an early refetch.
        *body.lock().unwrap() = vectors["jwks"].to_string();
        assert_eq!(jwt.verify(&token("rs256"), 1.0), Err(Rejection::Invalid));
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
        while jwt.verify(&token("rs256"), 1.0).is_err() && std::time::Instant::now() < deadline {
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        assert_eq!(jwt.verify(&token("rs256"), 1.0).unwrap()["sub"], "vector");
        // Settings that only make sense with a URL are rejected without one.
        for bad in [
            json!({"algorithms": ["RS256"], "jwks_url": "http://example.com/keys"}),
            json!({"algorithms": ["RS256"], "jwks_url": "https://x/k", "jwks_file": "/k"}),
            json!({"algorithms": ["RS256"], "jwks_file": "/k", "jwks_refresh_seconds": 60}),
            json!({"algorithms": ["RS256"], "jwks_url": "https://x/k", "jwks_refresh_seconds": 5}),
            json!({"algorithms": ["HS256"], "secret_env": "S", "jwks_url": "https://x/k"}),
        ] {
            assert!(
                JwtAuth::parse(&bad, |_| Ok(SECRET.to_vec())).is_err(),
                "{bad}"
            );
        }
    }

    #[test]
    fn openssl_rsa_and_ecdsa_vectors_verify_with_bound_algorithms() {
        // Generated with OpenSSL; only the public JWKS is committed.
        let vectors: Value =
            serde_json::from_str(include_str!("../tests/fixtures/jwt_vectors.json")).unwrap();
        let path =
            std::env::temp_dir().join(format!("bessemer-vectors-{}.json", std::process::id()));
        std::fs::write(&path, vectors["jwks"].to_string()).unwrap();
        let jwt = JwtAuth::parse(
            &json!({
                "algorithms": ["RS256", "PS256", "ES256"],
                "jwks_file": path.to_str().unwrap(),
                "issuer": "https://issuer.test",
                "audience": "api",
            }),
            |_| unreachable!(),
        )
        .unwrap();
        std::fs::remove_file(&path).unwrap();
        let token = |name: &str| {
            let parts: Vec<&str> = vectors[name]
                .as_array()
                .unwrap()
                .iter()
                .map(|part| part.as_str().unwrap())
                .collect();
            parts.join(".").into_bytes()
        };
        assert_eq!(jwt.verify(&token("rs256"), 1.0).unwrap()["sub"], "vector");
        assert_eq!(jwt.verify(&token("es256"), 1.0).unwrap()["sub"], "vector");
        // An HMAC secret alongside a kid-bearing JWKS still verifies HMAC
        // tokens that carry their own kid.
        let path = std::env::temp_dir().join(format!("bessemer-mixed-{}.json", std::process::id()));
        std::fs::write(&path, vectors["jwks"].to_string()).unwrap();
        let mixed = JwtAuth::parse(
            &json!({"algorithms": ["HS256", "RS256"], "secret_env": "S", "jwks_file": path.to_str().unwrap()}),
            |_| Ok(SECRET.to_vec()),
        )
        .unwrap();
        std::fs::remove_file(&path).unwrap();
        let hmac_token = hs256(
            &json!({"alg": "HS256", "kid": "hmac-1"}),
            &json!({"exp": 2000}),
        );
        assert!(mixed.verify(hmac_token.as_bytes(), 1.0).is_ok());
        assert!(mixed.verify(&token("rs256"), 1.0).is_ok());
        // The RSA key declares RS256, so a valid PSS signature is still refused.
        assert!(jwt.verify(&token("ps256_on_rs256_key"), 1.0).is_err());
        let mut tampered = token("es256");
        let last = tampered.len() - 2;
        tampered[last] = if tampered[last] == b'A' { b'B' } else { b'A' };
        assert!(jwt.verify(&tampered, 1.0).is_err());
    }
}

#[cfg(test)]
mod bench {
    use super::*;
    use serde_json::json;

    /// `cargo test --release --lib jwt::bench -- --ignored --nocapture`
    #[test]
    #[ignore]
    fn verification_cost() {
        let vectors: Value =
            serde_json::from_str(include_str!("../tests/fixtures/jwt_vectors.json")).unwrap();
        let path = std::env::temp_dir().join(format!("bessemer-bench-{}.json", std::process::id()));
        std::fs::write(&path, vectors["jwks"].to_string()).unwrap();
        for cache_size in [0, 4096] {
            let jwt = JwtAuth::parse(
                &json!({"algorithms": ["RS256", "ES256"], "jwks_file": path.to_str().unwrap(), "cache_size": cache_size}),
                |_| unreachable!(),
            )
            .unwrap();
            for name in ["rs256", "es256"] {
                let parts: Vec<&str> = vectors[name]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|part| part.as_str().unwrap())
                    .collect();
                let token = parts.join(".");
                let token = token.as_bytes();
                let rounds = 20_000;
                let start = std::time::Instant::now();
                for _ in 0..rounds {
                    std::hint::black_box(jwt.verify(token, 1.0).unwrap());
                }
                println!(
                    "{name} cache_size={cache_size}: {:.2} us/verify",
                    start.elapsed().as_secs_f64() * 1e6 / rounds as f64
                );
            }
        }
        std::fs::remove_file(&path).unwrap();
    }
}
