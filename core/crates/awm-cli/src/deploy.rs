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
    /// Plugins the reload restarts, comma separated, past a tripped crash breaker. Runs after the
    /// unit restart when both apply.
    #[arg(long, value_delimiter = ',')]
    reload_restart: Vec<String>,
    /// Print the steps and change nothing.
    #[arg(long)]
    dry_run: bool,
    /// How long to wait for the services to come up, in seconds. kb's first start gets an hour.
    #[arg(long, default_value_t = 90)]
    timeout: u64,
}

/// kb's first start copies every post into LanceDB, which took up to 11 minutes on a small node.
const KB_FIRST_START_S: u64 = 3600;

/// True until kb has written the marker its first start ends with.
fn kb_first_start_pending(state: &Path) -> bool {
    !state.join("services/kb/lance/journals/cutover.json").exists()
}

const STATIC_BUILD: &str = "x86_64-unknown-linux-musl/release/awm";

/// The static build `scripts/rust-build.sh` leaves for this tree: under `AWM_CARGO_TARGET_DIR`,
/// else `~/.cache/awm-target/<tree folder>`.
fn static_build(root: &Path, home: &Path) -> PathBuf {
    let target = std::env::var_os("AWM_CARGO_TARGET_DIR").map(PathBuf::from).unwrap_or_else(|| {
        home.join(".cache/awm-target").join(root.file_name().unwrap_or_default())
    });
    target.join(STATIC_BUILD)
}
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

#[derive(Debug, PartialEq, Eq)]
enum Apply {
    RestartUnit,
    WaitForGateway,
    Reload(Vec<String>),
}

/// The apply steps in order. A unit restart comes first and the named plugins restart after
/// the new gateway answers, because a restart before it would be lost with the old gateway.
fn plan(restart_unit: bool, names: &[String]) -> Vec<Apply> {
    match (restart_unit, names.is_empty()) {
        (true, true) => vec![Apply::RestartUnit],
        (true, false) => vec![Apply::RestartUnit, Apply::WaitForGateway, Apply::Reload(names.to_vec())],
        (false, _) => vec![Apply::Reload(names.to_vec())],
    }
}

/// The reload a deploy asks for. A plugin the deploy names restarts with force, since a stop
/// keeps its crash breaker and a tripped plugin would otherwise stay down through the deploy.
fn reload_args(names: &[String]) -> Value {
    json!({"restart": names, "force": !names.is_empty()})
}

async fn native(verb: &str) -> anyhow::Result<Value> {
    native_with(verb, json!({})).await
}

async fn native_with(verb: &str, args: Value) -> anyhow::Result<Value> {
    let url = format!("{}/invoke", crate::gateway_url());
    let resp = reqwest::Client::new()
        .post(&url)
        .header("X-Awm-Surface", "cli")
        .header("X-Awm-Caller-Pid", std::process::id().to_string())
        .json(&json!({"name": awm_gateway::gateway::NATIVE_TOOL, "args": {"verb": verb, "args": args}}))
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
        .or_else(|| Some(static_build(&root, &home)).filter(|p| p.is_file()));
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
        None => println!("  binary:  none given and no static build at {}; {} stays", static_build(&root, &home).display(), bin_dest.display()),
    }
    println!("  config:  {}", if config_path.is_file() { "kept" } else { "written from this checkout's defaults" });
    println!("  unit:    {}", if unit_changes { "replaced from core/deploy/awm.service" } else { "current" });
    println!("  apply:   {}", if restart { if args.reload_restart.is_empty() { "restart the awm user unit".to_string() } else { format!("restart the awm user unit, then awm reload --restart {} --force", args.reload_restart.join(",")) } } else if args.reload_restart.is_empty() { "awm reload".to_string() } else { format!("awm reload --restart {} --force", args.reload_restart.join(",")) });
    if args.dry_run {
        println!("--- dry run: nothing changed ---");
        return Ok(0);
    }

    if !args.no_install {
        step(
            "install",
            std::process::Command::new("bash")
                .arg(root.join("awm/install.sh"))
                .current_dir(&root)
                .env("AWM_DIR", &state)
                .env("AWM_CORE", std::env::current_exe()?),
        )?;
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
    for apply in plan(restart, &args.reload_restart) {
        match apply {
            Apply::RestartUnit => step("restart", std::process::Command::new("systemctl").args(["--user", "restart", "awm"]))?,
            Apply::WaitForGateway => {
                println!("-> wait for the gateway");
                let deadline = Instant::now() + Duration::from_secs(args.timeout);
                while native("status").await.is_err() {
                    anyhow::ensure!(Instant::now() < deadline, "the gateway did not answer within {}s", args.timeout);
                    tokio::time::sleep(Duration::from_secs(1)).await;
                }
            }
            Apply::Reload(names) => {
                let report = native_with("reload", reload_args(&names)).await?;
                println!("-> reload: {}", serde_json::to_string(&report)?);
            }
        }
    }

    let budget = if kb_first_start_pending(&state) { KB_FIRST_START_S } else { args.timeout };
    let deadline = Instant::now() + Duration::from_secs(budget);
    loop {
        let rows = native("services").await;
        let rows = rows.as_ref().ok().and_then(Value::as_array).map(Vec::as_slice).unwrap_or_default();
        let is_down = |r: &Value| r["enabled"] == json!(true) && r["external"] != json!(true) && r["status"] != json!("ready");
        let name = |r: &Value| format!("{} ({})", r["name"].as_str().unwrap_or("?"), r["status"].as_str().unwrap_or("?"));
        let gave_up: Vec<String> = rows.iter().filter(|r| is_down(r) && (r["failed"] == json!(true) || r["tripped"] == json!(true))).map(name).collect();
        if !gave_up.is_empty() {
            eprintln!("gave up starting: {}", gave_up.join(", "));
            return Ok(1);
        }
        let mut waiting: Vec<String> = rows.iter().filter(|r| is_down(r)).map(name).collect();
        if rows.is_empty() {
            waiting.push("the gateway".to_string());
        }
        if waiting.is_empty() {
            break;
        }
        if Instant::now() > deadline {
            eprintln!("not up after {budget}s: {}", waiting.join(", "));
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_unit_restart_comes_before_the_named_plugin_restart() {
        let names = vec!["channel".to_string()];
        assert_eq!(plan(true, &names), vec![Apply::RestartUnit, Apply::WaitForGateway, Apply::Reload(names.clone())]);
        assert_eq!(plan(true, &[]), vec![Apply::RestartUnit]);
        assert_eq!(plan(false, &names), vec![Apply::Reload(names)]);
        assert_eq!(plan(false, &[]), vec![Apply::Reload(vec![])]);
        assert_eq!(reload_args(&["a".to_string()]), json!({"restart": ["a"], "force": true}));
        assert_eq!(reload_args(&[]), json!({"restart": [], "force": false}), "a plain reload forces nothing");
        if std::env::var_os("AWM_CARGO_TARGET_DIR").is_none() {
            assert_eq!(static_build(Path::new("/w/plugin-redux"), Path::new("/h")),
                Path::new("/h/.cache/awm-target/plugin-redux/x86_64-unknown-linux-musl/release/awm"));
        }
    }
}
