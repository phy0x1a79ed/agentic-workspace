//! The edge's credential gate, with no HTTP in it: the front hands it header values and a
//! request shape, and it answers with a [`Decision`].
//!
//! The gate never stores credential state. It caches auth's [`Material`] (the signing secret,
//! the live device-token digests and the session-lifetime knobs) for up to 30 s, and then:
//!
//! * verifies and slides the session cookie offline with the secret;
//! * checks an `awmut1.` device token against the live digests. Such a bearer is settled on its
//!   own branch: one that fails is refused, never a fall-back to the cookie. A scoped token must
//!   also match the request's shape, and a caller that passes no shape never admits one.
//!
//! The device tokens it accepted are reported to auth on its next material fetch, which feeds
//! `last_used_at`. With no material at all the gate admits nothing.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::tokens::{self, session_mint, session_sat, session_sub, session_verify};
pub use crate::tokens::Shape;

pub const COOKIE_NAME: &str = "awm_session";
/// The readable twin of the session cookie: the signed-in name, for the pages' user chip. It
/// carries no authority.
pub const AS_COOKIE_NAME: &str = "awm_as";
/// How long fetched material stays fresh.
pub const REFRESH_INTERVAL: Duration = Duration::from_secs(30);
/// The session lifetime when the material names none.
pub const DEFAULT_SESSION_TTL: f64 = 86400.0;

/// One live device token as the edge sees it. `scope` is set only for a scoped token.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DeviceEntry {
    pub id: String,
    #[serde(default)]
    pub sub: String,
    #[serde(default)]
    pub sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scope: Option<String>,
}

/// What auth hands the edge, in the field order of `edge_material`'s answer.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Material {
    pub secret: String,
    #[serde(default)]
    pub device_tokens: Vec<DeviceEntry>,
    #[serde(default)]
    pub session_ttl_seconds: f64,
    #[serde(default)]
    pub max_session_seconds: f64,
}

/// Device-token use, `{id: epoch seconds}`.
pub type Used = BTreeMap<String, f64>;

/// Where the gate gets its material. `device_used` is the use to report since the last
/// successful fetch.
pub trait MaterialSource: Send + Sync {
    fn fetch(&self, device_used: &Used) -> anyhow::Result<Material>;
}

/// The gate's answer for one request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    /// Unauthenticated: a sign-in redirect or a 401, as the front decides.
    Refused,
    /// A live device token, acting as `sub`. Sets no cookie.
    Device { id: String, sub: String },
    /// A valid session cookie. `refreshed` is the slid token to set again, or `None` once the
    /// session has passed its age ceiling and only runs out its own `exp`.
    Session { sub: String, refreshed: Option<String> },
}

impl Decision {
    pub fn ok(&self) -> bool {
        !matches!(self, Decision::Refused)
    }

    pub fn sub(&self) -> Option<&str> {
        match self {
            Decision::Refused => None,
            Decision::Device { sub, .. } | Decision::Session { sub, .. } => Some(sub),
        }
    }

    pub fn refreshed(&self) -> Option<&str> {
        match self {
            Decision::Session { refreshed: Some(t), .. } => Some(t),
            _ => None,
        }
    }

    /// Python's `(ok, refreshed_cookie_or_None, sub)`.
    pub fn as_tuple(&self) -> (bool, Option<&str>, Option<&str>) {
        (self.ok(), self.refreshed(), self.sub())
    }
}

/// The token of an `Authorization: Bearer <t>` header.
pub fn bearer_of(authorization: Option<&str>) -> Option<&str> {
    let h = awm_pycompat::pystr::lstrip(authorization?);
    let end = h.find(awm_pycompat::chars::is_space)?;
    let (scheme, rest) = h.split_at(end);
    let token = awm_pycompat::pystr::strip(rest);
    if token.is_empty() || awm_pycompat::pystr::lower(scheme) != "bearer" {
        return None;
    }
    Some(token)
}

/// `operator`, or a name that reads as a node (`peer`, `peer:<node>`), as opposed to a person.
pub fn is_machine_sub(sub: Option<&str>) -> bool {
    matches!(sub, Some(s) if s == "operator" || s == "peer" || s.starts_with("peer:"))
}

/// `(id, sub, scope)` of a live device token. One whose owner reads as a machine is refused: a
/// device token never carries the operator's or a node's identity.
fn device_sub<'m>(mat: &'m Material, token: &str) -> Option<(&'m str, &'m str, Option<&'m str>)> {
    let (id, secret) = tokens::parse(token).ok()?;
    let entry = mat.device_tokens.iter().find(|e| e.id == id)?;
    if !tokens::matches(secret, &entry.sha256) {
        return None;
    }
    if entry.sub.is_empty() || is_machine_sub(Some(&entry.sub)) {
        tracing::warn!("edge: device token {} names machine identity {:?}; refused", entry.id, entry.sub);
        return None;
    }
    Some((&entry.id, &entry.sub, entry.scope.as_deref().filter(|s| !s.is_empty())))
}

/// The decision for one request under `mat`, at wall-clock `now`.
pub fn decide(mat: &Material, cookie: Option<&str>, bearer: Option<&str>, shape: Option<&Shape<'_>>, now: f64) -> Decision {
    if let Some(bearer) = bearer.filter(|b| tokens::looks_like_token(b)) {
        let Some((id, sub, scope)) = device_sub(mat, bearer) else {
            return Decision::Refused;
        };
        if let Some(scope) = scope {
            if !shape.is_some_and(|s| tokens::scope_admits(scope, s)) {
                return Decision::Refused;
            }
        }
        return Decision::Device { id: id.into(), sub: sub.into() };
    }
    let Some(claims) = cookie.filter(|c| !c.is_empty()).and_then(|c| session_verify(&mat.secret, c, now)) else {
        return Decision::Refused;
    };
    let sub = session_sub(&claims);
    // Python's int() raises on such a claim, and a raise admits nothing.
    let Some(sat) = session_sat(&claims) else {
        return Decision::Refused;
    };
    let ceiling = mat.max_session_seconds;
    if ceiling == 0.0 || (now - sat as f64) < ceiling {
        let refreshed = session_mint(&mat.secret, &sub, mat.session_ttl_seconds, Some(sat), now);
        return Decision::Session { sub, refreshed: Some(refreshed) };
    }
    // Valid but past the ceiling: this request passes, and the session stops sliding.
    Decision::Session { sub, refreshed: None }
}

#[derive(Default)]
struct Cache {
    material: Option<Arc<Material>>,
    fetched_at: Option<Instant>,
}

/// Caches auth's material and answers each request's authentication question.
pub struct AuthGate {
    source: Arc<dyn MaterialSource>,
    interval: Duration,
    cache: Mutex<Cache>,
    fetching: Mutex<()>,
    used: Mutex<Used>,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|p| p.into_inner())
}

impl AuthGate {
    pub fn new(source: Arc<dyn MaterialSource>) -> AuthGate {
        AuthGate { source, interval: REFRESH_INTERVAL, cache: Mutex::default(), fetching: Mutex::new(()), used: Mutex::default() }
    }

    /// The same gate with another freshness window.
    pub fn with_interval(mut self, interval: Duration) -> AuthGate {
        self.interval = interval;
        self
    }

    fn cached(&self) -> Option<Arc<Material>> {
        let c = lock(&self.cache);
        match (&c.material, c.fetched_at) {
            (Some(m), Some(at)) if at.elapsed() < self.interval => Some(m.clone()),
            _ => None,
        }
    }

    /// The material, fetched again once it is older than the window. A failed fetch keeps the
    /// stale material, if any, and keeps the use report for the next fetch.
    pub fn material(&self) -> Option<Arc<Material>> {
        self.cached().or_else(|| self.fetch(false))
    }

    /// Fetch the material now, whatever its age.
    pub fn refresh(&self) -> Option<Arc<Material>> {
        self.fetch(true)
    }

    fn fetch(&self, force: bool) -> Option<Arc<Material>> {
        let _one = lock(&self.fetching);
        if !force {
            if let Some(m) = self.cached() {
                return Some(m);
            }
        }
        let used = self.tokens_seen();
        match self.source.fetch(&used) {
            Ok(mat) if !mat.secret.is_empty() => {
                let mut c = lock(&self.cache);
                c.material = Some(Arc::new(mat));
                c.fetched_at = Some(Instant::now());
                drop(c);
                let mut pending = lock(&self.used);
                for (id, at) in &used {
                    if pending.get(id) == Some(at) {
                        pending.remove(id);
                    }
                }
            }
            Ok(_) => tracing::warn!("edge: auth material carries no secret; keeping what is cached"),
            Err(e) => tracing::warn!("edge: could not fetch auth material: {e:#}"),
        }
        lock(&self.cache).material.clone()
    }

    /// The device tokens accepted since the last successful fetch: the report the next fetch sends.
    pub fn tokens_seen(&self) -> Used {
        lock(&self.used).clone()
    }

    /// Authenticate one request at wall-clock `now`.
    pub fn authenticate(&self, cookie: Option<&str>, bearer: Option<&str>, shape: Option<&Shape<'_>>, now: f64) -> Decision {
        let Some(mat) = self.material() else {
            return Decision::Refused;
        };
        let decision = decide(&mat, cookie, bearer, shape, now);
        if let Decision::Device { id, .. } = &decision {
            lock(&self.used).insert(id.clone(), now);
        }
        decision
    }

    /// A fresh session token for `sub`, a checked portal grant's subject, and its lifetime in
    /// whole seconds. `None` with no material.
    pub fn mint_session(&self, sub: &str, now: f64) -> Option<(String, i64)> {
        let mat = self.material()?;
        let ttl = if mat.session_ttl_seconds == 0.0 { DEFAULT_SESSION_TTL } else { mat.session_ttl_seconds };
        Some((session_mint(&mat.secret, sub, ttl, None, now), ttl.trunc() as i64))
    }

    /// The session lifetime the cookies are set for.
    pub fn session_ttl_seconds(&self) -> f64 {
        match self.material() {
            Some(m) if m.session_ttl_seconds != 0.0 => m.session_ttl_seconds,
            _ => DEFAULT_SESSION_TTL,
        }
    }
}
