//! The `auth` tool: its verbs as plain functions over [`AuthStore`], each behind its caller gate.
//!
//! Any plugin may call any tool, so a verb that hands out this node's secrets names the plugins
//! it admits, from the call frame and not only from `as`. A call that crossed an edge carries an
//! `as` stamp, and none of the gated verbs admits one.

use std::collections::BTreeSet;

use awm_plugin_sdk::VerbError;
use awm_pycompat::repr::str_repr;
use serde_json::{json, Map, Value};

use crate::gate::{DeviceEntry, Material, MaterialSource, Used};
use crate::store::{AuthStore, AuthStoreError, DeviceRow};
use crate::tokens::{self, py_str, truthy, SCOPES};

pub const TOOL: &str = "auth";

/// The plugins that may read `edge_material` or mint, list or revoke a device token: none. The
/// in-process edge reads the material by a direct call.
pub const NO_PLUGINS: [&str; 0] = [];

const HOUR: f64 = 3600.0;
const DAY: f64 = 86400.0;
const LABEL_MAX: usize = 64;

/// Who is calling: the frame's `as` stamp and its calling plugin.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Caller<'a> {
    pub as_: Option<&'a str>,
    pub plugin: Option<&'a str>,
}

impl Caller<'static> {
    /// A call from this host's own CLI or service, with no plugin.
    pub const OPERATOR: Caller<'static> = Caller { as_: None, plugin: None };
}

pub struct VerbSpec {
    pub name: &'static str,
    pub description: &'static str,
    pub params: Value,
    /// Off the MCP surface: every auth verb is CLI and HTTP only.
    pub hidden: bool,
}

/// Every verb of the manifest, in Python's order. `config_get` and `config_set` are the
/// settings contract's, outside the manifest (see [`config_fragment`]).
pub fn verbs() -> Vec<VerbSpec> {
    let v = |name, description, params| VerbSpec { name, description, params, hidden: true };
    vec![
        v(
            "edge_material",
            "Signing secret + live device-token digests + session-lifetime knobs, for the httpsfront edge to enforce auth \
             offline. device_used reports the device tokens the edge accepted since its last fetch; token_scopes names the \
             scopes the edge enforces, and a scoped token goes only to an edge that names its scope.",
            json!([{"name": "device_used", "type": "object"}, {"name": "token_scopes", "type": "array"}]),
        ),
        v("status", "Report the session lifetime and the number of live device tokens.", json!([])),
        v(
            "device_mint",
            "Mint a revocable operator device token, for an unattended device or app that must not store a password. The \
             token is returned once. Operator only. scope narrows the token to a few exact request shapes: avatar-stage \
             admits only the avatar stage session (the WebSocket /hub/session/avatar/stage).",
            json!([{"name": "label", "type": "string", "required": true}, {"name": "scope", "type": "string"}]),
        ),
        v("device_list", "List device tokens (no secrets). Operator only.", json!([])),
        v(
            "device_revoke",
            "Revoke a device token by id. The edge stops honouring it within its ~30 s material refresh. Operator only.",
            json!([{"name": "id", "type": "string", "required": true}]),
        ),
    ]
}

/// The ready api's `config` block: the settings page's title and schema.
pub fn config_fragment(store: &AuthStore) -> Value {
    store.contract().manifest_fragment()
}

fn runtime(e: AuthStoreError) -> VerbError {
    VerbError::runtime(e.to_string())
}

/// Refuse a call that arrived through an edge listener.
fn operator_only(caller: Caller<'_>, verb: &str) -> Result<(), VerbError> {
    if caller.as_.is_some() {
        return Err(VerbError::permission(format!("{verb} is an operator verb: run it on the host, not through an edge")));
    }
    Ok(())
}

/// Refuse a call from any plugin outside `allowed`. A call that names no plugin passes.
fn plugins_only(caller: Caller<'_>, verb: &str, allowed: &[&str]) -> Result<(), VerbError> {
    match caller.plugin {
        Some(p) if !p.is_empty() && !allowed.contains(&p) => {
            Err(VerbError::permission(format!("{verb} is not open to plugin {}", str_repr(p))))
        }
        _ => Ok(()),
    }
}

/// `str(args.get(key) or "").strip()`.
fn str_arg(args: &Value, key: &str) -> String {
    match args.get(key) {
        Some(v) if truthy(v) => awm_pycompat::pystr::strip(&py_str(v)).to_string(),
        _ => String::new(),
    }
}

/// The edge's `{id: epoch seconds}` report: number values only, anything else ignored.
fn device_used(v: Option<&Value>) -> Used {
    let Some(Value::Object(m)) = v else { return Used::new() };
    m.iter().filter_map(|(k, v)| v.as_f64().map(|f| (k.clone(), f))).collect()
}

/// The material an edge that enforces `scopes` may hold. A scoped token goes only to an edge that
/// names its scope, because one that cannot read the scope would honour the token everywhere.
pub fn material(store: &AuthStore, used: &Used, scopes: &BTreeSet<String>) -> Result<Material, AuthStoreError> {
    store.device_touch(used)?;
    let s = store.settings()?;
    let secret = store.secret()?;
    let device_tokens = store
        .device_live()?
        .into_iter()
        .filter_map(|t| {
            let scope = t.scope.filter(|s| !s.is_empty());
            if let Some(sc) = &scope {
                if !scopes.contains(sc) {
                    return None;
                }
            }
            Some(DeviceEntry { id: t.id, sub: t.username, sha256: t.secret_sha256, scope })
        })
        .collect();
    Ok(Material {
        secret,
        device_tokens,
        session_ttl_seconds: s.session_ttl_hours * HOUR,
        max_session_seconds: s.max_session_days * DAY,
    })
}

/// The in-process edge's source: every scope this binary enforces.
impl MaterialSource for AuthStore {
    fn fetch(&self, device_used: &Used) -> anyhow::Result<Material> {
        let scopes = SCOPES.iter().map(|s| s.to_string()).collect();
        Ok(material(self, device_used, &scopes)?)
    }
}

/// `edge_material {device_used?, token_scopes?}`.
pub fn edge_material(store: &AuthStore, args: &Value, caller: Caller<'_>) -> Result<Value, VerbError> {
    operator_only(caller, "edge_material")?;
    plugins_only(caller, "edge_material", &NO_PLUGINS)?;
    let used = device_used(args.get("device_used"));
    let scopes: BTreeSet<String> = match args.get("token_scopes") {
        Some(Value::Array(a)) => a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect(),
        _ => BTreeSet::new(),
    };
    let mat = material(store, &used, &scopes).map_err(runtime)?;
    serde_json::to_value(mat).map_err(|e| VerbError::runtime(e.to_string()))
}

/// `status`: the session lifetime and the number of live device tokens.
pub fn status(store: &AuthStore) -> Result<Value, VerbError> {
    let s = store.settings().map_err(runtime)?;
    let live = store.device_live().map_err(runtime)?.len();
    Ok(json!({"session_ttl_hours": s.session_ttl_hours, "max_session_days": s.max_session_days, "device_tokens": live}))
}

/// A token as listings show it: never the digest.
pub fn public_device(t: &DeviceRow) -> Map<String, Value> {
    let Value::Object(m) = json!({
        "id": t.id, "username": t.username, "label": t.label, "created_at": t.created_at,
        "last_used_at": t.last_used_at, "revoked_at": t.revoked_at, "scope": t.scope,
    }) else {
        unreachable!("a json! object")
    };
    m
}

/// `device_mint {label, scope}`: a token that speaks for one device, as `device:<id>`, never
/// for the operator. The token is returned once, here.
pub fn device_mint(store: &AuthStore, args: &Value, caller: Caller<'_>, now: f64) -> Result<Value, VerbError> {
    operator_only(caller, "device_mint")?;
    plugins_only(caller, "device_mint", &NO_PLUGINS)?;
    let label = str_arg(args, "label");
    if label.is_empty() {
        return Err(VerbError::value("label is required"));
    }
    if label.chars().count() > LABEL_MAX {
        return Err(VerbError::value(format!("label is longer than {LABEL_MAX} characters")));
    }
    let scope = str_arg(args, "scope");
    if !SCOPES.contains(&scope.as_str()) {
        return Err(VerbError::value(format!("a device token needs a scope; known: {}", SCOPES.join(", "))));
    }
    let minted = tokens::mint();
    let row = store
        .device_insert(&minted.id, &format!("device:{}", minted.id), &label, &tokens::digest(&minted.secret), Some(&scope), now)
        .map_err(runtime)?;
    tracing::warn!("auth: minted device token {} ({label}, scope {scope})", minted.id);
    let mut out = public_device(&row);
    out.insert("token".into(), json!(minted.token));
    Ok(Value::Object(out))
}

/// `device_list`: every device token, revoked ones included.
pub fn device_list(store: &AuthStore, caller: Caller<'_>) -> Result<Value, VerbError> {
    operator_only(caller, "device_list")?;
    plugins_only(caller, "device_list", &NO_PLUGINS)?;
    let rows = store.device_list().map_err(runtime)?;
    Ok(json!({"tokens": rows.iter().map(public_device).collect::<Vec<_>>()}))
}

/// `device_revoke {id}`. The edge drops the token on its next refresh.
pub fn device_revoke(store: &AuthStore, args: &Value, caller: Caller<'_>, now: f64) -> Result<Value, VerbError> {
    operator_only(caller, "device_revoke")?;
    plugins_only(caller, "device_revoke", &NO_PLUGINS)?;
    let id = str_arg(args, "id");
    if id.is_empty() {
        return Err(VerbError::value("id is required"));
    }
    if store.device_get(&id).map_err(runtime)?.is_none() {
        return Err(VerbError::value(format!("no such device token {}", str_repr(&id))));
    }
    store.device_revoke(&id, now).map_err(runtime)?;
    let row = store.device_get(&id).map_err(runtime)?.ok_or_else(|| VerbError::value(format!("no such device token {}", str_repr(&id))))?;
    tracing::warn!("auth: revoked device token {id} of {}", row.username);
    Ok(Value::Object(public_device(&row)))
}

/// `config_get`: the settings contract and its live values.
pub fn config_get(store: &AuthStore) -> Result<Value, VerbError> {
    store.contract().get().map_err(|e| VerbError::value(e.to_string()))
}

/// `config_set {values}`: merge, validate and store, then answer as `config_get`.
pub fn config_set(store: &AuthStore, args: &Value) -> Result<Value, VerbError> {
    store.contract().set(args).map_err(|e| VerbError::value(e.to_string()))
}

/// Run `verb`, one of [`verbs`] or `config_get`/`config_set`, at wall-clock `now`.
pub fn call(store: &AuthStore, verb: &str, args: &Value, caller: Caller<'_>, now: f64) -> Result<Value, VerbError> {
    match verb {
        "edge_material" => edge_material(store, args, caller),
        "status" => status(store),
        "device_mint" => device_mint(store, args, caller, now),
        "device_list" => device_list(store, caller),
        "device_revoke" => device_revoke(store, args, caller, now),
        "config_get" => config_get(store),
        "config_set" => config_set(store, args),
        other => Err(VerbError::value(format!("auth has no verb {}", str_repr(other)))),
    }
}
