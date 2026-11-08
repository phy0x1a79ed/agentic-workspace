//! The Globus Transfer API v0.10, natively: the submission id, the transfer document, a
//! task's status and its skipped errors. It replaces the `globus` CLI.
//!
//! Submit, never block: a task runs for minutes to hours, so a verb returns the task id and
//! the `task` verb or the watcher polls it. The submission id is fetched per call and never
//! reused. It is Globus's idempotency token, and a replay returns the original task.
//!
//! The token: on first use the plugin imports the CLI's refresh token, read-only, from its
//! `storage.db` into `services/dvc/globus-tokens.json` (0600), with the CLI's client, and
//! refreshes the access token from then on. `dvc globus-login` mints a fresh one.

use std::fs;
use std::io::Write;
use std::os::fd::AsRawFd;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::now;

pub const TRANSFER_URL: &str = "https://transfer.api.globus.org/v0.10";
pub const AUTH_URL: &str = "https://auth.globus.org";
pub const TRANSFER_RS: &str = "transfer.api.globus.org";
pub const TRANSFER_SCOPE: &str = "urn:globus:auth:scope:transfer.api.globus.org:all";
/// The globus CLI's native app, for `globus-login` on a node the CLI never logged in on.
pub const NATIVE_CLIENT_ID: &str = "95fdeba8-fac2-42bd-a357-e068d82ff78e";
pub const AUTH_CODE_REDIRECT: &str = "https://auth.globus.org/v2/web/auth-code";
pub const TOKEN_FILE: &str = "globus-tokens.json";

/// Terminal task states. ACTIVE is not one: an ACTIVE task with faults is still retrying.
pub const TERMINAL: [&str; 2] = ["SUCCEEDED", "FAILED"];

/// The task fields `task_status` reports, as the CLI's `task show` selected them.
pub const STATUS_FIELDS: [&str; 11] = [
    "status",
    "label",
    "files",
    "files_transferred",
    "files_skipped",
    "bytes_transferred",
    "effective_bytes_per_second",
    "faults",
    "nice_status",
    "request_time",
    "completion_time",
];
const INT_FIELDS: [&str; 6] = ["files", "files_transferred", "files_skipped", "bytes_transferred", "effective_bytes_per_second", "faults"];

#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct GlobusError(pub String);

pub type Result<T> = std::result::Result<T, GlobusError>;

fn gerr(m: impl std::fmt::Display) -> GlobusError {
    GlobusError(m.to_string())
}

/// The two calls the Transfer API needs. The live client and the tests' fake implement it.
pub trait Api: Send + Sync {
    fn get(&self, path: &str) -> Result<Value>;
    fn post(&self, path: &str, body: &Value) -> Result<Value>;
}

#[derive(Debug, Clone, PartialEq)]
pub struct Submit<'a> {
    pub source: &'a str,
    pub destination: &'a str,
    pub label: &'a str,
    pub sync_level: i64,
    pub delete_destination_extra: bool,
    pub skip_source_errors: bool,
    pub filter_rules: Option<Vec<Value>>,
}

/// The transfer document, with a submission id fetched for this call.
pub fn document(submission_id: &str, items: &[Value], s: &Submit<'_>) -> Value {
    let label: String = s.label.chars().take(128).collect();
    let mut body = json!({
        "DATA_TYPE": "transfer",
        "submission_id": submission_id,
        "source_endpoint": s.source,
        "destination_endpoint": s.destination,
        "label": label,
        "sync_level": s.sync_level,
        "verify_checksum": true,
        "delete_destination_extra": s.delete_destination_extra,
        "skip_source_errors": s.skip_source_errors,
        "DATA": items,
    });
    if let Some(rules) = s.filter_rules.as_ref().filter(|r| !r.is_empty()) {
        body["filter_rules"] = json!(rules);
    }
    body
}

/// Submit a transfer document and return its task id.
pub fn submit(api: &dyn Api, items: &[Value], s: &Submit<'_>) -> Result<String> {
    let sid = api.get("/submission_id")?;
    let sid = sid.get("value").and_then(Value::as_str).ok_or_else(|| gerr("Globus returned no submission id"))?.to_string();
    let res = api.post("/transfer", &document(&sid, items, s))?;
    let task = res.get("task_id").and_then(Value::as_str).filter(|t| !t.is_empty()).ok_or_else(|| gerr(format!("Globus accepted the transfer but returned no task id: {res}")))?;
    tracing::info!("submitted {task} ({} items, label={:?})", items.len(), s.label);
    Ok(task.to_string())
}

/// One task field as the CLI's unix output and Python's parse gave it: an empty value is
/// null, a counter is an integer.
fn field(doc: &Value, name: &str) -> Value {
    let v = doc.get(name).cloned().unwrap_or(Value::Null);
    let as_text = match &v {
        Value::Null => return Value::Null,
        Value::String(s) if s.is_empty() || s == "None" => return Value::Null,
        Value::String(s) => s.clone(),
        Value::Bool(b) => if *b { "True" } else { "False" }.to_string(),
        other => other.to_string(),
    };
    if INT_FIELDS.contains(&name) {
        if let Some(i) = v.as_i64() {
            return json!(i);
        }
        if let Ok(i) = as_text.trim().parse::<i64>() {
            return json!(i);
        }
    }
    Value::String(as_text)
}

/// A task's state as the fields that tell anything, plus `done`.
pub fn status_of(task_id: &str, doc: &Value) -> Value {
    let mut out = Map::new();
    out.insert("task_id".into(), json!(task_id));
    for f in STATUS_FIELDS {
        out.insert(f.into(), field(doc, f));
    }
    let done = out.get("status").and_then(Value::as_str).is_some_and(|s| TERMINAL.contains(&s));
    out.insert("done".into(), json!(done));
    Value::Object(out)
}

pub fn task_status(api: &dyn Api, task_id: &str) -> Result<Value> {
    let doc = api.get(&format!("/task/{}", enc(task_id)))?;
    Ok(status_of(task_id, &doc))
}

/// The paths a task passed over because it could not read their source. A backup whose
/// whole source was unreadable ends SUCCEEDED with every counter at 0, and only this list
/// says so.
pub fn skipped_errors(api: &dyn Api, task_id: &str) -> Result<Vec<Value>> {
    let mut out = vec![];
    let mut marker: Option<String> = None;
    for _ in 0..1000 {
        let path = match &marker {
            None => format!("/task/{}/skipped_errors", enc(task_id)),
            Some(m) => format!("/task/{}/skipped_errors?marker={}", enc(task_id), enc(m)),
        };
        let page = api.get(&path)?;
        if let Some(d) = page.get("DATA").and_then(Value::as_array) {
            out.extend(d.iter().cloned());
        }
        match page.get("next_marker") {
            Some(Value::String(m)) if !m.is_empty() => marker = Some(m.clone()),
            Some(Value::Number(n)) => marker = Some(n.to_string()),
            _ => break,
        }
    }
    Ok(out)
}

/// Block until the task ends or `timeout` seconds pass. A timeout is `timed_out: true`, not
/// an error: still running is a normal answer for a multi-hour mirror.
pub fn wait(api: &dyn Api, task_id: &str, timeout: u64, poll: u64) -> Result<Value> {
    let start = Instant::now();
    let budget = Duration::from_secs(timeout);
    loop {
        let st = task_status(api, task_id)?;
        let done = st["done"].as_bool().unwrap_or(false);
        let elapsed = start.elapsed();
        if done || elapsed >= budget {
            let mut st = st;
            st["timed_out"] = json!(!done);
            return Ok(st);
        }
        std::thread::sleep(Duration::from_secs(poll.max(1)).min(budget - elapsed));
    }
}

fn enc(s: &str) -> String {
    let mut o = String::new();
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) {
            o.push(b as char);
        } else {
            o.push_str(&format!("%{b:02X}"));
        }
    }
    o
}

// -- tokens ------------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Tokens {
    pub client_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client_secret: Option<String>,
    pub refresh_token: String,
    #[serde(default)]
    pub access_token: String,
    #[serde(default)]
    pub expires_at_seconds: f64,
    #[serde(default)]
    pub source: String,
}

/// Where the globus CLI keeps its login: `$GLOBUS_CLI_STORAGE`, else `~/.globus/cli/storage.db`.
pub fn cli_storage() -> PathBuf {
    if let Some(p) = std::env::var_os("GLOBUS_CLI_STORAGE").filter(|p| !p.is_empty()) {
        return PathBuf::from(p);
    }
    PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".globus/cli/storage.db")
}

/// Read the CLI's transfer refresh token and its client, opening the database read-only.
pub fn import_from_cli(storage: &Path) -> Result<Tokens> {
    if !storage.is_file() {
        return Err(gerr(format!("no globus CLI login at {}", storage.display())));
    }
    let conn = rusqlite::Connection::open_with_flags(storage, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX)
        .map_err(|e| gerr(format!("{}: {e}", storage.display())))?;
    let q = |sql: &str| -> Vec<(String, String)> {
        conn.prepare(sql)
            .and_then(|mut st| st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?.collect())
            .unwrap_or_default()
    };
    let tokens = q(&format!("SELECT namespace, token_data_json FROM token_storage WHERE resource_server = '{TRANSFER_RS}'"));
    let (ns, raw) = tokens
        .iter()
        .find(|(ns, _)| ns == "userprofile/production")
        .or_else(|| tokens.first())
        .ok_or_else(|| gerr(format!("{} holds no {TRANSFER_RS} token: run `globus login` or `dvc globus-login`", storage.display())))?;
    let tok: Value = serde_json::from_str(raw).map_err(|e| gerr(format!("{}: token: {e}", storage.display())))?;
    let refresh = tok.get("refresh_token").and_then(Value::as_str).filter(|s| !s.is_empty()).ok_or_else(|| gerr(format!("{}: the transfer token has no refresh token", storage.display())))?;
    let client = q(&format!("SELECT namespace, config_data_json FROM config_storage WHERE config_name = 'auth_client_data' AND namespace = '{}'", ns.replace('\'', "''")))
        .into_iter()
        .next()
        .and_then(|(_, raw)| serde_json::from_str::<Value>(&raw).ok());
    let client_id = client.as_ref().and_then(|c| c.get("client_id")).and_then(Value::as_str).map(str::to_string).unwrap_or_else(|| NATIVE_CLIENT_ID.to_string());
    let client_secret = client.as_ref().and_then(|c| c.get("client_secret")).and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_string);
    Ok(Tokens {
        client_id,
        client_secret,
        refresh_token: refresh.to_string(),
        access_token: tok.get("access_token").and_then(Value::as_str).unwrap_or("").to_string(),
        expires_at_seconds: tok.get("expires_at_seconds").and_then(Value::as_f64).unwrap_or(0.0),
        source: format!("imported from {}", storage.display()),
    })
}

/// The token file and the auth server it refreshes against.
pub struct TokenStore {
    pub path: PathBuf,
    pub auth_url: String,
    pub cli_storage: PathBuf,
    http: reqwest::blocking::Client,
}

fn http() -> reqwest::blocking::Client {
    reqwest::blocking::Client::builder().timeout(Duration::from_secs(60)).user_agent("awm-dvc").build().unwrap_or_default()
}

impl TokenStore {
    pub fn new(state_dir: &Path) -> Self {
        Self {
            path: state_dir.join(TOKEN_FILE),
            auth_url: std::env::var("AWM_DVC_GLOBUS_AUTH_URL").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| AUTH_URL.into()),
            cli_storage: cli_storage(),
            http: http(),
        }
    }

    fn lock(&self) -> Result<fs::File> {
        let dir = self.path.parent().unwrap_or(Path::new("."));
        fs::create_dir_all(dir).map_err(gerr)?;
        let f = fs::OpenOptions::new().create(true).truncate(false).write(true).mode(0o600).open(dir.join("globus-tokens.lock")).map_err(gerr)?;
        // SAFETY: flock on a descriptor we own; released when the file closes.
        if unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX) } != 0 {
            return Err(gerr(std::io::Error::last_os_error()));
        }
        Ok(f)
    }

    pub fn read(&self) -> Result<Option<Tokens>> {
        match fs::read_to_string(&self.path) {
            Ok(t) => serde_json::from_str(&t).map(Some).map_err(|e| gerr(format!("{}: {e}", self.path.display()))),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(gerr(format!("{}: {e}", self.path.display()))),
        }
    }

    pub fn write(&self, t: &Tokens) -> Result<()> {
        let tmp = self.path.with_extension("json.tmp");
        {
            let mut f = fs::OpenOptions::new().create(true).truncate(true).write(true).mode(0o600).open(&tmp).map_err(gerr)?;
            f.write_all(serde_json::to_string_pretty(t).map_err(gerr)?.as_bytes()).map_err(gerr)?;
            f.sync_all().map_err(gerr)?;
        }
        fs::set_permissions(&tmp, fs::Permissions::from_mode(0o600)).map_err(gerr)?;
        fs::rename(&tmp, &self.path).map_err(gerr)
    }

    /// Import the CLI's login when no token file exists. Returns whether it imported.
    pub fn ensure_imported(&self) -> Result<bool> {
        let _l = self.lock()?;
        if self.read()?.is_some() {
            return Ok(false);
        }
        let t = import_from_cli(&self.cli_storage)?;
        self.write(&t)?;
        tracing::info!("dvc: imported the globus CLI's transfer token into {}", self.path.display());
        Ok(true)
    }

    fn post_token(&self, t: &Tokens, form: &[(&str, &str)]) -> Result<Value> {
        let mut req = self.http.post(format!("{}/v2/oauth2/token", self.auth_url.trim_end_matches('/')));
        let mut form: Vec<(&str, &str)> = form.to_vec();
        match &t.client_secret {
            Some(s) => req = req.basic_auth(&t.client_id, Some(s)),
            None => form.push(("client_id", &t.client_id)),
        }
        let resp = req.form(&form).send().map_err(|e| gerr(format!("Globus auth: {e}")))?;
        let status = resp.status();
        let body: Value = resp.json().unwrap_or(Value::Null);
        if !status.is_success() {
            let why = body.get("error_description").or_else(|| body.get("error")).cloned().unwrap_or(Value::Null);
            return Err(gerr(format!("Globus auth refused the token ({status}): {why}. Run `dvc globus-login`")));
        }
        Ok(body)
    }

    fn take_tokens(t: &mut Tokens, body: &Value) -> Result<()> {
        let mut all = vec![body.clone()];
        if let Some(o) = body.get("other_tokens").and_then(Value::as_array) {
            all.extend(o.iter().cloned());
        }
        let tok = all
            .iter()
            .find(|x| x.get("resource_server").and_then(Value::as_str) == Some(TRANSFER_RS))
            .or_else(|| all.first().filter(|x| x.get("resource_server").is_none()))
            .ok_or_else(|| gerr("Globus auth returned no transfer token"))?;
        t.access_token = tok.get("access_token").and_then(Value::as_str).unwrap_or("").to_string();
        t.expires_at_seconds = now() + tok.get("expires_in").and_then(Value::as_f64).unwrap_or(3600.0);
        if let Some(r) = tok.get("refresh_token").and_then(Value::as_str).filter(|r| !r.is_empty()) {
            t.refresh_token = r.to_string();
        }
        if t.access_token.is_empty() {
            return Err(gerr("Globus auth returned an empty access token"));
        }
        Ok(())
    }

    /// A live access token, refreshed when it expires within a minute or `force` is set.
    pub fn access_token(&self, force: bool) -> Result<String> {
        let _l = self.lock()?;
        let mut t = match self.read()? {
            Some(t) => t,
            None => {
                let t = import_from_cli(&self.cli_storage).map_err(|e| gerr(format!("no Globus token in {}: {e}", self.path.display())))?;
                self.write(&t)?;
                t
            }
        };
        if force || t.access_token.is_empty() || t.expires_at_seconds - 60.0 < now() {
            let refresh = t.refresh_token.clone();
            let body = self.post_token(&t, &[("grant_type", "refresh_token"), ("refresh_token", &refresh)])?;
            Self::take_tokens(&mut t, &body)?;
            self.write(&t)?;
        }
        Ok(t.access_token)
    }

    /// Finish a login: trade an authorization code (with its PKCE verifier) for tokens.
    pub fn exchange_code(&self, client_id: &str, code: &str, verifier: &str) -> Result<Tokens> {
        let mut t = Tokens { client_id: client_id.into(), client_secret: None, refresh_token: String::new(), access_token: String::new(), expires_at_seconds: 0.0, source: "dvc globus-login".into() };
        let body = self.post_token(&t, &[("grant_type", "authorization_code"), ("code", code), ("redirect_uri", AUTH_CODE_REDIRECT), ("code_verifier", verifier)])?;
        Self::take_tokens(&mut t, &body)?;
        if t.refresh_token.is_empty() {
            return Err(gerr("Globus auth returned no refresh token"));
        }
        let _l = self.lock()?;
        self.write(&t)?;
        Ok(t)
    }
}

/// The live client.
pub struct Native {
    pub base: String,
    pub tokens: TokenStore,
    http: reqwest::blocking::Client,
}

impl Native {
    pub fn new(state_dir: &Path) -> Self {
        Self {
            base: std::env::var("AWM_DVC_GLOBUS_TRANSFER_URL").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| TRANSFER_URL.into()),
            tokens: TokenStore::new(state_dir),
            http: http(),
        }
    }

    fn send(&self, method: reqwest::Method, path: &str, body: Option<&Value>) -> Result<Value> {
        let url = format!("{}{path}", self.base.trim_end_matches('/'));
        for attempt in 0..2 {
            let token = self.tokens.access_token(attempt > 0)?;
            let mut req = self.http.request(method.clone(), &url).bearer_auth(token);
            if let Some(b) = body {
                req = req.json(b);
            }
            let resp = req.send().map_err(|e| gerr(format!("Globus {method} {path}: {e}")))?;
            let status = resp.status();
            if status.as_u16() == 401 && attempt == 0 {
                continue;
            }
            let text = resp.text().unwrap_or_default();
            let v: Value = serde_json::from_str(&text).unwrap_or(Value::String(text));
            if !status.is_success() {
                let why = v.get("message").or_else(|| v.get("code")).cloned().unwrap_or(v.clone());
                return Err(gerr(format!("Globus {method} {path} failed ({status}): {why}")));
            }
            return Ok(v);
        }
        Err(gerr(format!("Globus {method} {path}: the token was refused after a refresh. Run `dvc globus-login`")))
    }
}

impl Api for Native {
    fn get(&self, path: &str) -> Result<Value> {
        self.send(reqwest::Method::GET, path, None)
    }
    fn post(&self, path: &str, body: &Value) -> Result<Value> {
        self.send(reqwest::Method::POST, path, Some(body))
    }
}

/// A PKCE verifier and its S256 challenge.
pub fn pkce() -> (String, String) {
    use base64::Engine;
    use rand::RngCore;
    use sha2::Digest;
    let mut raw = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut raw);
    let b64 = base64::engine::general_purpose::URL_SAFE_NO_PAD;
    let verifier = b64.encode(raw);
    let challenge = b64.encode(sha2::Sha256::digest(verifier.as_bytes()));
    (verifier, challenge)
}

pub fn authorize_url(auth_url: &str, client_id: &str, challenge: &str) -> String {
    format!(
        "{}/v2/oauth2/authorize?client_id={}&redirect_uri={}&scope={}&response_type=code&access_type=offline&code_challenge={}&code_challenge_method=S256&prompt=login",
        auth_url.trim_end_matches('/'),
        enc(client_id),
        enc(AUTH_CODE_REDIRECT),
        enc(TRANSFER_SCOPE),
        enc(challenge)
    )
}
