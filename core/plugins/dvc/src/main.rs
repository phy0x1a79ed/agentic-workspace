//! The dvc service, its backup job runner, the data layer's git hooks and merge driver, the
//! Globus login and the state check.

use std::io::BufRead;
use std::path::PathBuf;
use std::sync::Arc;

use awm_dvcdata::DataCmd;
use awm_plugin_dvc::globus::{self, Native};
use awm_plugin_dvc::jobs::{self, Live};
use awm_plugin_dvc::runs::RunsDao;
use awm_plugin_dvc::service::{self, Dvc};
use awm_plugin_dvc::watcher::{Timing, Watcher};
use awm_plugin_dvc::{clock, config, snapshot, Paths};
use awm_plugin_sdk::Service;
use awm_statecheck::cli::StateCmd;
use clap::{Parser, Subcommand};
use serde_json::Value;

#[derive(Parser)]
#[command(name = "dvc", about = "The chinook remote for the shared DVC cache, its backup jobs, and the DVC data layer.")]
struct Cli {
    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run the dvc service (the default).
    Serve,
    /// Run one backup job and wait for its verdict, with no gateway. routine's timer runs this;
    /// it is also the escape hatch by hand. Exits 0 on success or when declined as in flight.
    RunJob {
        /// cache_sync or workspace_backup.
        #[arg(long, default_value = jobs::CACHE_SYNC)]
        job: String,
        /// Seconds to wait for a terminal state. A run past it stays live for the service.
        #[arg(long, default_value_t = 86400)]
        timeout: u64,
        /// Submit even if a previous run is still in flight.
        #[arg(long)]
        force: bool,
        /// Build the transfer document, print it, submit nothing.
        #[arg(long)]
        dry_run: bool,
        /// What started the run, as its row records it.
        #[arg(long, default_value = "manual")]
        trigger: String,
    },
    /// Print a Globus task's status as the `task` verb does, with no gateway.
    Task {
        task_id: String,
        /// Seconds to wait for a terminal state.
        #[arg(long, default_value_t = 0)]
        wait: u64,
    },
    /// Log in to Globus for the transfers: prints a URL, then reads the code it shows.
    GlobusLogin {
        /// Import the globus CLI's login again instead, replacing the token file.
        #[arg(long)]
        import: bool,
    },
    /// The pre-redux state check.
    State {
        #[command(subcommand)]
        cmd: StateCmd,
    },
    #[command(flatten)]
    Data(DataCmd),
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    let paths = Paths::from_env();
    match Cli::parse().cmd {
        Some(Cmd::State { cmd }) => std::process::exit(awm_statecheck::cli::run(&cmd, "dvc", &snapshot::decl(&paths.awm_dir)?)?),
        Some(Cmd::Data(cmd)) => std::process::exit(tokio::task::spawn_blocking(move || awm_dvcdata::cli::run(cmd)).await?),
        Some(Cmd::Task { task_id, wait }) => std::process::exit(tokio::task::spawn_blocking(move || task(&paths, &task_id, wait)).await?),
        Some(Cmd::GlobusLogin { import }) => std::process::exit(tokio::task::spawn_blocking(move || globus_login(&paths, import)).await?),
        Some(Cmd::RunJob { job, timeout, force, dry_run, trigger }) => {
            std::process::exit(tokio::task::spawn_blocking(move || run_job(&paths, &job, timeout, force, dry_run, &trigger)).await?)
        }
        Some(Cmd::Serve) | None => serve(paths).await,
    }
}

fn task(paths: &Paths, id: &str, wait: u64) -> i32 {
    let api = Native::new(&paths.state_dir());
    let res = if wait > 0 { globus::wait(&api, id, wait, 15) } else { globus::task_status(&api, id) };
    match res {
        Ok(v) => {
            println!("{}", serde_json::to_string_pretty(&v).unwrap_or_default());
            0
        }
        Err(e) => {
            eprintln!("{e}");
            1
        }
    }
}

fn globus_login(paths: &Paths, import: bool) -> i32 {
    let store = globus::TokenStore::new(&paths.state_dir());
    if import {
        return match globus::import_from_cli(&store.cli_storage).and_then(|t| store.write(&t)) {
            Ok(()) => {
                println!("imported the globus CLI's login into {}", store.path.display());
                0
            }
            Err(e) => {
                eprintln!("{e}");
                1
            }
        };
    }
    let (verifier, challenge) = globus::pkce();
    println!("Open this URL, sign in, and paste the code it shows:\n\n{}\n", globus::authorize_url(&store.auth_url, globus::NATIVE_CLIENT_ID, &challenge));
    let mut code = String::new();
    if std::io::stdin().lock().read_line(&mut code).is_err() || code.trim().is_empty() {
        eprintln!("no code given");
        return 1;
    }
    match store.exchange_code(globus::NATIVE_CLIENT_ID, code.trim(), &verifier) {
        Ok(_) => {
            println!("logged in; the token is in {}", store.path.display());
            0
        }
        Err(e) => {
            eprintln!("{e}");
            1
        }
    }
}

fn run_job(paths: &Paths, job: &str, timeout: u64, force: bool, dry_run: bool, trigger: &str) -> i32 {
    if let Err(e) = config::load(&paths.awm_dir) {
        tracing::error!("{e}");
        return 1;
    }
    if let Err(e) = snapshot::take(&paths.awm_dir) {
        tracing::error!("the pre-redux state snapshot failed, so run-job refuses to run: {e:#}");
        return 1;
    }
    let dao = match RunsDao::open(&paths.db()) {
        Ok(d) => d,
        Err(e) => {
            tracing::error!("{}: {e}", paths.db().display());
            return 1;
        }
    };
    let api = Native::new(&paths.state_dir());
    if trigger == "schedule" && !dry_run {
        // Stamped before the submit, so the slot routine is told of on the next schedule
        // sync matches the one this fire served.
        let _ = dao.set_last_fire(job, awm_plugin_dvc::now());
    }
    let live = Live { paths, api: &api };
    let result = match jobs::run_job(paths, &dao, &live, job, trigger, force, dry_run) {
        Ok(r) => r,
        Err(e) => {
            tracing::error!("{job} failed to submit: {e}");
            return 1;
        }
    };
    if dry_run {
        println!("{}", serde_json::to_string_pretty(&result).unwrap_or_default());
        return 0;
    }
    // Declining to stack a second scan is the guard working, not a failure.
    if result.get("submitted").and_then(Value::as_bool) == Some(false) {
        tracing::warn!("{}", result.get("note").and_then(serde_json::Value::as_str).unwrap_or("not submitted"));
        return 0;
    }
    let task = result["task_id"].as_str().unwrap_or("").to_string();
    let run_id = result["run_id"].as_str().unwrap_or("").to_string();
    tracing::info!("submitted {task} (-> {}) — waiting up to {timeout}s", result["destination"].as_str().unwrap_or(""));
    let status = match globus::wait(&api, &task, timeout, 15) {
        Ok(s) => s,
        Err(e) => {
            tracing::error!("task {task}: {e} — left live for the service to adopt");
            return 1;
        }
    };
    // Recorded before anything else: this process may be about to be killed.
    if let Err(e) = jobs::record_status(&dao, &run_id, &status) {
        tracing::error!("could not record task {task}: {e}");
    }
    let skipped = globus::skipped_errors(&api, &task).unwrap_or_else(|e| {
        tracing::warn!("could not read skipped errors: {e}");
        vec![]
    });
    if let Some(first) = skipped.first() {
        let path = first.get("source_path").and_then(Value::as_str).unwrap_or("");
        let _ = dao.set_note(&run_id, &format!("{} source path(s) skipped; first: {path}", skipped.len()));
        tracing::warn!("{} source path(s) skipped, first {path} ({})", skipped.len(), first.get("error_code").and_then(serde_json::Value::as_str).unwrap_or("None"));
    }
    tracing::info!(
        "task {task}: status={} files={}/{} bytes={} faults={}",
        status["status"], status["files_transferred"], status["files"], status["bytes_transferred"], status["faults"]
    );
    if status["status"] == "SUCCEEDED" {
        if !skipped.is_empty() && status["files_transferred"].as_i64().unwrap_or(0) == 0 {
            tracing::error!("task {task} transferred nothing and skipped {} path(s)", skipped.len());
            return 1;
        }
        return 0;
    }
    if status["timed_out"] == true {
        tracing::error!("task {task} still running after {timeout}s — left live for the service to adopt");
    } else {
        tracing::error!("task {task} ended {}: {}", status["status"], status["nice_status"]);
    }
    1
}

async fn serve(paths: Paths) -> anyhow::Result<()> {
    let svc = Service::from_env()?;
    let ctx = svc.context();
    snapshot::take(&paths.awm_dir)?;
    let dao = Arc::new(RunsDao::open(&paths.db())?);
    jobs::ensure_seeded(&paths, &dao)?;
    let api = Arc::new(Native::new(&paths.state_dir()));
    if let Err(e) = api.tokens.ensure_imported() {
        tracing::warn!("dvc: no Globus token yet, so the byte-moving verbs will fail until `dvc globus-login`: {e}");
    }
    let emit_ctx = ctx.clone();
    let emit: awm_plugin_dvc::watcher::Emit = Arc::new(move |p: Value| {
        emit_ctx.emit(awm_plugin_dvc::watcher::TOPIC, p);
    });
    let watcher = Watcher::new(dao.clone(), api.clone(), emit, Timing::default());
    let plugin_dir = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let dvc = Arc::new(Dvc {
        paths: paths.clone(),
        dao: dao.clone(),
        api,
        watcher: Some(watcher.clone()),
        hub: Some(ctx.hub().clone()),
        plugin_dir: plugin_dir.clone(),
        hook_bin: paths.awm_dir.join("bin").join("dvc"),
    });
    let hub = ctx.hub().clone();
    let ready = async move {
        tokio::spawn(watcher.run());
        tokio::spawn(async move {
            // routine may start after dvc. Wait for it, then hand it the two timers.
            let mut idle = std::time::Duration::from_secs(5);
            loop {
                if let Ok(Some(_)) = hub.owner("routine").await {
                    break;
                }
                tokio::time::sleep(idle).await;
                idle = (idle * 2).min(std::time::Duration::from_secs(300));
            }
            let rows = match dao.list_jobs() {
                Ok(r) => r,
                Err(e) => return tracing::warn!("dvc: could not read dvc_jobs: {e}"),
            };
            let out = clock::sync_all(&hub, &plugin_dir, &rows).await;
            tracing::info!("dvc: routine clock: {out}");
        });
    };
    service::mount(svc, dvc).on_ready(ready).run().await
}
