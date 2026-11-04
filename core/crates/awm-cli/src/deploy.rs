//! `awm deploy`: the node-local step of a deploy (routines.md R32). It brings this node's
//! running awm in line with its checkout: install the plugins' envs, build the pages, reload
//! the plugins with no gateway restart (R144), and check every enabled service came up.
//! A changed core binary needs `--restart`, which restarts the `awm` user unit.

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

#[derive(clap::Args, Debug)]
pub struct Args {
    /// The checkout to deploy from. Default: $AWM_WORKSPACE, else the first parent of the
    /// working directory that holds awm/install.sh.
    #[arg(long)]
    root: Option<PathBuf>,
    /// Skip awm/install.sh.
    #[arg(long)]
    no_install: bool,
    /// Skip the page build.
    #[arg(long)]
    no_build: bool,
    /// Restart the `awm` user unit instead of a reload: the core binary changed.
    #[arg(long)]
    restart: bool,
    /// Print the steps and change nothing.
    #[arg(long)]
    dry_run: bool,
    /// How long to wait for the services to come up, in seconds.
    #[arg(long, default_value_t = 90)]
    timeout: u64,
}

fn find_root(explicit: Option<PathBuf>) -> anyhow::Result<PathBuf> {
    let holds = |p: &Path| p.join("awm/install.sh").is_file();
    if let Some(r) = explicit.or_else(|| std::env::var_os("AWM_WORKSPACE").map(PathBuf::from)) {
        anyhow::ensure!(holds(&r), "{} holds no awm/install.sh", r.display());
        return Ok(r);
    }
    let cwd = std::env::current_dir()?;
    cwd.ancestors().find(|p| holds(p)).map(Path::to_path_buf).ok_or_else(|| {
        anyhow::anyhow!("no awm checkout above {}: pass --root or set AWM_WORKSPACE", cwd.display())
    })
}

fn step(step: &str, cmd: &mut std::process::Command) -> anyhow::Result<()> {
    println!("-> {step}");
    let status = cmd.status().map_err(|e| anyhow::anyhow!("{step}: {e}"))?;
    anyhow::ensure!(status.success(), "{step} failed ({status})");
    Ok(())
}

async fn native(verb: &str) -> anyhow::Result<Value> {
    let url = format!("{}/invoke", crate::gateway_url());
    let resp = reqwest::Client::new()
        .post(&url)
        .header("X-Awm-Surface", "cli")
        .header("X-Awm-Caller-Pid", std::process::id().to_string())
        .json(&json!({"name": awm_gateway::gateway::NATIVE_TOOL, "args": {"verb": verb, "args": {}}}))
        .send()
        .await?;
    let ok = resp.status().is_success();
    let body: Value = resp.json().await.unwrap_or(Value::Null);
    anyhow::ensure!(ok, "awm {verb}: {}", body.get("detail").unwrap_or(&body));
    let text = body.get("result").and_then(Value::as_str).unwrap_or("null");
    Ok(serde_json::from_str(text).unwrap_or(Value::String(text.into())))
}

pub async fn run(args: Args) -> anyhow::Result<i32> {
    let root = find_root(args.root)?;
    println!("deploy target: {}", root.display());
    println!("  install: {}", if args.no_install { "no" } else { "awm/install.sh" });
    println!("  build:   {}", if args.no_build { "no" } else { "awm/scripts/build.sh" });
    println!("  apply:   {}", if args.restart { "restart the awm user unit" } else { "awm reload" });
    if args.dry_run {
        println!("--- dry run: nothing changed ---");
        return Ok(0);
    }
    if !args.no_install {
        step("install", std::process::Command::new("bash").arg(root.join("awm/install.sh")).current_dir(&root))?;
    }
    if !args.no_build {
        step("build pages", std::process::Command::new("bash").arg(root.join("awm/scripts/build.sh")).current_dir(&root))?;
    }
    if args.restart {
        step("restart", std::process::Command::new("systemctl").args(["--user", "restart", "awm"]))?;
    } else {
        let report = native("reload").await?;
        println!("-> reload: {}", serde_json::to_string(&report)?);
    }
    let deadline = Instant::now() + Duration::from_secs(args.timeout);
    loop {
        let waiting: Vec<String> = match native("services").await {
            Ok(rows) => rows
                .as_array()
                .into_iter()
                .flatten()
                .filter(|r| r["enabled"] == json!(true) && r["external"] != json!(true) && r["status"] != json!("ready"))
                .map(|r| format!("{} ({})", r["name"].as_str().unwrap_or("?"), r["status"].as_str().unwrap_or("?")))
                .collect(),
            Err(e) => vec![format!("gateway: {e}")],
        };
        if waiting.is_empty() {
            break;
        }
        if Instant::now() > deadline {
            eprintln!("not up after {}s: {}", args.timeout, waiting.join(", "));
            return Ok(1);
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
    let plugins = native("plugins").await?;
    let refused: Vec<String> = plugins["refused"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|r| format!("{}: {}", r["plugin"].as_str().unwrap_or("?"), r["reason"].as_str().unwrap_or("?")))
        .collect();
    if !refused.is_empty() {
        eprintln!("refused plugins:\n  {}", refused.join("\n  "));
        return Ok(1);
    }
    println!("deployed: every enabled service is ready");
    Ok(0)
}
