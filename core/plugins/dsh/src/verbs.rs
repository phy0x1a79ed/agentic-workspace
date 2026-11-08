//! The two tools dsh serves: `dsh` (the harness's lifecycle and model route) and `harness-ds`
//! (harness@1 over the fork's sessions), as one table of specs and one dispatcher.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use awm_harness::contract::{self, args::bool_lenient, args::int_lenient, args::text, target_params};
use awm_plugin_sdk::{CallOpts, Hub, VerbError};
use serde_json::{json, Value};

use crate::fork::ForkError;
use crate::sessions::{pid_arg, unsupported, Sessions, NAME, PLATFORM};
use crate::settings::{Settings, SettingsError};
use crate::supervisor::{listening, Supervisor};

pub const HARNESS_TOOL: &str = "harness-ds";
const FRONT_TIMEOUT: Duration = Duration::from_secs(5);

pub struct App {
    pub sup: Supervisor,
    pub sessions: Sessions,
    pub settings: Settings,
    pub hub: Hub,
    pub workspace: PathBuf,
}

/// One published verb.
pub struct Spec {
    pub tool: &'static str,
    pub verb: &'static str,
    pub description: String,
    pub params: Value,
    pub timeout: Option<u64>,
}

fn p(name: &str, ty: &str) -> Value {
    json!({"name": name, "type": ty, "required": false})
}

fn pd(name: &str, ty: &str, description: &str) -> Value {
    json!({"name": name, "type": ty, "required": false, "description": description})
}

const TARGET_REF: &str = "A DeepSeek harness session: its fork id (session-<uuid>), an R9 id prefix of 8 or more characters, or its exact title.";

fn target_plus(extra: &[(&str, &str)]) -> Value {
    let mut out = target_params(TARGET_REF);
    out.extend(extra.iter().map(|(n, t)| p(n, t)));
    Value::Array(out)
}

const DSH: [(&str, &str, Option<u64>); 7] = [
    ("status", "Report the DeepSeek Harness: whether the fork worktree is built and at what version, which revision it is on and whether that revision is what was built, whether the web server is running and at what pid/port, whether it is actually listening, the mesh front's listener and TLS state with the URL to open, and the model route's two halves \u{2014} provider declared, key available \u{2014} named separately.", None),
    ("start", "Start the harness web server on loopback, waiting until it binds. Idempotent.", Some(120)),
    ("stop", "Stop the harness web server, ending live GUI connections. Sessions are persisted, so this loses no conversation. Idempotent. The supervision loop will not respawn it until start is called again.", Some(60)),
    ("restart", "Stop the harness and start it again \u{2014} e.g. to pick up an edited settings.yaml, which is read at startup.", Some(180)),
    ("url", "The mesh URL that opens the harness GUI. Behind awm's edge session, so it is usable only by someone who could log into awm.", None),
    ("model", "The default model new sessions start on. With no arguments, report the current selection and the route's catalog. Given a model id, select it \u{2014} the harness watches its settings document, so this takes effect without a restart. This exists because the harness's own Settings UI is loopback-only: a browser on the mesh gets an in-memory settings mirror and the Models page reports that settings are unavailable.", Some(60)),
    ("logs", "Tail the harness's stdout/stderr log.", None),
];

pub const CONTRACT_VERBS: [&str; 12] =
    ["platform", "resolve", "find", "status", "list", "send", "interrupt", "start", "stop", "resume", "set", "compact"];

fn dsh_params(verb: &str) -> Value {
    match verb {
        "model" => json!([
            pd("model", "string", "Model id to select. Omit to report only."),
            pd("declare", "boolean", "Also add the id to the route's catalog. Needed for a model the route does not yet advertise."),
            pd("reasoning", "string", "Reasoning effort to set alongside the model. Omit to keep whatever is already selected."),
        ]),
        "logs" => json!([pd("tail", "number", "Lines to return (default 200).")]),
        _ => json!([]),
    }
}

fn contract_params(verb: &str) -> Value {
    match verb {
        "platform" => json!([]),
        "resolve" => json!([{"name": "pid", "type": "integer", "required": true}]),
        "find" => json!([{"name": "ref", "type": "string", "required": true}]),
        "status" | "interrupt" => target_plus(&[]),
        "stop" => target_plus(&[("keep", "boolean"), ("confirm", "boolean")]),
        "list" => json!([p("project", "string"), p("scope", "string"), p("include_stopped", "boolean")]),
        "send" => target_plus(&[("text", "string"), ("submit", "boolean"), ("confirm", "boolean"), ("followup", "string"), ("delay_ms", "integer")]),
        "start" => Value::Array(
            [("project", "string"), ("scope", "string"), ("prompt", "string"), ("name", "string"), ("model", "string"),
             ("effort", "string"), ("preset", "string"), ("trust", "string"), ("task_id", "string"), ("role", "string"),
             ("started_by", "string"), ("pin", "boolean"), ("suggest", "boolean"), ("isolated", "boolean"), ("pid", "integer")]
                .iter()
                .map(|(n, t)| p(n, t))
                .collect(),
        ),
        "resume" => target_plus(&[("prompt", "string")]),
        "set" => target_plus(&[("model", "string"), ("effort", "string"), ("name", "string"), ("cwd", "string")]),
        "compact" => target_plus(&[("followup", "string"), ("delay_ms", "integer")]),
        _ => unreachable!("{verb} is no contract verb"),
    }
}

/// Every verb dsh publishes, `dsh` first.
pub fn specs() -> Vec<Spec> {
    let mut out: Vec<Spec> = DSH
        .iter()
        .map(|(verb, description, timeout)| Spec { tool: "dsh", verb, description: description.to_string(), params: dsh_params(verb), timeout: *timeout })
        .collect();
    out.extend(CONTRACT_VERBS.iter().map(|verb| Spec {
        tool: HARNESS_TOOL,
        verb,
        description: format!("Harness contract verb `{verb}` for DeepSeek harness sessions."),
        params: contract_params(verb),
        timeout: Some(60),
    }));
    out
}

fn verb_error(e: &str) -> VerbError {
    match e.split_once(": ") {
        Some((class, msg)) if !class.contains(' ') => VerbError::new(class, msg),
        _ => VerbError::runtime(e),
    }
}

fn settings_error(e: SettingsError) -> VerbError {
    match e {
        SettingsError::Value(m) => VerbError::value(m),
        SettingsError::Unavailable(m) => VerbError::new("SettingsUnavailable", m),
    }
}

impl App {
    /// The `front` block of `status`, from `auth`. A failure reads as a front not serving.
    pub async fn front(&self) -> Value {
        let opts = CallOpts { timeout: FRONT_TIMEOUT, ..CallOpts::default() };
        let why = match self.hub.call_with("auth", "front", json!({"service": "dsh"}), &opts).await {
            Ok(v @ Value::Object(_)) if v.get("ok") != Some(&Value::Bool(false)) => return v,
            Ok(v) => v.get("error").and_then(Value::as_str).unwrap_or("no front block").to_string(),
            Err(e) => e.to_string(),
        };
        json!({"listener_port": null, "upstream": null, "tls": false, "san": null, "serving": false,
               "error": format!("auth did not answer: {why}"), "url": null})
    }

    async fn model(&self, args: &Value) -> Result<Value, VerbError> {
        let model = text(args.get("model"));
        let declare = bool_lenient(args.get("declare"), false);
        let reasoning = text(args.get("reasoning"));
        let file = self.settings.file.clone();
        let lock_timeout = self.settings.lock_timeout;
        tokio::task::spawn_blocking(move || {
            let s = Settings { file, lock_timeout };
            match model {
                None => {
                    let doc = s.read()?;
                    Ok(json!({"selection": crate::settings::selection(&doc), "models": crate::settings::declared_models(&doc),
                              "settings": s.file}))
                }
                Some(m) => s.set_default_model(&m, declare, reasoning.as_deref()),
            }
        })
        .await
        .map_err(|e| VerbError::runtime(e.to_string()))?
        .map_err(settings_error)
    }

    /// A `start` or `restart` answer; a harness that started gets its session stream opened.
    fn started(&self, res: Result<Value, String>) -> Result<Value, VerbError> {
        if res.is_ok() {
            self.sessions.lifecycle.kick();
        }
        res.map_err(|e| verb_error(&e))
    }

    fn provider(&self) -> Option<String> {
        self.settings.provider_name()
    }

    /// Run one verb.
    pub async fn handle(self: &Arc<Self>, tool: &str, verb: &str, args: &Value) -> Result<Value, VerbError> {
        if tool == HARNESS_TOOL {
            return Ok(match self.contract(verb, args).await {
                Ok(v) => v,
                Err(e) => contract::refusal(e.0),
            });
        }
        match verb {
            "status" => Ok(json!({"harness": self.sup.snapshot().await, "front": self.front().await})),
            "start" => self.started(self.sup.start().await),
            "stop" => Ok(self.sup.stop().await),
            "restart" => self.started(self.sup.restart().await),
            "url" => Ok(json!({"url": self.front().await["url"], "note": "requires an awm session"})),
            "model" => self.model(args).await,
            "logs" => {
                let tail = match int_lenient(args.get("tail"), 200) {
                    0 => 200,
                    t => t.max(0) as usize,
                };
                Ok(json!({"tail": tail, "log": self.sup.logs(tail)}))
            }
            other => Err(VerbError::value(format!("dsh has no verb {other}"))),
        }
    }

    async fn contract(self: &Arc<Self>, verb: &str, args: &Value) -> Result<Value, ForkError> {
        let s = &self.sessions;
        let fork_pid = self.sup.pid().await;
        match verb {
            "platform" => Ok(contract::platform(PLATFORM, NAME, &[])),
            "resolve" => Ok(s.resolve(pid_arg(args.get("pid")), fork_pid).await),
            "find" => {
                let row = s.find_one(&text(args.get("ref")).unwrap_or_default()).await?;
                Ok(json!({"ok": true, "matches": row.into_iter().collect::<Vec<_>>()}))
            }
            "status" => match s.target(args, fork_pid).await {
                Ok(row) => Ok(with_ok(row)),
                Err(_) if !listening(s.fork.port).await => Ok(json!({"ok": true, "live": false, "state": "stopped"})),
                Err(e) => Ok(json!({"ok": false, "error": e.0,
                                    "unknown_ref": text(args.get("session_id")).or_else(|| text(args.get("ref")))})),
            },
            "list" => {
                let rows = match s.rows().await {
                    Ok(rows) => rows,
                    Err(_) if !listening(s.fork.port).await => vec![],
                    Err(e) => return Err(e),
                };
                let wanted = |row: &Value, k: &str| text(args.get(k)).is_none_or(|v| row.get(k).and_then(Value::as_str) == Some(v.as_str()));
                let sessions: Vec<Value> = rows.into_iter().filter(|r| wanted(r, "project") && wanted(r, "scope")).map(list_row).collect();
                Ok(json!({"ok": true, "sessions": sessions}))
            }
            "send" => {
                if text(args.get("followup")).is_some() {
                    return Ok(unsupported("type a follow-up after any command but /compact"));
                }
                let Some(body) = args.get("text").and_then(Value::as_str).filter(|t| !t.is_empty()).map(str::to_string) else {
                    return Ok(contract::refusal("send needs text"));
                };
                s.send(&s.target(args, fork_pid).await?, &body).await
            }
            "interrupt" => s.interrupt(&s.target(args, fork_pid).await?).await,
            "start" => s.start(args, &self.workspace, self.provider()).await,
            "stop" => s.stop(&s.target(args, fork_pid).await?).await,
            "resume" => {
                let prompt = args.get("prompt").and_then(Value::as_str);
                s.resume(&s.target(args, fork_pid).await?, prompt).await
            }
            "set" => {
                let row = s.target(args, fork_pid).await?;
                s.set(&row, args, self.provider()).await
            }
            "compact" => {
                let row = s.target(args, fork_pid).await?;
                let followup = args.get("followup").and_then(Value::as_str).map(str::to_string);
                let (out, watch) = s.compact(&row, followup.as_deref()).await?;
                if let (Some((ticket, since)), Some(followup)) = (watch, followup) {
                    let app = self.clone();
                    tokio::spawn(async move {
                        app.sessions.watch_compaction(&row, &followup, &ticket, since).await;
                    });
                }
                Ok(out)
            }
            other => Ok(contract::refusal(format!("harness-ds has no verb {other}"))),
        }
    }
}

fn with_ok(row: Value) -> Value {
    let mut out = serde_json::Map::from_iter([("ok".to_string(), json!(true))]);
    if let Value::Object(m) = row {
        out.extend(m);
    }
    Value::Object(out)
}

/// A `list` row: the session row with the keys every harness's list carries.
fn list_row(mut row: Value) -> Value {
    row["job"] = row["fork_session_id"].clone();
    row["alive"] = row["live"].clone();
    row["kind"] = json!("web");
    row["transcript"] = Value::Null;
    if row.get("parent").is_none() {
        row["parent"] = Value::Null;
    }
    row
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fork::stub::{self, Stub};
    use crate::fork::Fork;
    use crate::lifecycle::Lifecycle;
    use crate::paths::Paths;
    use crate::sessions::tests::{default_items, recording, FakeProcs, FORK, R9};
    use crate::sessions::{COMPACT_WAIT, POLL};

    async fn app(fork_port: u16, dir: &std::path::Path) -> Arc<App> {
        let paths = Paths::under(dir, fork_port);
        let (reporter, _) = recording(paths.spool(), true);
        Arc::new(App {
            sup: Supervisor::new(paths.clone()),
            sessions: Sessions { fork: Fork::new(fork_port), procs: Box::new(FakeProcs::default()), reporter, lifecycle: Lifecycle::new().0, compact_wait: COMPACT_WAIT, poll: POLL },
            settings: Settings::new(paths.settings_file()),
            hub: Hub::new(&format!("http://127.0.0.1:{}", stub::dead_port().await), None),
            workspace: paths.workspace.clone(),
        })
    }

    async fn up() -> (Stub, Arc<App>, tempfile::TempDir) {
        let stub = Stub::default();
        stub.with(|g| g.items = default_items());
        let dir = tempfile::tempdir().unwrap();
        let a = app(stub::serve(stub.clone()).await, dir.path()).await;
        (stub, a, dir)
    }

    #[tokio::test]
    async fn list_is_empty_while_the_fork_is_down() {
        let dir = tempfile::tempdir().unwrap();
        let a = app(stub::dead_port().await, dir.path()).await;
        assert_eq!(a.handle(HARNESS_TOOL, "list", &json!({})).await.unwrap(), json!({"ok": true, "sessions": []}));
        let st = a.handle(HARNESS_TOOL, "status", &json!({"session_id": R9})).await.unwrap();
        assert_eq!(st, json!({"ok": true, "live": false, "state": "stopped"}));
    }

    #[tokio::test]
    async fn list_rows_carry_the_contract_keys_and_filter() {
        let (_s, a, _d) = up().await;
        let out = a.handle(HARNESS_TOOL, "list", &json!({"project": "awm"})).await.unwrap();
        let row = &out["sessions"][0];
        assert_eq!(out["sessions"].as_array().unwrap().len(), 1);
        assert_eq!((row["job"].as_str(), row["alive"].clone(), row["kind"].as_str()), (Some(FORK), json!(true), Some("web")));
        assert_eq!((row["parent"].clone(), row["transcript"].clone()), (Value::Null, Value::Null));
        let none = a.handle(HARNESS_TOOL, "list", &json!({"scope": "other"})).await.unwrap();
        assert_eq!(none["sessions"], json!([]));
    }

    #[tokio::test]
    async fn send_without_text_is_a_refusal_not_a_crash() {
        let (stub, a, _d) = up().await;
        let out = a.handle(HARNESS_TOOL, "send", &json!({"session_id": R9})).await.unwrap();
        assert_eq!(out, json!({"ok": false, "error": "send needs text"}));
        let out = a.handle(HARNESS_TOOL, "send", &json!({"session_id": R9, "text": ""})).await.unwrap();
        assert_eq!(out["ok"], false);
        assert!(stub.calls().is_empty());
        let out = a.handle(HARNESS_TOOL, "send", &json!({"session_id": R9, "text": "hi", "followup": "x"})).await.unwrap();
        assert_eq!(out["unsupported"], true);
        assert_eq!(a.handle(HARNESS_TOOL, "send", &json!({"session_id": R9, "text": "hi"})).await.unwrap()["confirmed"], "enqueued");
    }

    #[tokio::test]
    async fn a_fork_fault_is_ok_false_never_a_verb_error() {
        let (stub, a, _d) = up().await;
        stub.with(|g| g.refuse.push("session.cancel".into()));
        let out = a.handle(HARNESS_TOOL, "interrupt", &json!({"ref": "web"})).await.unwrap();
        assert_eq!(out, json!({"ok": false, "error": "session.cancel: E_STUB: refused"}));
        let out = a.handle(HARNESS_TOOL, "status", &json!({"ref": "nothing"})).await.unwrap();
        assert_eq!(out["ok"], false);
        assert_eq!(out["error"], "no DeepSeek harness session matches 'nothing'");
        assert_eq!(out["unknown_ref"], "nothing");
        let out = a.handle(HARNESS_TOOL, "stop", &json!({})).await.unwrap();
        assert_eq!(out["error"], "name a session with session_id or ref");
        assert_eq!(a.handle(HARNESS_TOOL, "platform", &json!({})).await.unwrap()["code"], "ds");
    }

    #[tokio::test]
    async fn status_reports_the_front_as_not_serving_when_auth_does_not_answer() {
        let (_s, a, _d) = up().await;
        let st = a.handle("dsh", "status", &json!({})).await.unwrap();
        assert_eq!(st["front"]["serving"], false);
        assert!(st["front"]["error"].as_str().unwrap().starts_with("auth did not answer: "));
        assert_eq!(st["harness"]["installed"], false);
        let url = a.handle("dsh", "url", &json!({})).await.unwrap();
        assert_eq!(url, json!({"url": null, "note": "requires an awm session"}));
    }

    #[tokio::test]
    async fn the_model_verb_reports_and_selects() {
        let (_s, a, _d) = up().await;
        a.settings.ensure();
        let r = a.handle("dsh", "model", &json!({})).await.unwrap();
        assert_eq!(r["selection"]["model"], crate::settings::MODELS[0]);
        let e = a.handle("dsh", "model", &json!({"model": "vendor/x"})).await.unwrap_err();
        assert_eq!(e.error_class, "ValueError");
        let r = a.handle("dsh", "model", &json!({"model": "vendor/x", "declare": "true", "reasoning": "low"})).await.unwrap();
        assert_eq!((r["model"].as_str(), r["selection"]["reasoningEffort"].as_str()), (Some("vendor/x"), Some("low")));
    }

    #[tokio::test]
    async fn starting_an_unbuilt_fork_is_a_verb_error_naming_why() {
        let (_s, a, _d) = up().await;
        let e = a.handle("dsh", "start", &json!({})).await.unwrap_err();
        assert_eq!(e.error_class, "FileNotFoundError");
        assert!(e.message.contains("no built CLI"));
        assert_eq!(a.handle("dsh", "logs", &json!({"tail": "5"})).await.unwrap()["tail"], 5);
    }
}
