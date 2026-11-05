//! `awm deploy`: the node-local step of a deploy (routines.md R32). It brings this node's
//! running awm in line with its checkout: the plugins' envs, the pages, the core binary at
//! ~/.local/bin/awm, the gateway's config.toml and the `awm` user unit. It reloads the plugins
//! with no gateway restart (R144), or restarts the unit when the binary or the unit changed,
//! then checks that every enabled service came up and that what it placed is in place.

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

#[derive(clap::Args, Debug)]
pub struct Args {
    /// The checkout to deploy from. Default: $AWM_WORKSPACE, else the first parent of the
    /// working directory that holds awm/install.sh.
    #[arg(long)]
    root: Option<PathBuf>,
    /// The gateway's state directory. Default: $AWM_DIR, else <root>/.awm.
    #[arg(long)]
    state: Option<PathBuf>,
    /// The core binary to place at ~/.local/bin/awm. Default: $AWM_BIN, else the checkout's
    /// static build (core/target/x86_64-unknown-linux-musl/release/awm) when there is one.
    #[arg(long)]
    bin: Option<PathBuf>,
    /// Skip awm/install.sh.
    #[arg(long)]
    no_install: bool,
    /// Skip the page build.
    #[arg(long)]
    no_build: bool,
    /// Restart the `awm` user unit even when neither the binary nor the unit changed.
    #[arg(long)]
    restart: bool,
    /// Print the steps and change nothing.
    #[arg(long)]
    dry_run: bool,
    /// How long to wait for the services to come up, in seconds.
    #[arg(long, default_value_t = 90)]
    timeout: u64,
}

const STATIC_BUILD: &str = "core/target/x86_64-unknown-linux-musl/release/awm";
const UNIT: &str = "core/deploy/awm.service";

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

fn home() -> anyhow::Result<PathBuf> {
    std::env::var_os("HOME").map(PathBuf::from).ok_or_else(|| anyhow::anyhow!("HOME is not set"))
}

fn step(step: &str, cmd: &mut std::process::Command) -> anyhow::Result<()> {
    println!("-> {step}");
    let status = cmd.status().map_err(|e| anyhow::anyhow!("{step}: {e}"))?;
    anyhow::ensure!(status.success(), "{step} failed ({status})");
    Ok(())
}

fn same_bytes(a: &Path, b: &Path) -> bool {
    matches!((std::fs::read(a), std::fs::read(b)), (Ok(x), Ok(y)) if x == y)
}

/// Write `bytes` to `dest` through a sibling temp file, so a running copy is never half written.
fn place(dest: &Path, bytes: &[u8], mode: u32) -> anyhow::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    if let Some(dir) = dest.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let tmp = dest.with_extension("new");
    std::fs::write(&tmp, bytes)?;
    std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(mode))?;
    std::fs::rename(&tmp, dest)?;
    Ok(())
}

/// The unit as this node runs it: the tracked one, with its home-relative paths kept as `%h`.
fn unit_text(root: &Path) -> anyhow::Result<String> {
    Ok(std::fs::read_to_string(root.join(UNIT))?)
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
    let state = args.state.or_else(|| std::env::var_os("AWM_DIR").map(PathBuf::from)).unwrap_or_else(|| root.join(".awm"));
    let home = home()?;
    let bin_dest = home.join(".local/bin/awm");
    let unit_dest = home.join(".config/systemd/user/awm.service");
    let bin_src = args
        .bin
        .or_else(|| std::env::var_os("AWM_BIN").map(PathBuf::from))
        .or_else(|| Some(root.join(STATIC_BUILD)).filter(|p| p.is_file()));
    let config_path = state.join(awm_gateway::config::CONFIG_FILE);

    let bin_changes = bin_src.as_ref().is_some_and(|src| !same_bytes(src, &bin_dest));
    let unit_changes = std::fs::read_to_string(&unit_dest).ok() != Some(unit_text(&root)?);
    let restart = args.restart || bin_changes || unit_changes;

    println!("deploy target: {} (state {})", root.display(), state.display());
    println!("  install: {}", if args.no_install { "no" } else { "awm/install.sh" });
    println!("  build:   {}", if args.no_build { "no" } else { "awm/scripts/build.sh" });
    match &bin_src {
        Some(src) if bin_changes => println!("  binary:  {} -> {}", src.display(), bin_dest.display()),
        Some(_) => println!("  binary:  {} is current", bin_dest.display()),
        None => println!("  binary:  none given and no static build at {STATIC_BUILD}; {} stays", bin_dest.display()),
    }
    println!("  config:  {}", if config_path.is_file() { "kept" } else { "written from this checkout's defaults" });
    println!("  unit:    {}", if unit_changes { "replaced from core/deploy/awm.service" } else { "current" });
    println!("  apply:   {}", if restart { "restart the awm user unit" } else { "awm reload" });
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
    if let Some(src) = bin_src.as_ref().filter(|_| bin_changes) {
        println!("-> place {}", bin_dest.display());
        place(&bin_dest, &std::fs::read(src)?, 0o755)?;
    }
    if !config_path.is_file() {
        println!("-> bootstrap {}", state.display());
        crate::bootstrap::run(crate::bootstrap::Args { state: state.clone(), config: None, awm_root: Some(root.clone()), force: false })?;
    }
    if unit_changes {
        println!("-> place {}", unit_dest.display());
        place(&unit_dest, unit_text(&root)?.as_bytes(), 0o644)?;
        step("daemon-reload", std::process::Command::new("systemctl").args(["--user", "daemon-reload"]))?;
    }
    if restart {
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

    let mut failed = Vec::new();
    let plugins = native("plugins").await?;
    for r in plugins["refused"].as_array().into_iter().flatten() {
        failed.push(format!("refused plugin {}: {}", r["plugin"].as_str().unwrap_or("?"), r["reason"].as_str().unwrap_or("?")));
    }
    if let Some(src) = &bin_src {
        if !same_bytes(src, &bin_dest) {
            failed.push(format!("{} is not {}", bin_dest.display(), src.display()));
        }
    }
    if std::fs::read_to_string(&unit_dest).ok() != Some(unit_text(&root)?) {
        failed.push(format!("{} differs from {UNIT}", unit_dest.display()));
    }
    if let Err(e) = std::fs::read_to_string(&config_path).map_err(|e| e.to_string()).and_then(|t| {
        toml::from_str::<awm_gateway::config::Config>(&t).map_err(|e| e.to_string())
    }) {
        failed.push(format!("{}: {e}", config_path.display()));
    }
    if !failed.is_empty() {
        eprintln!("deploy checks failed:\n  {}", failed.join("\n  "));
        return Ok(1);
    }
    println!("deployed: every enabled service is ready; binary, config and unit are in place");
    Ok(0)
}
