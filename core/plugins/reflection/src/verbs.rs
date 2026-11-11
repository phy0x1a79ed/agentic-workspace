//! The verbs: each one's work on the session it acts on, through that session's provider, and
//! the wrapper that turns every failure into `ok: false` with a reason, logged once.

use std::sync::Arc;

use awm_harness::contract::args::{bool_lenient, int_lenient};
use awm_plugin_sdk::ids;
use futures_util::future::join_all;
use serde_json::{json, Map, Value};

use awm_pycompat::repr::str_repr;
use crate::app::{declared, is_false, is_ok, merged, object, ok_or_absent, text, truthy, App, Failure};
use crate::caller::Frame;
use crate::gateway::Gateway;
use crate::ledger::Ticket;
use crate::providers::{HarnessError, Provider, Ref, DOWN_FOR};
use crate::trust::recorded_in;
use crate::waits::NewWait;

/// The mode the gateway's gate restricts hardest (core modes.rs).
pub const UNKNOWN_MODE: &str = "unknown";
/// The level an isolated session starts at when the caller names none (fleet.md R109).
pub const ISOLATED_TRUST: &str = "untrusted";
/// `harness_event` kinds that report a session's lifecycle.
pub const LIFECYCLE: [&str; 5] = ["started", "stopped", "remote_control", "renamed", "state"];
/// The emit topic of the session lifecycle, `reflection/sessions`.
pub const SESSIONS_TOPIC: &str = "sessions";
/// The keys every `list_other` row carries, null when its provider gave none.
pub const ROW_KEYS: [&str; 8] = ["session_id", "parent", "state", "live", "alive", "kind", "name", "transcript"];


const NO_MODEL: &str = "model and effort are required, with no default. Pick both to fit the task: a smaller model and \
                        lower effort for mechanical work, a larger model and high effort for design or debugging. Fable \
                        needs Tony's consent first.";
const OWN_STOP: &str = "that is your own session; stopping it ends this conversation. Pass confirm=true to proceed.";
const TERMINAL_WAIT: &str =
    "a terminal session stays open; the wake line is typed in when every listed session has retired. End your turn now";

fn flag(args: &Value, key: &str, default: bool) -> bool {
    bool_lenient(args.get(key), default)
}

fn int(args: &Value, key: &str) -> i64 {
    int_lenient(args.get(key), 0)
}

/// The `to` a verb names, None for the caller's own session, or the refusal of a malformed one.
fn to_arg(args: &Value) -> Result<Option<Ref>, Value> {
    Ref::from_args(args).map_err(|e| reason(&e))
}

/// The `to` a verb requires, or the refusal of a missing or malformed one.
fn required_to(args: &Value) -> Result<Ref, Value> {
    to_arg(args)?.ok_or_else(|| reason("to is required: {id} or {name}"))
}

/// Unwraps a `to_arg` or `required_to` result, returning its refusal as the verb's reply.
macro_rules! or_refuse {
    ($e:expr) => {
        match $e {
            Ok(v) => v,
            Err(refused) => return Ok(refused),
        }
    };
}

/// A field as Python's `str()` prints it.
fn shown(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => "None".into(),
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
    }
}

fn reason(text: &str) -> Value {
    json!({"ok": false, "reason": text})
}

/// The refusal of an effort above high, the upper limit for any session reflection starts or
/// retunes.
fn over_limit(level: Option<&Value>) -> Option<Value> {
    let level = level.and_then(Value::as_str)?;
    if !awm_trust::trust::OVER_CEILING.contains(&level) {
        return None;
    }
    awm_trust::trust::check_effort(level).err().map(|e| reason(&e.to_string()))
}

impl<G: Gateway> App<G> {
    /// Run `verb` for `frame`'s caller. Every failure answers `{ok: false, error}`.
    pub async fn handle(self: &Arc<Self>, verb: &str, frame: &Frame, args: &Value) -> Value {
        let out = match verb {
            "message_self" => self.message_self(frame, args).await,
            "compact" => self.compact(frame, args).await,
            "mode" => self.mode(frame, args).await,
            "remote_control" => {
                let extra = json!({"on": flag(args, "on", false), "name": args.get("name").filter(|n| truthy(Some(n)))});
                self.on_target(frame, args, "remote_control", Some("remote_control"), extra).await
            }
            "model" => self.on_target(frame, args, "set", Some("model"), json!({"model": args.get("model")})).await,
            "effort" => match over_limit(args.get("level")) {
                Some(refused) => Ok(refused),
                None => self.on_target(frame, args, "set", Some("effort"), json!({"effort": args.get("level")})).await,
            },
            "rename" => self.on_target(frame, args, "set", Some("rename"), json!({"name": args.get("name")})).await,
            "cd" => self.cd(frame, args).await,
            "mcp" => {
                let extra = json!({"action": args.get("action"), "server": args.get("server")});
                self.on_target(frame, args, "mcp", Some("mcp"), extra).await
            }
            "interrupt" => self.on_target(frame, args, "interrupt", Some("interrupt"), json!({})).await,
            "stop" => self.stop(frame, args).await,
            "link" => self.on_target(frame, args, "link", None, json!({})).await,
            "message" => self.message(frame, args).await,
            "pending" => Ok(self.pending().await),
            "whoami" => self.whoami(frame).await,
            "session_of" => self.session_of(args).await,
            "start_other" => self.start_other(frame, args).await,
            "list_other" => Ok(self.list_other(frame, args).await),
            "probe" => Ok(self.probe(frame, args).await),
            "stop_other" => self.stop_other(frame, args).await,
            "resume_other" => self.resume_other(frame, args).await,
            "adopt_other" => self.on_other(frame, args, "adopt", "start_other", json!({})).await.map(|(out, _)| out),
            "wait" => self.wait(frame, args).await,
            "waits" => Ok(json!({"ok": true, "waits": self.waits().await})),
            "retired" => Ok(self.retired(args).await),
            "retired_among" => Ok(self.retired_among(args).await),
            "harness_event" => Ok(self.harness_event(args).await),
            _ => Ok(json!({"ok": false, "error": format!("unknown verb {}", str_repr(verb))})),
        };
        self.guarded(verb, frame, out).await
    }

    /// Turn a failure into `ok: false` with a reason, and log it once. Never logs the args: a
    /// `text` is the caller's own prompt.
    async fn guarded(&self, verb: &str, frame: &Frame, out: Result<Value, Failure>) -> Value {
        match out {
            Ok(v) => {
                if v.is_object() && !ok_or_absent(&v) {
                    let why = v.get("reason").filter(|r| truthy(Some(r))).or_else(|| v.get("error"));
                    tracing::info!("reflection: {verb} refused for caller pid {}: {}", frame.pid_text(), shown(why));
                }
                v
            }
            Err(e) => {
                let mut why = e.to_string();
                let unknown = matches!(&e, Failure::Harness(HarnessError::Unknown(_)));
                if let Failure::Harness(HarnessError::Unknown(r)) = &e {
                    // Only a session's own call looks further: a peer's call has no caller pid,
                    // and must not fan out again from here.
                    if frame.pid.is_some() {
                        why.push_str(&crate::peers::hint_text(&self.holders(r).await));
                    }
                }
                tracing::warn!("reflection: {verb} failed for caller pid {}: {why}", frame.pid_text());
                let mut out = json!({"ok": false, "error": why});
                if unknown && verb == "message" {
                    out["code"] = json!("gone");
                }
                out
            }
        }
    }

    // --- the caller's own session ------------------------------------------------

    async fn own(&self, frame: &Frame, verb: &str, extra: Value) -> Result<(Provider, Value, Value), Failure> {
        let (p, who) = self.resolve_caller(frame).await?;
        let out = self.harness_call(&p, verb, merged(&frame.me(), &extra), None).await?;
        Ok((p, who, out))
    }

    async fn with_ticket(&self, p: &Provider, who: &Value, out: Value) -> Value {
        if truthy(out.get("ticket")) {
            let t = Ticket {
                ticket: shown(out.get("ticket")),
                session_id: text(who, "session_id").unwrap_or_default().into(),
                tool: p.tool.clone(),
                command: text(&out, "text").unwrap_or_default().into(),
                followup: text(&out, "followup").unwrap_or_default().into(),
                name: text(who, "name").unwrap_or_default().into(),
                cwd: text(who, "cwd").map(str::to_string),
            };
            self.open_ticket(&t).await;
        }
        out
    }

    async fn message_self(&self, frame: &Frame, args: &Value) -> Result<Value, Failure> {
        let extra = json!({
            "text": self.provider_value(frame, args.get("text")).await,
            "submit": flag(args, "enter", true),
            "delay_ms": int(args, "delay_ms"),
            "confirm": flag(args, "confirm", false),
            "followup": self.provider_value(frame, args.get("followup")).await,
        });
        let (p, who, out) = self.own(frame, "send", extra).await?;
        Ok(self.with_ticket(&p, &who, out).await)
    }

    async fn compact(&self, frame: &Frame, args: &Value) -> Result<Value, Failure> {
        let extra = json!({"delay_ms": int(args, "delay_ms"), "followup": self.provider_value(frame, args.get("followup")).await});
        let (p, who, out) = self.own(frame, "compact", extra).await?;
        Ok(self.with_ticket(&p, &who, out).await)
    }

    async fn mode(&self, frame: &Frame, args: &Value) -> Result<Value, Failure> {
        Ok(self.own(frame, "mode", json!({"expect_session": args.get("expect_session")})).await?.2)
    }

    async fn whoami(&self, frame: &Frame) -> Result<Value, Failure> {
        let (p, who) = self.resolve_caller(frame).await?;
        let sid = text(&who, "session_id").unwrap_or_default().to_string();
        let task = if sid.is_empty() { Value::Null } else { self.task_of(&sid).await };
        let session = text(&who, "name").map_or_else(|| json!(sid), |n| json!(n));
        let extra = json!({"ok": true, "harness": p.tool, "platform": p.code, "trust": self.level(&who).await,
                           "task_id": task, "session": session});
        Ok(merged(&who, &extra))
    }

    /// The session at `pid`, with its mode and trust, for the gateway's gate and stamps. One
    /// `resolve` per provider answers both cases: a pid no harness plugin claims is
    /// `known: false`, with the mode the plugins say of it (null when it is positively not a
    /// started session), or `unknown` when none could say.
    async fn session_of(&self, args: &Value) -> Result<Value, Failure> {
        let pid = int(args, "pid");
        let answers: Vec<(Provider, Value)> = self
            .everyone("resolve", &json!({"pid": pid}), None)
            .await
            .into_iter()
            .filter_map(|(p, got)| got.map_err(|e| tracing::info!("reflection: {e}")).ok().map(|v| (p, v)))
            .collect();
        let as_pid = u32::try_from(pid).ok().filter(|p| *p > 0);
        match crate::providers::pick_caller(as_pid, &answers) {
            Err(e @ HarnessError::Ambiguous(_)) => Err(e.into()),
            Err(e) => {
                let modes: Vec<&Value> = answers.iter().filter_map(|(_, r)| r.get("mode")).collect();
                let mode = if modes.is_empty() {
                    json!(UNKNOWN_MODE)
                } else {
                    modes.into_iter().find(|m| !m.is_null()).cloned().unwrap_or(Value::Null)
                };
                Ok(json!({"ok": true, "known": false, "mode": mode, "reason": e.to_string()}))
            }
            Ok((p, who)) => {
                let mode = who.get("mode").cloned().unwrap_or(json!(UNKNOWN_MODE));
                let extra = json!({"ok": true, "known": true, "harness": p.tool, "platform": p.code, "mode": mode,
                                   "trust": self.level(&who).await});
                Ok(merged(&who, &extra))
            }
        }
    }

    // --- a session named by `to`, or the caller's own --------------------------------

    /// The target `named` as its harness describes it, or `named` with `known: false`.
    async fn described(&self, p: &Provider, named: &Map<String, Value>) -> Value {
        let unknown = || merged(&Value::Object(named.clone()), &json!({"known": false}));
        match self.harness_call(p, "status", Value::Object(named.clone()), None).await {
            Ok(who) if !is_false(&who, "ok") && text(&who, "session_id").is_some() => who,
            _ => unknown(),
        }
    }

    /// The one target-and-steer helper: the provider and target args of the session `to` names,
    /// else of the caller's own session. A `to` is a steer, checked when `steer` names the verb.
    async fn steer_target(&self, frame: &Frame, to: Option<&Ref>, steer: Option<&str>) -> Result<(Provider, Map<String, Value>), Failure> {
        let (p, named) = self.target(to, frame).await?;
        if let (Some(verb), Some(_)) = (steer, to) {
            let actor = self.actor(frame).await;
            let target = self.described(&p, &named).await;
            self.check_steer(actor.as_ref(), verb, Some(&target)).await?;
        }
        Ok((p, named))
    }

    async fn on_target(&self, frame: &Frame, args: &Value, verb: &str, steer: Option<&str>, extra: Value) -> Result<Value, Failure> {
        let to = or_refuse!(to_arg(args));
        let (p, named) = self.steer_target(frame, to.as_ref(), steer).await?;
        Ok(self.harness_call(&p, verb, merged(&Value::Object(named), &extra), None).await?)
    }

    /// A verb that needs a `to` and always checks the steer, sent with the caller's `as`.
    async fn on_other(&self, frame: &Frame, args: &Value, verb: &str, steer: &str, extra: Value) -> Result<(Value, Map<String, Value>), Failure> {
        let to = match required_to(args) {
            Ok(to) => to,
            Err(refused) => return Ok((refused, Map::new())),
        };
        let (p, named) = self.steer_target(frame, Some(&to), Some(steer)).await?;
        let out = self.harness_call(&p, verb, merged(&Value::Object(named.clone()), &extra), frame.as_.as_deref()).await?;
        Ok((out, named))
    }

    async fn stop(self: &Arc<Self>, frame: &Frame, args: &Value) -> Result<Value, Failure> {
        let to = or_refuse!(to_arg(args));
        let confirm = flag(args, "confirm", false);
        let (p, named) = match &to {
            Some(to) => self.steer_target(frame, Some(to), Some("stop")).await?,
            None => {
                let (p, who) = self.resolve_caller(frame).await?;
                if !confirm {
                    return Ok(json!({"ok": false, "reason": OWN_STOP, "kind": "self"}));
                }
                // Named by its id, so the provider sees the caller stop itself.
                let named = json!({"session_id": who.get("session_id"), "pid": frame.pid_value()});
                (p, object(&named))
            }
        };
        let sid = text(&Value::Object(named.clone()), "session_id").map(str::to_string);
        let extra = json!({"keep": false, "confirm": confirm});
        let out = self.harness_call(&p, "stop", merged(&Value::Object(named), &extra), None).await?;
        Ok(self.after_retire(out, sid.as_deref()).await)
    }

    /// A retired session ends its scope claims, and wakes its waiters here and on each peer (R51).
    async fn after_retire(self: &Arc<Self>, out: Value, session_id: Option<&str>) -> Value {
        let sid = text(&out, "session_id").or(session_id).map(str::to_string);
        let Some(sid) = sid.filter(|s| is_ok(&out) && ids::SessionId::parse(s).is_ok()) else { return out };
        self.release_claims(&sid).await;
        let woken = self.retire(&sid, true, |row| self.wake(row)).await;
        let app = self.clone();
        let told = sid.clone();
        self.detach(async move { app.tell_peers(&told).await });
        merged(&out, &json!({"woken": woken}))
    }

    async fn message(&self, frame: &Frame, args: &Value) -> Result<Value, Failure> {
        let to = or_refuse!(required_to(args));
        let (p, named) = self.steer_target(frame, Some(&to), None).await?;
        let extra = json!({"text": self.provider_value(frame, args.get("text")).await, "confirm": flag(args, "confirm", false)});
        let mut out = self.harness_call(&p, "send", merged(&Value::Object(named), &extra), None).await?;
        if is_false(&out, "ok") && truthy(out.get("unknown_ref")) && out.get("code").is_none() {
            out["code"] = json!("gone");
        }
        let actor = if is_ok(&out) { self.actor(frame).await } else { None };
        let hint = match &actor {
            Some(a) => self.first_message_hint(text(a, "session_id")).await,
            None => None,
        };
        Ok(match hint {
            Some(h) => merged(&out, &json!({"hint": h})),
            None => out,
        })
    }

    /// Move a session, claiming the scope it moves into (sessions lane, scope claims).
    async fn cd(&self, frame: &Frame, args: &Value) -> Result<Value, Failure> {
        let to = or_refuse!(to_arg(args));
        let (p, named) = self.steer_target(frame, to.as_ref(), Some("cd")).await?;
        let before = self.harness_call(&p, "status", Value::Object(named.clone()), None).await?;
        let sid = text(&before, "session_id").or_else(|| named.get("session_id").and_then(Value::as_str)).unwrap_or_default().to_string();
        let path = args.get("path").and_then(Value::as_str);
        let place = self.scope_of(path).await;
        let interactive = before.get("kind").and_then(Value::as_str) == Some("interactive");
        let mut claim = json!("unavailable");
        let mut warning = None;
        if let (Some((project, scope)), false) = (&place, sid.is_empty()) {
            let ask = json!({"project": project, "scope": scope, "session_id": sid, "pid": before.get("pid"), "terminal": interactive});
            if let Some(got) = self.scopes_call("claim", ask).await {
                if !ok_or_absent(&got) && !truthy(got.get("claimed")) {
                    let why = text(&got, "reason").map(str::to_string);
                    if !interactive {
                        let why = why.unwrap_or_else(|| format!("{project}/{scope} is held by {}", shown(got.get("holder"))));
                        return Ok(json!({"ok": false, "claim": got, "reason": why}));
                    }
                    warning = Some(why.unwrap_or_else(|| format!("{project}/{scope} is held")));
                }
                claim = got;
            }
        }
        let line = match &place {
            Some((project, scope)) => self.brief_line(project, scope).await,
            None => String::new(),
        };
        let mut extra = json!({"cwd": args.get("path")});
        if !line.is_empty() {
            extra["brief"] = json!(line);
        }
        let out = self.harness_call(&p, "set", merged(&Value::Object(named), &extra), None).await?;
        let old = self.scope_of(text(&before, "cwd")).await;
        if let Some((project, scope)) = old.as_ref().filter(|o| is_ok(&out) && Some(*o) != place.as_ref() && !sid.is_empty() && claim != json!("unavailable")) {
            self.scopes_call("release", json!({"project": project, "scope": scope, "session_id": sid})).await;
        }
        let mut out = merged(&out, &json!({"claim": claim}));
        if let Some(w) = warning {
            out["warning"] = json!(w);
        }
        Ok(out)
    }

    async fn pending(&self) -> Value {
        let mut rows = vec![];
        for (p, out) in self.answers("pending", &json!({})).await {
            for r in out.get("pending").and_then(Value::as_array).cloned().unwrap_or_default() {
                if r.is_object() {
                    rows.push(merged(&r, &json!({"harness": p.tool})));
                }
            }
        }
        let lost: Vec<Value> = self.recent_lost().await.iter().map(|e| merged(e, &json!({"state": "lost"}))).collect();
        json!({"ok": true, "pending": rows, "tickets": self.open_tickets().await, "lost": lost})
    }

    // --- lifecycle ---------------------------------------------------------------

    async fn start_other(&self, frame: &Frame, args: &Value) -> Result<Value, Failure> {
        let actor = self.actor(frame).await;
        self.check_steer(actor.as_ref(), "start_other", None).await?;
        if !truthy(args.get("model")) || !truthy(args.get("effort")) {
            return Ok(reason(NO_MODEL));
        }
        if let Some(refused) = over_limit(args.get("effort")) {
            return Ok(refused);
        }
        let requested = match args.get("trust").filter(|t| truthy(Some(t))) {
            Some(t) => Some(shown(Some(t))),
            None if truthy(args.get("isolated")) => Some(ISOLATED_TRUST.to_string()),
            None => None,
        };
        let level = self.start_level(actor.as_ref(), requested.as_deref()).await?;
        let platform = args.get("platform").filter(|p| truthy(Some(p))).map_or_else(|| ids::Platform::Claude.code().to_string(), |p| shown(Some(p)));
        let p = self.for_platform(&platform).await?;
        // A lobby start claims nothing until its own `cd`.
        let place = (truthy(args.get("project")) && truthy(args.get("scope")) && !truthy(args.get("suggest")))
            .then(|| (shown(args.get("project")), shown(args.get("scope"))));
        if let Some((project, scope)) = &place {
            if let Some(holder) = self.held_by(project, scope).await {
                let why = format!(
                    "session {} holds {project}/{scope}. Start the session elsewhere, or ask Tony on the board (R7).",
                    shown(holder.get("session_id"))
                );
                return Ok(json!({"ok": false, "holder": holder, "reason": why}));
            }
        }
        let mut fwd = declared(args);
        fwd.remove("platform");
        if fwd.contains_key("prompt") {
            let prompt = self.provider_value(frame, args.get("prompt")).await;
            fwd.insert("prompt".into(), prompt);
        }
        fwd.insert("pid".into(), frame.pid_value());
        let mut out = self.harness_call(&p, "start", Value::Object(fwd), frame.as_.as_deref()).await?;
        let Some(sid) = text(&out, "session_id").map(str::to_string) else { return Ok(out) };
        if !is_ok(&out) {
            return Ok(out);
        }
        if let Some(place) = &place {
            out = self.start_claim(&p, place, out, frame).await;
            if !is_ok(&out) {
                return Ok(out);
            }
        }
        if !level.is_empty() {
            let origin = match &actor {
                Some(a) => format!("session:{}", shown(a.get("session_id"))),
                None => frame.as_.clone().filter(|a| !a.is_empty()).unwrap_or_else(|| "service".into()),
            };
            self.record_trust(&sid, &level, &origin).await;
            out["trust"] = json!(level);
        }
        if let Some(parent) = actor.as_ref().and_then(|a| text(a, "session_id")) {
            self.record_parent(&sid, parent, "start").await;
        }
        Ok(out)
    }

    /// Claim the scope a started session runs in, or stop the session (scopes.md R42). The claim
    /// carries the new session's pid, so it ends with the session's process.
    async fn start_claim(&self, p: &Provider, place: &(String, String), out: Value, frame: &Frame) -> Value {
        let (project, scope) = place;
        let sid = text(&out, "session_id").unwrap_or_default().to_string();
        let new_pid = match self.harness_call(p, "status", json!({"session_id": sid}), None).await {
            Ok(st) => st.get("pid").cloned().unwrap_or(Value::Null),
            Err(e) => {
                tracing::warn!("reflection: no pid for the new session {sid}: {e}");
                Value::Null
            }
        };
        let ask = json!({"project": project, "scope": scope, "session_id": sid, "pid": new_pid});
        let Some(got) = self.scopes_call("claim", ask).await else { return merged(&out, &json!({"claim": "unavailable"})) };
        if ok_or_absent(&got) || truthy(got.get("claimed")) {
            return merged(&out, &json!({"claim": got}));
        }
        let stop = json!({"session_id": sid, "pid": frame.pid_value(), "keep": false, "confirm": true});
        let stopped = match self.harness_call(p, "stop", stop, None).await {
            Ok(st) => ok_or_absent(&st),
            Err(e) => {
                tracing::warn!("reflection: could not stop {sid} after its claim was refused: {e}");
                false
            }
        };
        let why = text(&got, "reason").map_or_else(|| format!("{project}/{scope} is held by {}", shown(got.get("holder"))), str::to_string);
        let stopped = if stopped { json!(sid) } else { Value::Null };
        json!({"ok": false, "claim": got, "stopped": stopped, "reason": why})
    }

    /// Every session of every provider. A provider that fails, or that a failed call left marked
    /// down, adds an entry to `errors`, and the verb still answers.
    async fn list_other(&self, frame: &Frame, args: &Value) -> Value {
        let fwd = Value::Object(declared(args));
        let (providers, down) = self.providers_and_down().await;
        let asks = providers.iter().map(|p| self.harness_call(p, "list", fwd.clone(), frame.as_.as_deref()));
        let answers = join_all(asks).await;
        let mut errors: Vec<Value> = down
            .iter()
            .map(|tool| json!({"harness": tool, "error": format!("{tool} failed a call lately; it is skipped for {} s", DOWN_FOR.as_secs())}))
            .collect();
        let (mut rows, mut extras) = (vec![], Map::new());
        for (p, got) in providers.iter().zip(answers) {
            let got = match got {
                Err(e) => {
                    errors.push(json!({"harness": p.tool, "error": e.to_string()}));
                    continue;
                }
                Ok(got) if is_false(&got, "ok") => {
                    errors.push(merged(&json!({"harness": p.tool}), &got));
                    continue;
                }
                Ok(got) => got,
            };
            for r in got.get("sessions").and_then(Value::as_array).cloned().unwrap_or_default() {
                if r.is_object() {
                    rows.push(merged(&r, &json!({"harness": p.tool})));
                }
            }
            for key in ["pool", "daemon"] {
                if let Some(v) = got.get(key) {
                    extras.insert(key.into(), v.clone());
                }
            }
        }
        // `trust` is the recorded level, or the row's own for a session with no record.
        let data = self.trust_data().await;
        for r in rows.iter_mut() {
            let level = recorded_in(&data, text(r, "session_id"));
            r["trust"] = level.map_or_else(|| r.get("trust").cloned().unwrap_or(Value::Null), |l| json!(l));
            if let Some(link) = text(r, "link").map(str::to_string) {
                r["rc_url"] = json!(link);
            }
            for key in ROW_KEYS {
                if r.get(key).is_none() {
                    r[key] = Value::Null;
                }
            }
        }
        let mut out = json!({"ok": true, "sessions": rows});
        if !errors.is_empty() {
            out["errors"] = json!(errors);
        }
        merged(&out, &Value::Object(extras))
    }

    /// Whether `node` answers this node, as each provider here that serves `probe` sees it. The
    /// call carries the caller's `as`, so a provider's peer gate sees the peer.
    async fn probe(&self, frame: &Frame, args: &Value) -> Value {
        let node = text(args, "node").map(str::trim).unwrap_or_default().to_string();
        if node.is_empty() {
            return reason("probe needs a node");
        }
        let probers: Vec<Provider> = self.providers().await.into_iter().filter(|p| p.serves("probe")).collect();
        if probers.is_empty() {
            return reason("no harness plugin on this node serves probe");
        }
        let asks = probers.iter().map(|p| self.harness_call(p, "probe", json!({"node": node}), frame.as_.as_deref()));
        let rows: Vec<Value> = probers
            .iter()
            .zip(join_all(asks).await)
            .map(|(p, got)| match got {
                Ok(v) => merged(&json!({"harness": p.tool}), &v),
                Err(e) => json!({"harness": p.tool, "ok": false, "error": e.to_string()}),
            })
            .collect();
        let best = rows
            .iter()
            .find(|r| r.get("answers") == Some(&json!(true)))
            .or_else(|| rows.iter().find(|r| r.get("answers").is_some_and(Value::is_boolean)))
            .unwrap_or(&rows[0]);
        let mut out = best.clone();
        if rows.len() > 1 {
            out["probes"] = json!(rows);
        }
        out
    }

    async fn stop_other(self: &Arc<Self>, frame: &Frame, args: &Value) -> Result<Value, Failure> {
        let (out, named) = self.on_other(frame, args, "stop", "stop_other", json!({"keep": false})).await?;
        let sid = named.get("session_id").and_then(Value::as_str).map(str::to_string);
        Ok(self.after_retire(out, sid.as_deref()).await)
    }

    async fn resume_other(&self, frame: &Frame, args: &Value) -> Result<Value, Failure> {
        let extra = json!({
            "prompt": self.provider_value(frame, args.get("prompt")).await,
            "model": args.get("model"), "effort": args.get("effort"),
        });
        let (out, named) = self.on_other(frame, args, "resume", "resume_other", extra).await?;
        if is_ok(&out) {
            for sid in [named.get("session_id"), out.get("session_id")].into_iter().flatten().filter_map(Value::as_str) {
                self.drop_wait(sid).await;
            }
        }
        Ok(out)
    }

    // --- wait --------------------------------------------------------------------

    async fn wait(&self, frame: &Frame, args: &Value) -> Result<Value, Failure> {
        let raw: Vec<Value> = match args.get("until") {
            Some(Value::String(s)) => s.replace(',', " ").split_whitespace().map(|u| json!(u)).collect(),
            Some(Value::Array(a)) => a.clone(),
            _ => vec![],
        };
        if raw.is_empty() {
            return Ok(reason("until is required: the session ids to wait for"));
        }
        let mut until: Vec<String> = vec![];
        for u in &raw {
            let text_u = u.as_str().map_or_else(|| u.to_string(), |s| s.trim().to_lowercase());
            match u.as_str().and_then(ids::SessionId::checked) {
                Some(id) => {
                    if !until.contains(&id) {
                        until.push(id);
                    }
                }
                _ => {
                    return Ok(reason(&format!(
                        "{} is not an R9 session id; wait takes session ids, as start_other returns them",
                        str_repr(&text_u)
                    )))
                }
            }
        }
        let (p, who) = self.resolve_caller(frame).await?;
        let sid = text(&who, "session_id").unwrap_or_default().to_string();
        if until.contains(&sid) {
            return Ok(reason("a session cannot wait on itself"));
        }
        let mut already = self.retired_here(&until).await;
        already.extend(self.retired_elsewhere(until.clone()).await);
        if until.iter().all(|u| already.contains(u)) {
            return Ok(json!({"ok": true, "waiting": false, "until": until,
                             "reason": "every session in until has already retired; carry on"}));
        }
        let kept = text(&who, "hosting") == Some("background");
        let row = self
            .add_wait(NewWait {
                session_id: sid.clone(),
                until,
                tool: p.tool.clone(),
                kept,
                name: text(&who, "name").unwrap_or_default().into(),
                cwd: text(&who, "cwd").map(str::to_string),
                retired: already,
            })
            .await;
        if !kept {
            return Ok(json!({"ok": true, "waiting": true, "until": row["until"], "retired": row["retired"],
                             "terminal": true, "note": TERMINAL_WAIT}));
        }
        let stop = json!({"session_id": sid, "pid": frame.pid_value(), "keep": true, "confirm": true});
        let out = self.harness_call(&p, "stop", stop, None).await?;
        if !is_ok(&out) {
            self.drop_wait(&sid).await;
            return Ok(merged(&out, &json!({"waiting": false})));
        }
        Ok(merged(&out, &json!({"ok": true, "waiting": true, "until": row["until"], "retired": row["retired"]})))
    }

    async fn retired(&self, args: &Value) -> Value {
        let Some(sid) = args.get("session_id").and_then(Value::as_str).and_then(ids::SessionId::checked) else {
            return reason("session_id is required: an R9 session id");
        };
        json!({"ok": true, "woken": self.retire(&sid, false, |row| self.wake(row)).await})
    }

    async fn retired_among(&self, args: &Value) -> Value {
        let asked: Vec<String> = args
            .get("session_ids")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(|s| s.as_str().and_then(ids::SessionId::checked)).collect())
            .unwrap_or_default();
        let mut gone = self.retired_here(&asked).await;
        let unretired: Vec<&String> = asked.iter().filter(|s| !gone.contains(s)).collect();
        let states = join_all(unretired.iter().map(|s| self.session_state(s))).await;
        let known: Vec<&String> = unretired.into_iter().zip(states).filter(|(_, st)| matches!(st.as_deref(), Some("live" | "stopped"))).map(|(s, _)| s).collect();
        gone.sort();
        gone.dedup();
        json!({"ok": true, "retired": gone, "known": known})
    }

    // --- events from the providers -------------------------------------------------

    /// A provider's report: a command with a follow-up ended (`done`, `lost`), or a session
    /// cleared itself (`clear`). Each report also starts a sweep of the tickets.
    pub async fn harness_event(self: &Arc<Self>, args: &Value) -> Value {
        let event = declared(args);
        let out = match event.get("kind").and_then(Value::as_str) {
            Some(kind) if LIFECYCLE.contains(&kind) => return self.lifecycle(&event).await,
            Some("clear") => self.cleared(&event).await,
            _ => self.record_event(event).await,
        };
        self.kick_sweep();
        out
    }

    /// A provider's lifecycle report, onto `reflection/sessions`. It is not a ticket, so no
    /// ledger row is kept and a provider never resends one.
    async fn lifecycle(&self, event: &Map<String, Value>) -> Value {
        let field = |k: &str| event.get(k).cloned().unwrap_or(Value::Null);
        let place = self.scope_of(event.get("cwd").and_then(Value::as_str)).await;
        let (project, scope) = place.map_or((None, None), |(p, s)| (Some(p), Some(s)));
        let ts = chrono::DateTime::from_timestamp_millis(self.node.now_ms()).map(|t| t.to_rfc3339());
        let mut out = json!({"event": field("kind"), "session_id": field("session_id"), "name": field("name"),
                             "node": self.node.name(), "project": project, "scope": scope, "kind": field("session_kind"), "ts": ts});
        for key in ["task_id", "rc_url", "state", "needs", "retired", "pool"] {
            if let Some(v) = event.get(key).filter(|v| truthy(Some(v))) {
                out[key] = v.clone();
            }
        }
        self.node.emit(SESSIONS_TOPIC, out);
        json!({"ok": true})
    }

    /// A session cleared itself: its new id takes the old one as its parent (sessions.md R13).
    async fn cleared(&self, event: &Map<String, Value>) -> Value {
        let id = |k: &str| event.get(k).and_then(Value::as_str).and_then(ids::SessionId::checked);
        let (Some(new), Some(old)) = (id("session_id"), id("parent")) else {
            return json!({"ok": false, "error": "a clear needs session_id and parent as session ids"});
        };
        json!({"ok": true, "recorded": self.record_parent(&new, &old, "clear").await})
    }

    // --- boot ----------------------------------------------------------------------

    /// What a start does once the service is reachable: read the waits once, waking any whose
    /// sessions retired meanwhile, and sweep the tickets.
    pub async fn boot(self: Arc<Self>) {
        let app = &self;
        let lost = app
            .check_waits(
                |sid| async move { app.state_anywhere(&sid).await },
                |sids| async move { app.retired_elsewhere(sids).await },
                |row| app.wake(row),
            )
            .await;
        if !lost.is_empty() {
            tracing::warn!("reflection: {} waited-on sessions are lost", lost.len());
        }
        self.kick_sweep();
    }
}
