//! The `awm` command line. Every command but `gateway serve` talks to a running gateway.

use std::path::{Path, PathBuf};

use clap::{Parser, Subcommand};
use serde_json::{json, Map, Value};

pub mod bootstrap;
pub mod deploy;
pub mod edge;

#[derive(Parser, Debug)]
#[command(name = "awm", version, about = "awm: the agentic workspace core", disable_help_subcommand = true)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Command,
}

/// Each arm is one top-level command. Any other first word is a tool: `awm <tool> <verb> ...`.
#[derive(Subcommand, Debug)]
pub enum Command {
    /// Run the gateway.
    Gateway {
        #[command(subcommand)]
        action: GatewayAction,
    },
    /// Serve MCP over stdio, forwarding to the gateway.
    Mcp,
    /// Prepare a state directory from an install config.
    Bootstrap(bootstrap::Args),
    /// Usage per plugin and surface.
    Usage {
        #[arg(long, default_value_t = 60)]
        days: i64,
    },
    /// The gateway's status.
    Status,
    /// What the loader loaded, refused and skipped.
    Plugins,
    /// Load the plugins again with no gateway restart (R144). Starts added plugins and stops
    /// removed or disabled ones. Restarts only the plugins `--restart` names.
    Reload {
        /// Plugins to restart as well, comma separated.
        #[arg(long, value_delimiter = ',')]
        restart: Vec<String>,
        /// Bypass the crash breaker for the restarted plugins.
        #[arg(long)]
        force: bool,
    },
    /// Restart one plugin through the reload path. `--force` bypasses the crash breaker.
    Restart {
        plugin: String,
        #[arg(long)]
        force: bool,
    },
    /// Stop the gateway and every service it runs. A restart (systemctl restart awm) leaves the
    /// services running for the next gateway to adopt; only this command stands them down.
    Stop,
    /// Show whether the gateway is in hold mode, or `awm hold leave` to end it. The gateway
    /// holds when it restarts more than 3 times in 10 minutes: it adopts services and spawns none.
    Hold {
        /// `leave` ends hold mode and starts the services that are down.
        action: Option<String>,
    },
    /// Install or update plugins from folders or git:<url>@<commit>, as one change.
    Install {
        sources: Vec<String>,
        /// The approved request id of a git change.
        #[arg(long)]
        approval: Option<String>,
    },
    /// Remove an installed plugin.
    Uninstall { name: String },
    /// List the services, or start, stop, restart, enable or disable one.
    Services {
        /// list, start, stop, restart, enable or disable.
        #[arg(default_value = "list")]
        action: String,
        name: Option<String>,
    },
    /// Print each payload a service emits on a topic, one JSON line each.
    Listen {
        service: String,
        topic: String,
        /// Listen on another node, over the edge.
        #[arg(long)]
        peer: Option<String>,
        /// Print only payloads whose `key` equals `value` (repeatable, all must match).
        #[arg(long = "match", value_name = "KEY=VALUE")]
        matches: Vec<String>,
    },
    /// Refresh this node from its checkout: install the plugins' envs, build the pages, reload.
    Deploy(deploy::Args),
    /// Serve calls from another node over ssh, as `ssh <node> awm edge-serve --from <caller>`.
    EdgeServe {
        /// The calling node. A user key's login names it. A service key's forced command names it.
        #[arg(long)]
        from: Option<String>,
        /// `user` (the default) or `service`. Only a service key's forced command says `service`.
        #[arg(long)]
        level: Option<String>,
    },
    /// One call through the service door: the forced command of a service key.
    Port {
        /// The key's name, `awm:<holder>:service`.
        #[arg(long)]
        key: String,
    },
    /// Run a verb of a tool: awm <tool> <verb> [--key value]... [--json '{..}'] [--peer <node>]
    #[command(external_subcommand)]
    Tool(Vec<String>),
}

#[derive(Subcommand, Debug)]
pub enum GatewayAction {
    /// Serve on the install config's `listen` address until SIGINT or SIGTERM.
    Serve {
        /// The state directory (AWM_DIR) that holds config.toml.
        #[arg(long, env = "AWM_DIR")]
        state: PathBuf,
    },
}

/// This node's state directory: `AWM_DIR`, else `<AWM_WORKSPACE>/.awm`, else
/// `~/agentic_workspace/.awm`, the layout the user unit sets. An ssh login into
/// `awm edge-serve` carries none of these variables.
pub fn awm_dir() -> PathBuf {
    if let Some(dir) = std::env::var_os("AWM_DIR").filter(|d| !d.is_empty()) {
        return PathBuf::from(dir);
    }
    if let Some(ws) = std::env::var_os("AWM_WORKSPACE").filter(|d| !d.is_empty()) {
        return PathBuf::from(ws).join(".awm");
    }
    let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default();
    home.join("agentic_workspace").join(".awm")
}

/// The gateway URL: `AWM_GATEWAY_URL`, else `<awm_dir>/gateway.json`, else the default port.
pub fn gateway_url() -> String {
    if let Ok(url) = std::env::var("AWM_GATEWAY_URL") {
        return url;
    }
    if let Some(url) = read_gateway_json(&awm_dir()) {
        return url;
    }
    format!("http://{}", awm_gateway::config::DEFAULT_LISTEN)
}

fn read_gateway_json(dir: &Path) -> Option<String> {
    let text = std::fs::read_to_string(dir.join("gateway.json")).ok()?;
    serde_json::from_str::<Value>(&text).ok()?.get("url")?.as_str().map(str::to_string)
}

pub async fn run(cli: Cli) -> anyhow::Result<i32> {
    match cli.command {
        Command::Gateway { action: GatewayAction::Serve { state } } => {
            let gw = edge::open_gateway(&state).await?;
            awm_gateway::server::serve(gw).await?;
            Ok(0)
        }
        Command::Mcp => {
            awm_mcp::run_stdio(&gateway_url()).await?;
            Ok(0)
        }
        Command::Bootstrap(args) => bootstrap::run(args),
        Command::Usage { days } => get_and_print(&format!("/usage?days={days}")).await,
        Command::Status => get_and_print("/status").await,
        Command::Plugins => get_and_print("/hub/plugins").await,
        Command::Reload { restart, force } => post_and_print("/hub/reload", json!({"restart": restart, "force": force})).await,
        Command::Restart { plugin, force } => post_and_print("/hub/reload", json!({"restart": [plugin], "force": force})).await,
        Command::Stop => stop().await,
        Command::Hold { action } => hold(action.as_deref()).await,
        Command::Install { sources, approval } => {
            let sources: Vec<String> = sources
                .into_iter()
                .map(|s| match std::path::Path::new(&s).canonicalize() {
                    Ok(p) if !s.contains(':') => p.display().to_string(),
                    _ => s,
                })
                .collect();
            post_and_print("/hub/install", json!({"sources": sources, "approval": approval})).await
        }
        Command::Uninstall { name } => post_and_print("/hub/uninstall", json!({"name": name})).await,
        Command::Services { action, name } => services(&action, name).await,
        Command::Listen { service, topic, peer, matches } => listen(&service, &topic, peer, &matches).await,
        Command::Deploy(args) => deploy::run(args).await,
        Command::EdgeServe { from, level } => edge::edge_serve(from.as_deref(), level.as_deref()).await,
        Command::Port { key } => edge::port(&key).await,
        Command::Tool(words) => run_tool(&words).await,
    }
}

async fn post_and_print(path: &str, body: Value) -> anyhow::Result<i32> {
    let url = format!("{}{path}", gateway_url());
    let resp = reqwest::Client::new().post(&url).json(&body).send().await.map_err(|e| anyhow::anyhow!("awm gateway unreachable at {url}: {e}"))?;
    let ok = resp.status().is_success();
    let body: Value = resp.json().await?;
    println!("{}", serde_json::to_string_pretty(&body)?);
    Ok(if ok { 0 } else { 1 })
}

async fn get_and_print(path: &str) -> anyhow::Result<i32> {
    let url = format!("{}{path}", gateway_url());
    let resp = reqwest::get(&url).await.map_err(|e| anyhow::anyhow!("awm gateway unreachable at {url}: {e}"))?;
    let ok = resp.status().is_success();
    let body: Value = resp.json().await?;
    println!("{}", serde_json::to_string_pretty(&body)?);
    Ok(if ok { 0 } else { 1 })
}

/// `awm stop`: ask the gateway to drain, then wait until it has exited.
async fn stop() -> anyhow::Result<i32> {
    let body = json!({"name": awm_gateway::gateway::NATIVE_TOOL, "args": {"verb": "drain", "args": {}}});
    if invoke_and_print(&body).await? != 0 {
        return Ok(1);
    }
    let marker = awm_dir().join("gateway.json");
    for _ in 0..120 {
        if !marker.exists() {
            println!("gateway stopped");
            return Ok(0);
        }
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }
    eprintln!("awm stop: the gateway is still draining after 60s");
    Ok(1)
}

/// `awm hold`: the hold flag from `/status`, or `leave` to end hold mode.
async fn hold(action: Option<&str>) -> anyhow::Result<i32> {
    match action {
        None => {
            let url = format!("{}/status", gateway_url());
            let status: Value = reqwest::get(&url).await.map_err(|e| anyhow::anyhow!("awm gateway unreachable at {url}: {e}"))?.json().await?;
            println!("hold: {}", status["hold"].as_bool().unwrap_or(false));
            Ok(0)
        }
        Some("leave") => invoke_and_print(&json!({"name": awm_gateway::gateway::NATIVE_TOOL, "args": {"verb": "hold_leave", "args": {}}})).await,
        Some(other) => anyhow::bail!("awm hold: {other:?} is not `leave`"),
    }
}

/// `awm services`: the `awm` tool's service verbs.
async fn services(action: &str, name: Option<String>) -> anyhow::Result<i32> {
    const ACTIONS: [&str; 6] = ["list", "start", "stop", "restart", "enable", "disable"];
    if !ACTIONS.contains(&action) {
        anyhow::bail!("awm services: {action:?} is not one of {}", ACTIONS.join(", "));
    }
    let (verb, args) = match (action, name) {
        ("list", _) => ("services", json!({})),
        (_, Some(n)) => (action, json!({"name": n})),
        (_, None) => anyhow::bail!("awm services {action} needs a service name"),
    };
    let body = json!({"name": awm_gateway::gateway::NATIVE_TOOL, "args": {"verb": verb, "args": args}});
    invoke_and_print(&body).await
}

/// `awm listen`: one JSON line per payload, until the gateway closes the stream.
async fn listen(service: &str, topic: &str, peer: Option<String>, matches: &[String]) -> anyhow::Result<i32> {
    let mut wanted = Vec::new();
    for m in matches {
        let Some((k, v)) = m.split_once('=') else { anyhow::bail!("--match takes KEY=VALUE, not {m:?}") };
        wanted.push((k.to_string(), v.to_string()));
    }
    let mut url = format!("{}/hub/emit/{service}/{topic}", gateway_url().replacen("http", "ws", 1));
    if let Some(p) = peer {
        url.push_str(&format!("?peer={p}"));
    }
    let mut pipe = awm_gateway::peer::connect_ws(&url, &[("x-awm-caller-pid", std::process::id().to_string())])
        .await
        .map_err(|e| anyhow::anyhow!("awm listen {service}/{topic}: {e}"))?;
    while let Some(msg) = pipe.rx.recv().await {
        match msg {
            awm_proto::WsMsg::Text(t) => {
                let v: Value = serde_json::from_str(&t).unwrap_or(Value::String(t.clone()));
                let hit = wanted.iter().all(|(k, want)| match v.get(k) {
                    Some(Value::String(s)) => s == want,
                    Some(other) => want.eq(&other.to_string()),
                    None => false,
                });
                if hit {
                    println!("{}", serde_json::to_string(&v)?);
                }
            }
            awm_proto::WsMsg::Close(Some(why)) => {
                eprintln!("awm listen: closed: {why}");
                return Ok(1);
            }
            awm_proto::WsMsg::Close(None) => break,
            awm_proto::WsMsg::Binary(_) => {}
        }
    }
    Ok(0)
}

/// Parse `<tool> <verb> [--key value]...` into the `/invoke` body. A verb word may spell
/// its underscores as hyphens.
pub fn parse_tool_call(words: &[String]) -> anyhow::Result<Value> {
    let [tool, rest @ ..] = words else { anyhow::bail!("usage: awm <tool> <verb> [--key value]...") };
    let verb_word;
    let (verb, flags) = match rest {
        [verb, flags @ ..] if !verb.starts_with("--") => {
            verb_word = verb.replace('-', "_");
            (verb_word.as_str(), flags)
        }
        _ => ("describe", rest),
    };
    let mut args = Map::new();
    let mut peer = None;
    let mut i = 0;
    while i < flags.len() {
        let Some(flag) = flags[i].strip_prefix("--") else {
            anyhow::bail!("unexpected argument {:?}: pass arguments as --name value, or `--json '{{...}}'`", flags[i])
        };
        let (key, inline) = match flag.split_once('=') {
            Some((k, v)) => (k.to_string(), Some(v.to_string())),
            None => (flag.to_string(), None),
        };
        let value = match inline {
            Some(v) => v,
            None if flags.get(i + 1).is_some_and(|n| !n.starts_with("--")) => {
                i += 1;
                flags[i].clone()
            }
            None => "true".into(),
        };
        match key.as_str() {
            "peer" => peer = Some(value),
            "json" => {
                let Value::Object(m) = serde_json::from_str(&value)? else { anyhow::bail!("--json takes an object") };
                args.extend(m);
            }
            _ => {
                let parsed = serde_json::from_str::<Value>(&value).unwrap_or(Value::String(value));
                args.insert(key.replace('-', "_"), parsed);
            }
        }
        i += 1;
    }
    let mut envelope = json!({"verb": verb, "args": args});
    if let Some(p) = peer {
        envelope["peer"] = json!(p);
    }
    Ok(json!({"name": tool, "args": envelope}))
}

async fn run_tool(words: &[String]) -> anyhow::Result<i32> {
    let help = words.iter().any(|w| w == "--help" || w == "-h");
    let words: Vec<String> = words.iter().filter(|w| *w != "--help" && *w != "-h").cloned().collect();
    let mut body = parse_tool_call(&words)?;
    if help {
        // `awm <tool> <verb> --help` prints the verb's schema instead of running it.
        let verb = body["args"]["verb"].as_str().unwrap_or(awm_gateway::gateway::DESCRIBE).to_string();
        let filter = if verb == awm_gateway::gateway::DESCRIBE { json!({}) } else { json!({"verbs": [verb]}) };
        body["args"] = json!({"verb": awm_gateway::gateway::DESCRIBE, "args": filter, "peer": body["args"]["peer"]});
    }
    invoke_and_print(&body).await
}

async fn invoke_and_print(body: &Value) -> anyhow::Result<i32> {
    let url = format!("{}/invoke", gateway_url());
    let resp = reqwest::Client::new()
        .post(&url)
        .header("X-Awm-Surface", "cli")
        // The gateway walks this pid's ancestors to the session, if any, that runs the CLI.
        .header("X-Awm-Caller-Pid", std::process::id().to_string())
        .json(body)
        .send()
        .await
        .map_err(|e| anyhow::anyhow!("awm gateway unreachable at {url}: {e}"))?;
    let status = resp.status();
    let body: Value = resp.json().await.unwrap_or(Value::Null);
    if status.is_success() {
        println!("{}", body.get("result").and_then(Value::as_str).unwrap_or_default());
        Ok(0)
    } else {
        let detail = body.get("detail").cloned().unwrap_or(body);
        let text = match detail {
            Value::String(s) => s,
            other => serde_json::to_string_pretty(&other)?,
        };
        eprintln!("awm: {} {text}", status.as_u16());
        if let Some(hint) = missing_flags_hint(&text) {
            eprintln!("awm: pass {hint}");
        }
        Ok(1)
    }
}

/// The flags a verb's "missing required argument(s)" refusal asks for, spelled for this CLI.
fn missing_flags_hint(error: &str) -> Option<String> {
    let (_, tail) = error.split_once("missing required argument(s): ")?;
    let (names, _) = tail.split_once('.')?;
    Some(names.split(", ").map(|n| format!("--{n} <value>")).collect::<Vec<_>>().join(" "))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn w(s: &str) -> Vec<String> {
        s.split_whitespace().map(str::to_string).collect()
    }

    #[test]
    fn parses_tool_calls() {
        let b = parse_tool_call(&w("journal post --project awm --scope redux-core --limit 5 --dry-run --peer alcor")).unwrap();
        assert_eq!(b["name"], "journal");
        assert_eq!(b["args"]["verb"], "post");
        assert_eq!(b["args"]["args"]["limit"], 5);
        assert_eq!(b["args"]["args"]["dry_run"], true);
        assert_eq!(b["args"]["args"]["project"], "awm");
        assert_eq!(b["args"]["peer"], "alcor");
        let b = parse_tool_call(&w("journal")).unwrap();
        assert_eq!(b["args"]["verb"], "describe");
        let b = parse_tool_call(&["x".into(), "v".into(), "--json".into(), r#"{"a":[1]}"#.into(), "--b=c".into()]).unwrap();
        assert_eq!(b["args"]["args"]["a"][0], 1);
        assert_eq!(b["args"]["args"]["b"], "c");
        assert!(parse_tool_call(&w("x v stray")).is_err());
        assert_eq!(parse_tool_call(&w("scope git-gather --hub x")).unwrap()["args"]["verb"], "git_gather");
        let refusal = r#"{"error": "connect: missing required argument(s): host, port. Ask Tony."}"#;
        assert_eq!(missing_flags_hint(refusal).unwrap(), "--host <value> --port <value>");
        assert_eq!(missing_flags_hint("connect: refused"), None);
    }
}
