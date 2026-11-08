//! The channel plugin: private channels to the other side of a room or a machine.
//!
//! One binary serves two services and picks one by `AWM_SERVICE_NAME`. `channel` runs on every
//! member node, holds its sessions' channels and supervises `tether-operator`. `relay` runs on
//! the public host, keeps every room and issues every machine slot, and supervises
//! `tether-relay`. `channel issue` is the operator daemon's slot issuer: the daemon runs it once
//! per request, with one JSON object on stdin and one on stdout.

mod channel;
mod db;
mod manifest;
mod relay;
mod tether;

use std::sync::Arc;
use std::time::Duration;

use awm_plugin_sdk::hub::{CallOpts, Hub, HubError};
use awm_plugin_sdk::{Call, Service, VerbError};
use serde_json::{json, Value};

use channel::{Channels, Holder, Parts};
use manifest::VerbSpec;
use relay::store::Member;
use relay::Relay;
use tether::adopted::{self, Adopted, Launch};
use tether::paths::{Paths, Role};
use tether::stream::{Mark, SessionLogs, Stream};

/// The relay's refusal, as the channel half reports it.
fn relay_error(e: HubError) -> String {
    match e {
        HubError::Verb { error, .. } => error,
        HubError::NotInstalled { detail, .. } | HubError::NotUp { detail, .. } | HubError::Http { detail, .. } => detail,
        HubError::Transport { reason, .. } => format!("the relay could not be reached: {reason}"),
    }
}

/// Publish `table`'s verbs of `tool`, each served by `handle`.
fn verbs<F, Fut>(mut svc: Service, tool: &str, description: &str, table: &[VerbSpec], handle: F) -> Service
where
    F: Fn(Call) -> Fut + Clone + Send + Sync + 'static,
    Fut: std::future::Future<Output = Result<Value, VerbError>> + Send + 'static,
{
    svc = svc.api("description", json!(description));
    for v in table {
        let params: Value = serde_json::from_str(v.params).expect("the verb table's params are JSON");
        svc = svc.verb(tool, v.name, v.description, params, handle.clone());
        if let Some(t) = v.timeout_s {
            svc = svc.timeout(Duration::from_secs(t));
        }
        if v.hidden {
            svc = svc.hidden();
        }
    }
    svc
}

// -- the channel half --------------------------------------------------------

/// `_number`: absent or empty is None, and anything else must read as a whole number.
fn number(call: &Call, key: &str) -> Result<Option<i64>, VerbError> {
    match call.args.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if s.is_empty() => Ok(None),
        Some(Value::String(s)) => s.trim().parse().map(Some).map_err(|_| channel::refuse(format!("`{key}` must be a whole number, not '{s}'"))),
        Some(Value::Bool(b)) => Ok(Some(*b as i64)),
        Some(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().map(|f| f.trunc() as i64)).map(Some).ok_or_else(|| channel::refuse(format!("`{key}` must be a whole number, not {n}"))),
        Some(other) => Err(channel::refuse(format!("`{key}` must be a whole number, not {other}"))),
    }
}

fn yes(call: &Call, key: &str) -> bool {
    matches!(call.args.get(key), Some(v) if v == &json!(true) || v == &json!("true") || v == &json!("True") || v == &json!("1") || v == &json!(1))
}

fn name_arg(call: &Call) -> Option<&str> {
    call.str_arg("name").filter(|n| !n.is_empty())
}

async fn holder(call: &Call) -> Result<Holder, VerbError> {
    let h = awm_plugin_sdk::holders::caller(call, "a channel").await?;
    Ok(Holder {
        node: h.node,
        session: h.session,
        job: h.job,
        name: h.name,
        level: call.caller_trust().filter(|t| !t.is_empty()).unwrap_or("untrusted").to_string(),
        isolated: awm_plugin_sdk::ids::to_r9(call.caller_session_id()).is_some(),
    })
}

async fn channel_verb(ch: Arc<Channels>, call: Call) -> Result<Value, VerbError> {
    let who = holder(&call).await?;
    let text = channel::text(&call.args, "text");
    match call.verb.as_str() {
        "open" => ch.open(&who, &channel::text(&call.args, "kind"), call.str_arg("os").filter(|o| !o.is_empty()), yes(&call, "persistent")).await,
        "join" => ch.join(&who, &channel::text(&call.args, "name")).await,
        "write" => {
            let w = channel::Write {
                target: Some(channel::text(&call.args, "target")).filter(|t| !t.is_empty()).unwrap_or_else(|| "say".into()),
                keys: yes(&call, "keys"),
                task: number(&call, "task")?,
                terminal: yes(&call, "terminal"),
                limit_s: number(&call, "limit_s")?,
                cols: number(&call, "cols")?,
                rows: number(&call, "rows")?,
            };
            ch.write(&who, &text, name_arg(&call), w).await
        }
        "drain" => {
            let wait = number(&call, "wait")?.unwrap_or(0) as f64;
            ch.drain(&who, name_arg(&call), wait, number(&call, "until")?, number(&call, "task")?, yes(&call, "raw")).await
        }
        "status" => ch.status(&who, name_arg(&call)).await,
        "encrypt" => ch.encrypt(&who, name_arg(&call)).await,
        "close" => ch.close(&who, name_arg(&call), number(&call, "task")?).await,
        other => Err(channel::refuse(format!("unknown verb {other}"))),
    }
}

fn channel_service(svc: Service, ch: Arc<Channels>) -> Service {
    verbs(svc, "channel", manifest::CHANNEL_DESCRIPTION, manifest::CHANNEL_VERBS, move |call| channel_verb(ch.clone(), call))
}

/// The argv the operator daemon runs for each slot: this plugin's installed binary.
fn issuer_argv() -> Vec<String> {
    let installed = awm_plugin_sdk::config::awm_dir().join("bin").join("channel");
    let bin = if installed.is_file() { installed } else { std::env::current_exe().unwrap_or(installed) };
    vec![bin.display().to_string(), "issue".into()]
}

/// The tether binary of `paths`' role: fetched at its pin, then adopted for good.
fn supervise(ctx: &awm_plugin_sdk::Context, paths: Paths, child: Arc<Adopted>) {
    ctx.spawn("tether", async move {
        let client = reqwest::Client::builder().timeout(Duration::from_secs(200)).build()?;
        if let Err(e) = tether::fetch::fetch(&paths, &tether::fetch::Source::from_env(), &client).await {
            tracing::warn!("tether fetch failed, keeping the binaries in place: {e}");
        }
        child.supervise_forever().await;
        Ok(())
    });
}

async fn run_channel(svc: Service) -> anyhow::Result<()> {
    let ctx = svc.context();
    let awm_dir = awm_plugin_sdk::config::awm_dir();
    let paths = Paths::new(Role::Operator, &awm_dir);
    let child = Adopted::new(paths.clone(), adopted::operator_probe(paths.control_socket()), Launch::from_env(&paths, Some(issuer_argv())));
    let stream = Stream::new(Mark::load(&paths.stream_state()), SessionLogs::new(paths.sessions_dir()));
    let machines = Arc::new(channel::machines::Machines::new(stream.clone(), Some(child.clone()), paths.clone(), channel::machines::socket_control(&paths)));
    let dir = awm_plugin_sdk::config::service_dir("channel");
    let hub = ctx.hub().clone();
    let (h1, h2, h3) = (hub.clone(), hub.clone(), hub.clone());
    let channels = Channels::new(Parts {
        here: ctx.env().node.clone(),
        store: channel::store::Store::open(&dir.join("channels.db"))?,
        relay: Arc::new(move |verb, args, timeout| {
            let hub = h1.clone();
            Box::pin(async move { hub.call_with("relay", verb, args, &CallOpts { timeout, ..CallOpts::default() }).await.map_err(relay_error) })
        }),
        machines,
        send: Arc::new(move |node, job, text| {
            let hub = h2.clone();
            Box::pin(async move { awm_plugin_sdk::push::reflect(&hub, &node, &job, &text).await })
        }),
        transcripts: channel::lines::Transcripts { root: dir.join("transcripts") },
        list_sessions: Arc::new(move || {
            let hub = h3.clone();
            Box::pin(async move { awm_plugin_sdk::holders::live_sessions(&hub).await })
        }),
        feed_wait_s: channel::FEED_WAIT_S,
    });
    let (ready, down) = (channels.clone(), channels.clone());
    let socket = paths.control_socket();
    supervise(&ctx, paths, child);
    let svc = channel_service(svc, channels)
        .on_ready(async move {
            tokio::spawn(stream.clone().watch_forever(socket));
            ready.resume();
            tokio::spawn(ready.clone().feed_forever());
            let prune = ready.clone();
            let watch = awm_plugin_sdk::sessions::SessionWatch::new(hub, "channel", move |_node| {
                let prune = prune.clone();
                async move {
                    prune.prune().await;
                }
            });
            watch.ensure(&awm_plugin_sdk::config::node_name());

        })
        .on_shutdown(async move {
            down.stop();
            down.machines.stream.shutdown();
        });
    svc.run().await
}

// -- the relay half ----------------------------------------------------------

const RESTORE_KEYS: [&str; 3] = ["slot", "token", "owner"];

/// The calling node, once the call is shown to be that node's own service.
fn node_of(call: &Call) -> Result<String, VerbError> {
    if call.args.get("_caller_session").is_some_and(|c| !c.is_null()) {
        return Err(relay::refuse("a session holds rooms through its own node's `channel` tool, never through relay"));
    }
    if call.caller_trust() != Some("trusted") {
        return Err(relay::refuse("only a member node's channel service calls relay"));
    }
    Ok(call.peer_node().map(str::to_string).unwrap_or_else(|| call.ctx.env().node.clone()))
}

fn member_of(call: &Call) -> Result<Member, VerbError> {
    let node = node_of(call)?;
    let raw = call.args.get("member").filter(|m| m.is_object());
    let session = raw.and_then(|m| m.get("session")).and_then(Value::as_str).filter(|s| !s.is_empty());
    let (Some(raw), Some(session)) = (raw, session) else {
        return Err(relay::refuse("`member` names the session this call acts for"));
    };
    let name = channel::text(raw, "name");
    let level = Some(channel::text(raw, "level")).filter(|l| !l.is_empty()).unwrap_or_else(|| "untrusted".into());
    let who = format!("{}@{node}", if name.is_empty() { session } else { &name });
    Ok(Member { node, session: session.to_string(), name, level, who })
}

async fn relay_verb(r: Arc<Relay>, child: Arc<Adopted>, call: Call) -> Result<Value, VerbError> {
    let name = channel::text(&call.args, "name");
    match call.verb.as_str() {
        "open" => {
            let persistent = matches!(call.args.get("persistent"), Some(v) if v == &json!(true) || v == &json!("true") || v == &json!(1));
            r.open(&member_of(&call)?, persistent).await
        }
        "join" => r.join(&member_of(&call)?, &name).await,
        "write" => {
            let message = call.args.get("message").filter(|m| m.is_object()).cloned().ok_or_else(|| relay::refuse("`message` is an object"))?;
            r.write(&member_of(&call)?, &name, &message).await
        }
        "close" => r.close(&member_of(&call)?, &name).await,
        "status" => Ok(r.status(&member_of(&call)?)),
        "feed" => {
            let live = match call.args.get("live") {
                None | Some(Value::Null) => None,
                Some(Value::Array(list)) => Some(list.iter().map(|s| s.as_str().map(str::to_string).unwrap_or_else(|| s.to_string())).collect()),
                Some(_) => return Err(relay::refuse("`live` is a list of session ids, or absent")),
            };
            let acks = match call.args.get("acks") {
                None | Some(Value::Null) => vec![],
                Some(Value::Array(a)) if a.iter().all(Value::is_object) => a.clone(),
                Some(_) => return Err(relay::refuse("`acks` is a list of {room, session, through}")),
            };
            let wait = call.args.get("wait").and_then(Value::as_f64).unwrap_or(0.0);
            Ok(r.feed(&node_of(&call)?, live, &acks, wait).await)
        }
        "issue" => {
            let node = node_of(&call)?;
            let given: Vec<&str> = RESTORE_KEYS.iter().copied().filter(|k| call.args.get(*k).is_some_and(|v| !v.is_null() && v != "")).collect();
            if given.is_empty() {
                return r.issue(&node, None).await;
            }
            if given.len() < RESTORE_KEYS.len() {
                return Err(relay::refuse("a restore names `slot`, `token` and `owner`"));
            }
            let restore: serde_json::Map<String, Value> = RESTORE_KEYS.iter().map(|k| (k.to_string(), json!(channel::text(&call.args, k)))).collect();
            r.issue(&node, Some(Value::Object(restore))).await
        }
        "tether" => {
            node_of(&call)?;
            Ok(json!({"role": "relay", "release": child.paths.release_report(), "child": child.snapshot().await, "log": child.paths.daemon_log()}))
        }
        other => Err(relay::refuse(format!("unknown verb {other}"))),
    }
}

fn relay_service(svc: Service, r: Arc<Relay>, child: Arc<Adopted>) -> Service {
    verbs(svc, "relay", manifest::RELAY_DESCRIPTION, manifest::RELAY_VERBS, move |call| relay_verb(r.clone(), child.clone(), call))
}

async fn run_relay(svc: Service) -> anyhow::Result<()> {
    let ctx = svc.context();
    let paths = Paths::new(Role::Relay, &awm_plugin_sdk::config::awm_dir());
    let rooms = relay::rooms::Rooms::new(None, None, None);
    let mut launch = Launch::from_env(&paths, None);
    if let Some(bearer) = rooms.bearer().filter(|_| launch.secrets.is_empty()) {
        launch.secrets.push((relay::rooms::BEARER_ENV.into(), bearer));
    }
    let child = Adopted::new(paths.clone(), adopted::relay_probe(relay::rooms::default_base(), rooms.bearer()), launch);
    let store = relay::store::Store::open(&awm_plugin_sdk::config::service_dir("relay").join("channels.db"))?;
    let r = Relay::new(store, rooms, Relay::lease_from_env());
    let (ready, down) = (r.clone(), r.clone());
    supervise(&ctx, paths, child.clone());
    let svc = relay_service(svc, r, child)
        .on_ready(async move {
            ready.resume();
            tokio::spawn(ready.clone().prune_forever());
        })
        .on_shutdown(async move { down.stop() });
    svc.run().await
}

// -- the issuer --------------------------------------------------------------

/// One slot request from the operator daemon, answered through `relay issue` on the channel
/// service's account. The bearer that gates the tether relay never leaves the public host.
async fn issue() -> i32 {
    use std::io::Read;
    let mut raw = String::new();
    let _ = std::io::stdin().read_to_string(&mut raw);
    let answer = async {
        let request: Value = serde_json::from_str(if raw.trim().is_empty() { "{}" } else { &raw }).map_err(|_| "the request is a JSON object".to_string())?;
        if !request.is_object() {
            return Err("the request is a JSON object".to_string());
        }
        let opts = CallOpts { timeout: Duration::from_secs(90), ..CallOpts::default() };
        let result = Hub::from_env().call_with("relay", "issue", request, &opts).await.map_err(|e| format!("relay issue: {}", relay_error(e).trim()))?;
        if !result.is_object() {
            return Err(format!("relay issue answered {result}"));
        }
        Ok(result)
    };
    match answer.await {
        Ok(v) => {
            println!("{v}");
            0
        }
        Err(e) => {
            eprintln!("{e}");
            1
        }
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    if std::env::args().nth(1).as_deref() == Some("issue") {
        std::process::exit(issue().await);
    }
    awm_plugin_sdk::telemetry::init();
    let awm_dir = awm_plugin_sdk::config::awm_dir();
    awm_statecheck::cli::state_subcommand("channel", include_str!("../state.toml"), &awm_plugin_sdk::config::service_dir("channel"), &awm_dir)?;
    let svc = Service::from_env()?.state(include_str!("../state.toml"));
    let service = svc.context().env().service.clone();
    match service.as_str() {
        "relay" => run_relay(svc).await,
        _ => run_channel(svc).await,
    }
}

#[cfg(test)]
mod tests;
