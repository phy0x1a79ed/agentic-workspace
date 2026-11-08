//! The operations service, the singleton door's locked command, the operator's commands on
//! alcor, the Claude Code hook and the state checks.

use awm_plugin_operations::{door, hook, settings::Settings, snapshot, store::Store};
use awm_statecheck::cli::StateCmd;
use clap::{Parser, Subcommand};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use serde_json::json;

#[derive(Parser)]
#[command(name = "operations", about = "The task queue, the approver and its vault API, request_human, and alcor's singleton door.")]
struct Cli {
    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run the operations service (the default).
    Serve,
    /// The singleton door's locked command: reads one call on stdin (fleet.md R98 item 2).
    Door {
        #[arg(long)]
        node: String,
    },
    /// Print a one-time link that enrols a device (R85).
    EnrolLink {
        #[arg(long, default_value_t = 15)]
        minutes: i64,
    },
    /// List the enrolled devices.
    Devices,
    /// The Claude Code hook: reads one hook payload on stdin, always exits 0.
    Hook,
    /// Parse every stored key and passkey, and open each store read-only. Exits 1 on a fault.
    CheckState {
        /// Copy the live state into this new directory instead, by VACUUM INTO, and stop.
        #[arg(long)]
        copy: Option<PathBuf>,
        /// Run the first start's migrations first. Use it only on a copy.
        #[arg(long)]
        migrate: bool,
    },
    /// Rename the sections back to Python's (history to done, stale to working), for a rollback.
    DowngradeSections,
    /// The pre-redux state check.
    State {
        #[command(subcommand)]
        cmd: StateCmd,
    },
}

/// Copies the live stores, keys and settings into `dir`, laid out as a `HOME` and an
/// `AWM_APPROVER_DIR`, without touching the live files. Each database goes by `VACUUM INTO`.
fn copy_state(s: &Settings, dir: &Path) -> anyhow::Result<()> {
    anyhow::ensure!(!dir.exists(), "{} exists: give a new path", dir.display());
    let ops = dir.join(".awm/operations");
    let ap = dir.join("approver");
    std::fs::create_dir_all(&ops)?;
    std::fs::create_dir_all(ap.join("keys"))?;
    for d in [dir, &ops, &ap, &ap.join("keys")] {
        std::fs::set_permissions(d, std::fs::Permissions::from_mode(0o700))?;
    }
    for (src, dst) in [(&s.queue_db, ops.join("queue.db")), (&s.relay_db, ops.join("relay.db")), (&s.approver_dir.join("approver.db"), ap.join("approver.db"))] {
        if !src.exists() {
            println!("absent: {}", src.display());
            continue;
        }
        let c = rusqlite::Connection::open_with_flags(src, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        c.execute("VACUUM INTO ?1", [dst.to_string_lossy()])?;
        println!("copied {}", src.display());
    }
    for e in std::fs::read_dir(s.approver_dir.join("keys"))? {
        let e = e?;
        if e.file_type()?.is_file() {
            std::fs::copy(e.path(), ap.join("keys").join(e.file_name()))?;
        }
    }
    let cfg = s.approver_dir.join("config.json");
    if cfg.exists() {
        std::fs::copy(&cfg, ap.join("config.json"))?;
    }
    println!("next: HOME={} AWM_APPROVER_DIR={} operations state snapshot, then check-state --migrate, then state check", dir.display(), ap.display());
    Ok(())
}

struct NoTools;
impl awm_plugin_operations::approver::Tools for NoTools {
    fn twofa<'a>(&'a self, _verb: &'a str, _args: serde_json::Value) -> awm_plugin_operations::relay::async_trait_free::BoxFut<'a, Result<serde_json::Value, String>> {
        Box::pin(async { Err("no tools in check-state".to_string()) })
    }
}

/// Runs the first start's migrations on the stores the environment names. Use it on a copy.
fn migrate(s: &Settings) -> anyhow::Result<()> {
    use awm_plugin_operations::approver::{Approver, Config};
    let store = std::sync::Arc::new(Store::open(&s.queue_db, false)?);
    println!("queue.db migrated: mark {}", store.max_event_id()?);
    let keys = awm_plugin_operations::keys::Keys::load(&s.approver_dir.join("keys"))?;
    let cfg = Config { origin: s.origin.clone(), allowed_orders: s.allowed_orders.clone(), rate_max: s.rate_max, portals: s.portals.clone() };
    Approver::open(&s.approver_dir.join("approver.db"), keys, cfg, store, std::sync::Arc::new(NoTools), false, std::sync::Arc::new(awm_plugin_operations::now))?;
    println!("approver.db migrated");
    Ok(())
}

fn check_state(copy: Option<PathBuf>, migrate_first: bool) -> anyhow::Result<i32> {
    let s = Settings::from_env()?;
    if let Some(dir) = copy {
        copy_state(&s, &dir)?;
        return Ok(0);
    }
    if migrate_first {
        migrate(&s)?;
    }
    let mut faults = awm_plugin_operations::keys::check_state(&s.approver_dir.join("keys"));
    if faults.is_empty() {
        println!("keys: ok");
    }
    let db = s.approver_dir.join("approver.db");
    match rusqlite::Connection::open_with_flags(&db, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY) {
        Ok(c) => {
            let mut st = c.prepare("SELECT cred_id, public_key FROM credentials WHERE revoked_at IS NULL")?;
            let rows: Vec<(String, String)> = st.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<rusqlite::Result<_>>()?;
            for (id, k) in &rows {
                if let Err(e) = awm_plugin_operations::passkeys::parse_cose(k) {
                    faults.push(format!("credential {}…: {e}", id.chars().take(8).collect::<String>()));
                }
            }
            println!("passkeys: {} active, parsed", rows.len());
        }
        Err(e) => faults.push(format!("{}: {e}", db.display())),
    }
    for p in [&s.queue_db, &s.relay_db] {
        match rusqlite::Connection::open_with_flags(p, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY) {
            Ok(c) => {
                let n: i64 = c.query_row("SELECT COUNT(*) FROM sqlite_master", [], |r| r.get(0)).unwrap_or(-1);
                println!("{}: {n} schema objects", p.display());
            }
            Err(e) => faults.push(format!("{}: {e}", p.display())),
        }
    }
    for f in &faults {
        eprintln!("fault: {f}");
    }
    Ok(i32::from(!faults.is_empty()))
}

fn control(req: serde_json::Value) -> i32 {
    match door::control(&req) {
        Ok(out) => {
            if let Some(e) = out.get("error").and_then(|e| e.as_str()) {
                eprintln!("{e}");
                return 1;
            }
            match out.get("url").and_then(|u| u.as_str()) {
                Some(u) => println!("{u}"),
                None => println!("{}", serde_json::to_string_pretty(&out).unwrap_or_default()),
            }
            0
        }
        Err(e) => {
            eprintln!("the operations door socket: {e}");
            1
        }
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let cmd = Cli::parse().cmd;
    if matches!(cmd, Some(Cmd::Hook)) {
        std::process::exit(hook::run());
    }
    awm_plugin_sdk::telemetry::init();
    let awm_dir = awm_plugin_sdk::config::awm_dir();
    match cmd {
        Some(Cmd::Door { node }) => std::process::exit(door::forced_command(&node, None)),
        Some(Cmd::EnrolLink { minutes }) => std::process::exit(control(json!({"cmd": "enrol_link", "minutes": minutes}))),
        Some(Cmd::Devices) => std::process::exit(control(json!({"cmd": "devices"}))),
        Some(Cmd::CheckState { copy, migrate }) => std::process::exit(check_state(copy, migrate)?),
        Some(Cmd::DowngradeSections) => {
            let s = Settings::from_env()?;
            println!("{}", Store::open(&s.queue_db, false)?.downgrade_sections()?);
            Ok(())
        }
        Some(Cmd::State { cmd }) => std::process::exit(awm_statecheck::cli::run(&cmd, "operations", &snapshot::decl(&awm_dir)?)?),
        Some(Cmd::Hook) => unreachable!(),
        Some(Cmd::Serve) | None => awm_plugin_operations::service::serve().await,
    }
}
