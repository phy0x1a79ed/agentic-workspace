//! The `awm` command line. Every command but `gateway serve` talks to a running gateway.

use std::path::{Path, PathBuf};

use clap::{Parser, Subcommand};
use serde_json::{json, Map, Value};

pub mod bootstrap;

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
    /// Load the plugins again and apply the change, with no gateway restart (R144).
    Reload,
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

/// The gateway URL: `AWM_GATEWAY_URL`, else `<AWM_DIR>/gateway.json`, else the default port.
pub fn gateway_url() -> String {
    if let Ok(url) = std::env::var("AWM_GATEWAY_URL") {
        return url;
    }
    if let Some(dir) = std::env::var_os("AWM_DIR") {
        if let Some(url) = read_gateway_json(Path::new(&dir)) {
            return url;
        }
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
            let gw = awm_gateway::Gateway::open(&state).await?;
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
        Command::Reload => post_and_print("/hub/reload").await,
        Command::Tool(words) => run_tool(&words).await,
    }
}

async fn post_and_print(path: &str) -> anyhow::Result<i32> {
    let url = format!("{}{path}", gateway_url());
    let resp = reqwest::Client::new().post(&url).send().await.map_err(|e| anyhow::anyhow!("awm gateway unreachable at {url}: {e}"))?;
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

/// Parse `<tool> <verb> [--key value]...` into the `/invoke` body.
pub fn parse_tool_call(words: &[String]) -> anyhow::Result<Value> {
    let [tool, rest @ ..] = words else { anyhow::bail!("usage: awm <tool> <verb> [--key value]...") };
    let (verb, flags) = match rest {
        [verb, flags @ ..] if !verb.starts_with("--") => (verb.as_str(), flags),
        _ => ("describe", rest),
    };
    let mut args = Map::new();
    let mut peer = None;
    let mut i = 0;
    while i < flags.len() {
        let Some(flag) = flags[i].strip_prefix("--") else { anyhow::bail!("unexpected argument {:?}", flags[i]) };
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
    let body = parse_tool_call(words)?;
    let url = format!("{}/invoke", gateway_url());
    let resp = reqwest::Client::new()
        .post(&url)
        .header("X-Awm-Surface", "cli")
        .json(&body)
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
        eprintln!("awm: {} {}", status.as_u16(), match detail {
            Value::String(s) => s,
            other => serde_json::to_string_pretty(&other)?,
        });
        Ok(1)
    }
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
    }
}
