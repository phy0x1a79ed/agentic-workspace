//! Arbutus admin approvals: an agent asks through `request_human`, an admin answers in a
//! Slack thread (users.md R123). This path is Ryan's and Tony's, apart from the passkey.
//!
//! The agent names a bundle it wrote for `~/.ssh/awm-admin-approve`. The front freezes a copy
//! under `~/.ssh/admin-approvals/pending/<ref>/`, where the admin-gate hook keeps every Claude
//! session out, and posts the bundle to the approval channel. An admin's `approve` or `deny`
//! in that post's thread decides it. No verb grants an approval: only a message event from an
//! admin's own hand does, and social flags as `relayed` any message it sent for a person.

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::relay::async_trait_free::BoxFut;

pub const DEFAULT_APPROVAL_CHANNEL: &str = "slack-bot:C0C9XNR77ML";
pub const DEFAULT_APPROVAL_ADMINS: &str = "U01Q4CDSTFX=tony,UC7RUF67P=ryan";
pub const EXPIRES_S: f64 = 24.0 * 3600.0;
pub const RUN_TIMEOUT: Duration = Duration::from_secs(60);

fn env(k: &str) -> Option<String> {
    std::env::var(k).ok()
}

fn home() -> PathBuf {
    PathBuf::from(env("HOME").unwrap_or_else(|| "/".into()))
}

/// `AWM_APPROVAL_CHANNEL` is `account:channel`. Unset, #arbutus; set empty, none.
pub fn approval_channel() -> Option<(String, String)> {
    let raw = env("AWM_APPROVAL_CHANNEL").map(|s| s.trim().to_string()).unwrap_or_else(|| DEFAULT_APPROVAL_CHANNEL.into());
    let (a, c) = raw.split_once(':')?;
    (!a.is_empty() && !c.is_empty()).then(|| (a.to_string(), c.to_string()))
}

/// Platform user id -> admin name, from `AWM_APPROVAL_ADMINS` (`id=name,id=name`).
pub fn approval_admins() -> Vec<(String, String)> {
    let raw = env("AWM_APPROVAL_ADMINS").unwrap_or_else(|| DEFAULT_APPROVAL_ADMINS.into());
    raw.replace(',', " ")
        .split_whitespace()
        .filter_map(|p| p.split_once('=').map(|(u, n)| (u.to_string(), n.to_string())))
        .filter(|(u, n)| !u.is_empty() && !n.is_empty())
        .collect()
}

pub fn admin_approve_cmd() -> PathBuf {
    env("AWM_ADMIN_APPROVE").filter(|s| !s.is_empty()).map(PathBuf::from).unwrap_or_else(|| home().join(".ssh/awm-admin-approve"))
}

/// Where agents write bundles: the same `ARB_BUNDLES` the approve script reads.
pub fn approval_bundles_dir() -> PathBuf {
    env("ARB_BUNDLES").filter(|s| !s.is_empty()).map(PathBuf::from).unwrap_or_else(|| home().join(".local/share/arbutus-approvals"))
}

/// The frozen copies, under ~/.ssh/admin-approvals, where the admin-gate hook keeps sessions out.
pub fn approval_pending_dir() -> PathBuf {
    env("AWM_APPROVAL_PENDING").filter(|s| !s.is_empty()).map(PathBuf::from).unwrap_or_else(|| home().join(".ssh/admin-approvals/pending"))
}

fn sha256_file(p: &Path) -> std::io::Result<String> {
    Ok(hex::encode(Sha256::digest(std::fs::read(p)?)))
}

fn valid_name(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
}

/// The bundle's reason, minutes, ssh targets and tf changes. The approve script is the real check.
pub fn parse_bundle(text: &str) -> Result<Value, String> {
    let (mut reason, mut minutes) = (String::new(), String::new());
    let (mut ssh, mut tf) = (vec![], vec![]);
    for line in text.lines() {
        if line.trim().is_empty() || line.starts_with('#') {
            continue;
        }
        let f: Vec<&str> = line.split('\t').filter(|f| !f.is_empty()).collect();
        match (f[0], f.len()) {
            ("reason", 2) if reason.is_empty() => reason = f[1].into(),
            ("minutes", 2) if minutes.is_empty() => minutes = f[1].into(),
            ("ssh", 2) => ssh.push(f[1].to_string()),
            ("tf", 3) => tf.push(format!("{} {}", f[1], f[2])),
            _ => return Err(format!("malformed bundle line: {line:?}")),
        }
    }
    if reason.is_empty() {
        return Err("the bundle names no reason".into());
    }
    let m: Option<u32> = minutes.parse().ok().filter(|_| minutes.bytes().all(|b| b.is_ascii_digit()));
    if !m.is_some_and(|m| (1..=480).contains(&m)) {
        return Err(format!("minutes must be 1 to 480, not {minutes:?}"));
    }
    Ok(json!({"reason": reason, "minutes": minutes, "ssh": ssh, "tf": tf}))
}

fn slack(text: &str) -> String {
    text.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

pub fn request_text(req: &Value) -> String {
    let s = |k: &str| req[k].as_str().unwrap_or_default().to_string();
    let list = |k: &str| req[k].as_array().cloned().unwrap_or_default().iter().filter_map(|v| v.as_str().map(str::to_string)).collect::<Vec<_>>();
    let mut lines = vec![
        format!("*Admin approval request* `{}` from {} on {}", s("ref"), slack(&s("agent")), s("node")),
        format!("> {}", slack(&s("reason"))),
        format!("For {} minutes:", s("minutes")),
    ];
    let (ssh, tf) = (list("ssh"), list("tf"));
    lines.extend(ssh.iter().map(|t| format!("• admin ssh window on `{}`", slack(t))));
    lines.extend(tf.iter().map(|c| format!("• terraform `{}`", slack(c))));
    if ssh.is_empty() && tf.is_empty() {
        lines.push("• nothing beyond the record itself".into());
    }
    lines.push(format!("Bundle `{}`, sha256 `{}`.", s("name"), s("sha256").chars().take(12).collect::<String>()));
    lines.push("Tony or Ryan: reply `approve` or `deny` in this thread.".into());
    lines.join("\n")
}

/// The first word of a reply, lowercased and stripped to letters.
fn first_word(text: &str) -> String {
    text.split_whitespace().next().map(|w| w.to_lowercase().chars().filter(|c| c.is_ascii_lowercase()).collect()).unwrap_or_default()
}

/// The approval path's ways out: the Slack post, the approve script and the session line.
pub trait ArbutusIo: Send + Sync {
    fn post<'a>(&'a self, account: &'a str, channel: &'a str, text: &'a str, thread: Option<&'a str>, quiet: bool) -> BoxFut<'a, Result<Value, String>>;
    fn run<'a>(&'a self, name: &'a str, bundles: &'a Path, approved_by: &'a str) -> BoxFut<'a, (i32, String)>;
    fn tell<'a>(&'a self, job: &'a str, line: &'a str) -> BoxFut<'a, Result<(), String>>;
}

pub type Decided = Arc<dyn Fn(String, String) -> BoxFut<'static, ()> + Send + Sync>;

pub struct Approvals {
    io: Arc<dyn ArbutusIo>,
    lock: tokio::sync::Mutex<()>,
    on_decided: Mutex<Option<Decided>>,
    pending_dir: PathBuf,
    bundles_dir: PathBuf,
}

impl Approvals {
    pub fn new(io: Arc<dyn ArbutusIo>) -> Approvals {
        Approvals::with_dirs(io, approval_pending_dir(), approval_bundles_dir())
    }

    pub fn with_dirs(io: Arc<dyn ArbutusIo>, pending_dir: PathBuf, bundles_dir: PathBuf) -> Approvals {
        Approvals { io, lock: tokio::sync::Mutex::new(()), on_decided: Mutex::new(None), pending_dir, bundles_dir }
    }

    pub fn set_on_decided(&self, f: Decided) {
        *self.on_decided.lock().expect("decided lock") = Some(f);
    }

    fn dir(&self, r: &str) -> PathBuf {
        self.pending_dir.join(r)
    }

    fn save(&self, req: &Value) -> std::io::Result<()> {
        let path = self.dir(req["ref"].as_str().unwrap_or_default()).join("request.json");
        let tmp = path.with_extension("tmp");
        std::fs::write(&tmp, serde_json::to_string_pretty(req).unwrap_or_default())?;
        std::fs::rename(tmp, path)
    }

    fn requests(&self) -> Vec<Value> {
        let Ok(rd) = std::fs::read_dir(&self.pending_dir) else { return vec![] };
        let mut dirs: Vec<PathBuf> = rd.filter_map(Result::ok).map(|e| e.path().join("request.json")).filter(|p| p.is_file()).collect();
        dirs.sort();
        dirs.into_iter()
            .filter_map(|p| match std::fs::read_to_string(&p).map(|s| serde_json::from_str::<Value>(&s)) {
                Ok(Ok(v)) => Some(v),
                _ => {
                    tracing::warn!("operations: unreadable approval request {}", p.display());
                    None
                }
            })
            .collect()
    }

    /// Freeze the named bundle, post it, and record the request. Grants nothing.
    pub async fn open(&self, bundle: &str, agent: &str, node: &str, job: &str) -> Result<Value, String> {
        let Some((account, channel)) = approval_channel() else { return Err("this node has no approval channel: set AWM_APPROVAL_CHANNEL".into()) };
        if !valid_name(bundle) {
            return Err("bundle must name a bundle file: letters, digits, '.', '_' and '-'".into());
        }
        let src = self.bundles_dir.join(format!("{bundle}.bundle"));
        if !src.is_file() {
            return Err(format!("no bundle at {}", src.display()));
        }
        let text = std::fs::read_to_string(&src).map_err(|e| e.to_string())?;
        let parsed = parse_bundle(&text)?;
        let r = {
            use rand::RngCore;
            let mut b = [0u8; 3];
            rand::thread_rng().fill_bytes(&mut b);
            hex::encode(b)
        };
        let frozen = self.dir(&r);
        std::fs::create_dir_all(&frozen).map_err(|e| e.to_string())?;
        for d in [&frozen, &self.pending_dir] {
            let _ = std::fs::set_permissions(d, std::fs::Permissions::from_mode(0o700));
        }
        let copy = frozen.join(format!("{bundle}.bundle"));
        std::fs::copy(&src, &copy).map_err(|e| e.to_string())?;
        let mut req = json!({
            "ref": r, "name": bundle, "sha256": sha256_file(&copy).map_err(|e| e.to_string())?,
            "agent": agent, "node": node, "job": job, "task": null, "account": account, "channel": channel,
            "post_ts": "", "created": crate::now(), "state": "pending",
        });
        if let (Some(m), Value::Object(p)) = (req.as_object_mut(), parsed) {
            m.extend(p);
        }
        let sent = match self.io.post(&account, &channel, &request_text(&req), None, true).await {
            Ok(s) => s,
            Err(e) => {
                let _ = std::fs::remove_dir_all(&frozen);
                return Err(e);
            }
        };
        let pick = |a: &str, b: &str| sent.get(a).or(sent.get(b)).map(|v| v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string()));
        req["post_ts"] = json!(pick("ts", "message_id").unwrap_or_default());
        req["channel"] = json!(sent.get("channel_id").and_then(Value::as_str).unwrap_or(&channel));
        self.save(&req).map_err(|e| e.to_string())?;
        Ok(req)
    }

    pub fn attach(&self, r: &str, task_id: &str) -> std::io::Result<()> {
        let path = self.dir(r).join("request.json");
        let mut req: Value = serde_json::from_str(&std::fs::read_to_string(path)?)?;
        req["task"] = json!(task_id);
        self.save(&req)
    }

    /// One social message event: decide the request whose thread it answers, if any.
    pub async fn handle(&self, event: &Value) {
        let Some(thread) = event.get("thread_id").and_then(Value::as_str).filter(|s| !s.is_empty()) else { return };
        let word = first_word(event.get("text").and_then(Value::as_str).unwrap_or_default());
        let approve = ["approve", "approved"].contains(&word.as_str());
        if !approve && !["deny", "denied", "reject"].contains(&word.as_str()) {
            return;
        }
        let (req, text) = {
            let _g = self.lock.lock().await;
            let Some(mut req) = self.requests().into_iter().find(|r| {
                r["post_ts"].as_str() == Some(thread) && r["channel"] == event["channel_id"] && r["account"] == event["account"]
            }) else {
                return;
            };
            let text = self.decide(&mut req, event, approve).await;
            (req, text)
        };
        if let Some(t) = text {
            let (a, c, ts) = (req["account"].as_str().unwrap_or_default(), req["channel"].as_str().unwrap_or_default(), req["post_ts"].as_str().unwrap_or_default());
            if let Err(e) = self.io.post(a, c, &t, Some(ts), false).await {
                tracing::warn!("operations: could not answer approval {} in its thread: {e}", req["ref"]);
            }
        }
    }

    async fn decide(&self, req: &mut Value, event: &Value, approve: bool) -> Option<String> {
        if event.get("relayed") == Some(&json!(true)) || event.get("sender_bot") == Some(&json!(true)) {
            tracing::warn!("operations: ignored a relayed or bot reply on approval {}", req["ref"]);
            return None;
        }
        let sender = event.get("sender_id").map(|v| v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string())).unwrap_or_default();
        let Some(admin) = approval_admins().into_iter().find(|(u, _)| *u == sender).map(|(_, n)| n) else {
            return Some(format!("Only an Arbutus admin can decide this. {} (`{sender}`) is not on the list.",
                                slack(event.get("sender_name").and_then(Value::as_str).unwrap_or_default())));
        };
        let r = req["ref"].as_str().unwrap_or_default().to_string();
        if req["state"] != "pending" {
            return Some(format!("`{r}` is already {}.", req["state"].as_str().unwrap_or_default()));
        }
        if crate::now() - req["created"].as_f64().unwrap_or(0.0) > EXPIRES_S {
            return Some(self.close(req, "expired", &admin, "This request expired. The agent can ask again.").await);
        }
        let name = req["name"].as_str().unwrap_or_default().to_string();
        let frozen = self.dir(&r).join(format!("{name}.bundle"));
        if !frozen.is_file() || sha256_file(&frozen).ok().as_deref() != req["sha256"].as_str() {
            return Some(self.close(req, "refused", &admin, "The frozen bundle changed after it was posted. Nothing was approved.").await);
        }
        if !approve {
            return Some(self.close(req, "denied", &admin, &format!("Denied by {admin}.")).await);
        }
        let (rc, output) = self.io.run(&name, &self.dir(&r), &format!("{admin} via Slack")).await;
        if rc != 0 {
            return Some(self.close(req, "failed", &admin, &format!("The approve script failed (exit {rc}):\n```{}```", slack(&output))).await);
        }
        Some(self.close(req, "approved", &admin, &format!("Approved by {admin}.\n```{}```", slack(&output))).await)
    }

    async fn close(&self, req: &mut Value, state: &str, admin: &str, text: &str) -> String {
        req["state"] = json!(state);
        req["decided_by"] = json!(admin);
        req["decided_at"] = json!(crate::now());
        if let Err(e) = self.save(req) {
            tracing::warn!("operations: could not record approval {}: {e}", req["ref"]);
        }
        let (name, r) = (req["name"].as_str().unwrap_or_default(), req["ref"].as_str().unwrap_or_default());
        let mut line = format!("Admin approval {name} (ref {r}) is {state}, by {admin} in Slack.");
        if state != "approved" {
            line.push_str(" Nothing was granted.");
        } else {
            if req["ssh"].as_array().is_some_and(|a| !a.is_empty()) {
                line.push_str(&format!(" Its ssh windows are open for {} minutes.", req["minutes"].as_str().unwrap_or_default()));
            }
            if req["tf"].as_array().is_some_and(|a| !a.is_empty()) {
                line.push_str(&format!(" Run scripts/tf --approval {name} for its terraform changes."));
            }
        }
        if let Some(job) = req["job"].as_str().filter(|j| !j.is_empty()) {
            if let Err(e) = self.io.tell(job, &line).await {
                tracing::info!("operations: {job} did not take the approval line: {e}");
            }
        }
        if let Some(task) = req["task"].as_str().filter(|t| !t.is_empty()) {
            let hook = self.on_decided.lock().expect("decided lock").clone();
            if let Some(h) = hook {
                tokio::spawn(h(task.to_string(), line.clone()));
            }
        }
        text.to_string()
    }
}

/// Run the approve script on a frozen bundle, with the admin's name as the approver.
pub async fn run_approve(name: &str, bundles: &Path, approved_by: &str) -> (i32, String) {
    let mut cmd = tokio::process::Command::new(admin_approve_cmd());
    cmd.arg(name).env("ARB_BUNDLES", bundles).env("ARB_APPROVED_BY", approved_by).kill_on_drop(true);
    let fut = cmd.output();
    match tokio::time::timeout(RUN_TIMEOUT, fut).await {
        Err(_) => (124, "the approve script did not finish in time".into()),
        Ok(Err(e)) => (127, e.to_string()),
        Ok(Ok(out)) => {
            let mut text = String::from_utf8_lossy(&out.stdout).to_string();
            text.push_str(&String::from_utf8_lossy(&out.stderr));
            (out.status.code().unwrap_or(1), text.trim().to_string())
        }
    }
}
