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

struct Key {
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
    issuers: Vec<String>,
    audiences: Vec<String>,
    leeway: f64,
    require_exp: bool,
    pub exclude_paths: HashSet<String>,
    cache: Option<Mutex<Cache>>,
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
    let mut chunks = input.chunks_exact(4);
    for chunk in &mut chunks {
        let word = value(chunk[0])? << 18
            | value(chunk[1])? << 12
            | value(chunk[2])? << 6
            | value(chunk[3])?;
        output.extend_from_slice(&[(word >> 16) as u8, (word >> 8) as u8, word as u8]);
    }
    match *chunks.remainder() {
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
    match config.get(name) {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(Value::String(value)) if !value.is_empty() && value.len() <= 2048 => {
            Ok(vec![value.clone()])
        }
        Some(Value::Array(values)) if !values.is_empty() && values.len() <= 64 => values
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
            "jwt {name} must be a nonempty string or a list of 1 to 64 strings"
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

fn jwk_keys(document: &Value, allowed: &HashSet<Algorithm>) -> Result<Vec<Key>, String> {
    let keys = document
        .get("keys")
        .and_then(Value::as_array)
        .ok_or("jwks must be a JSON Web Key Set object with a keys array")?;
    if keys.len() > 256 {
        return Err("jwks cannot contain more than 256 keys".into());
    }
    let mut output = Vec::new();
    for jwk in keys {
        let jwk = jwk.as_object().ok_or("jwks keys must be objects")?;
        // Encryption keys and unknown key types never verify signatures.
        if jwk
            .get("use")
            .and_then(Value::as_str)
            .is_some_and(|value| value != "sig")
        {
            continue;
        }
        if let Some(operations) = jwk.get("key_ops") {
            let verifies = operations
                .as_array()
                .is_some_and(|items| items.iter().any(|item| item == "verify"));
            if !verifies {
                continue;
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
                None => continue,
            },
            _ => return Err("JWK alg must be a string".into()),
        };
        let (family, material): (Vec<Algorithm>, Material) = match jwk
            .get("kty")
            .and_then(Value::as_str)
        {
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
                    _ => continue,
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
                    continue;
                }
                let x = jwk_bytes(jwk, "x")?;
                if x.len() != 32 {
                    return Err("Ed25519 JWK x must be 32 bytes".into());
                }
                (vec![Algorithm::EdDSA], Material::Ed25519(x))
            }
            // Shared secrets belong in secret_env, never a published key set.
            _ => continue,
        };
        let algorithms: Vec<_> = family
            .into_iter()
            .filter(|algorithm| {
                allowed.contains(algorithm) && declared.is_none_or(|value| value == *algorithm)
            })
            .collect();
        if !algorithms.is_empty() {
            output.push(Key {
                kid,
                algorithms,
                material,
            });
        }
    }
    Ok(output)
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

impl JwtAuth {
    pub fn parse(
        value: &Value,
        secret: impl Fn(&str) -> Result<Vec<u8>, String>,
    ) -> Result<Self, String> {
        let config = value.as_object().ok_or("jwt must be an object")?;
        const SETTINGS: [&str; 10] = [
            "algorithms",
            "secret_env",
            "jwks_env",
            "jwks_file",
            "issuer",
            "audience",
            "leeway_seconds",
            "require_exp",
            "exclude_paths",
            "cache_size",
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
        let jwks = match (config.get("jwks_env"), config.get("jwks_file")) {
            (Some(Value::String(_)), Some(Value::String(_))) => {
                return Err("jwt accepts only one of jwks_env and jwks_file".into());
            }
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
            None if asymmetric => {
                return Err("asymmetric jwt algorithms require jwks_env or jwks_file".into());
            }
            None => {}
        }
        let issuers = text_list(config, "issuer")?;
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
        Ok(Self {
            algorithms,
            keys,
            issuers,
            audiences,
            leeway,
            require_exp,
            exclude_paths: paths.into_iter().collect(),
            cache: (cache_size > 0).then(|| {
                Mutex::new(Cache {
                    entries: HashMap::with_capacity(cache_size.min(4096)),
                    capacity: cache_size,
                })
            }),
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
        if let Some(cache) = &self.cache {
            let mut cache = cache.lock().unwrap();
            if let Some(entry) = cache.entries.get(token) {
                if now < entry.valid_until
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
        let mut candidates = self
            .keys
            .iter()
            .filter(|key| key.algorithms.contains(&algorithm));
        let verified = match kid {
            Some(kid) if self.keys.iter().any(|key| key.kid.is_some()) => candidates
                .find(|key| key.kid.as_deref() == Some(kid))
                .is_some_and(|key| key.verify(algorithm, message, &signature_bytes)),
            // Unidentified keys are tried in order; the set is bounded at 256.
            _ => candidates.any(|key| key.verify(algorithm, message, &signature_bytes)),
        };
        if !verified {
            return Err(Rejection::Invalid);
        }
        let payload = base64url(payload.as_bytes()).ok_or(Rejection::Invalid)?;
        if payload.first() != Some(&b'{') {
            return Err(Rejection::Invalid);
        }
        let claims = crate::conversion::parse_json(&payload).map_err(|_| Rejection::Invalid)?;
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
            if !issuer.is_some_and(|issuer| self.issuers.iter().any(|value| value == issuer)) {
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
        assert!(
            jwt.verify(sign(json!({"alg": "EdDSA"})).as_bytes(), 1.0)
                .is_ok()
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
        let token = |name: &str| vectors[name].as_str().unwrap().as_bytes().to_vec();
        assert_eq!(jwt.verify(&token("rs256"), 1.0).unwrap()["sub"], "vector");
        assert_eq!(jwt.verify(&token("es256"), 1.0).unwrap()["sub"], "vector");
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
                let token = vectors[name].as_str().unwrap().as_bytes();
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
