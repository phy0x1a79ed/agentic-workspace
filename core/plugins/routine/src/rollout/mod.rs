//! Node rollout: make each node of the fleet run a release commit.
//!
//! The `ship` skill moves `release` and then runs this. A failed node does not stop the others:
//! the run reports every failure and exits non-zero. Rerunning is the recovery, because
//! `node-refresh.sh` skips a node that already took the commit and retries one that failed.

pub mod artifacts;
pub mod deployconf;
pub mod fleetconf;
pub mod sh;

use std::collections::HashMap;
use std::fs::File;
use std::io::Write;
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use deployconf::DeployConfig;
use serde_json::Value;
use sh::Ran;

use crate::timers::quote as q;

/// The last line scripts/node-refresh.sh prints.
pub const RESULT_MARK: &str = "AWM-REFRESH-RESULT ";
const PROJECT: &str = "awm";
const PUSH_BUDGET: f64 = 300.0;
const REFRESH_BUDGET: f64 = 1800.0;

pub fn parse_result(output: &str) -> Option<Value> {
    let line = output.lines().rev().find_map(|l| l.strip_prefix(RESULT_MARK))?;
    let res: Value = serde_json::from_str(line).ok()?;
    res.get("ok").is_some().then_some(res)
}

fn say(msg: &str) {
    let mut out = std::io::stdout().lock();
    let _ = writeln!(out, "{msg}");
    let _ = out.flush();
}

fn tail(text: &str, n: usize) -> String {
    let lines: Vec<&str> = text.trim_end().lines().collect();
    lines[lines.len().saturating_sub(n)..].join("\n")
}

fn short(sha: &str) -> &str {
    &sha[..sha.len().min(12)]
}

/// The restart list as `--restart` gives it: plugin names, `gateway`, or `none`.
pub fn restart_list(text: &str) -> Result<String, String> {
    let names: Vec<&str> = text.split(',').map(str::trim).filter(|n| !n.is_empty()).collect();
    match names.as_slice() {
        [] => Err("--restart names the plugins to restart, `gateway`, or `none`".into()),
        ["none"] => Ok(String::new()),
        _ => Ok(names.join(",")),
    }
}

fn answers(node: &str) -> bool {
    sh::bash(&format!("ssh -o BatchMode=yes -o ConnectTimeout=8 {} true", q(node)), Path::new("/"), Duration::from_secs(20), &HashMap::new(), Duration::from_secs(1)).ok()
}

/// Run `cmd`, its output appended to `log`. Only a timeout is an error here.
fn run(cmd: &str, cwd: &Path, label: &str, log: &Path, timeout: f64, env: &HashMap<String, String>) -> Result<Ran, String> {
    let t0 = Instant::now();
    let mut f = std::fs::OpenOptions::new().create(true).append(true).open(log).map_err(|e| format!("{}: {e}", log.display()))?;
    let _ = writeln!(f, "$ {cmd}");
    let _ = f.flush();
    let r = sh::bash(cmd, cwd, Duration::from_secs_f64(timeout), env, Duration::from_secs(5));
    let _ = f.write_all(r.stdout.as_bytes());
    if r.timed_out {
        return Err(format!("{label} ran past its {timeout}s budget. Log: {}", log.display()));
    }
    say(&format!("  {label}: exit {} in {:.0}s (log {})", r.code, t0.elapsed().as_secs_f64(), log.display()));
    Ok(r)
}

/// A held `flock`. The kernel releases it when the process ends, however it ends.
struct Flock(#[allow(dead_code)] File);

#[derive(Debug, PartialEq)]
pub enum Outcome {
    Done,
    Asleep,
    Failed(String),
}

pub struct Rollout {
    pub project: String,
    pub workspace: PathBuf,
    pub bare: PathBuf,
    pub awm_dir: PathBuf,
    pub node: String,
    pub fleet: fleetconf::Fleet,
    pub restart: String,
}

impl Rollout {
    /// Refresh each node onto `to`, in fleet order, and return what happened on each.
    pub fn run(&self, to: &str) -> Result<Vec<(String, Outcome)>, String> {
        let dir = self.awm_dir.join("rollout");
        let logs = dir.join("logs").join(short(to));
        std::fs::create_dir_all(&logs).map_err(|e| format!("{}: {e}", logs.display()))?;
        let _held = self.lock(&dir)?;
        let cfg = self.config(to)?;
        let mut nodes = self.fleet.rollout(&self.project);
        if nodes.is_empty() {
            nodes = vec![self.node.clone()];
        }
        let mut out = vec![];
        for node in nodes {
            say(&format!("== {node} {}", crate::now()));
            let outcome = self.refresh_node(&node, to, &cfg, &logs.join(format!("{node}.log"))).unwrap_or_else(Outcome::Failed);
            out.push((node, outcome));
        }
        Ok(out)
    }

    fn lock(&self, dir: &Path) -> Result<Flock, String> {
        let f = File::create(dir.join("lock")).map_err(|e| e.to_string())?;
        // SAFETY: flock on a descriptor we own.
        if unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err("another rollout is running on this node".into());
        }
        Ok(Flock(f))
    }

    fn config(&self, to: &str) -> Result<DeployConfig, String> {
        match artifacts::show(&self.bare, &format!("{to}:{}", deployconf::FILE)) {
            Some(text) => deployconf::parse(&text).map_err(|e| e.0),
            None => Ok(DeployConfig::default()),
        }
    }

    fn refresh_node(&self, node: &str, to: &str, cfg: &DeployConfig, log: &Path) -> Result<Outcome, String> {
        if node != self.node {
            if self.fleet.sleepers.iter().any(|s| s == node) && !answers(node) {
                return Ok(Outcome::Asleep);
            }
            self.push(node, to, log)?;
        }
        let out = run(&self.refresh_command(node, cfg, to), Path::new("/"), "refresh", log, REFRESH_BUDGET, &self.refresh_env(node, to))?;
        let result = parse_result(&out.stdout);
        let ok = match &result {
            Some(r) => r["ok"].as_bool().unwrap_or(false),
            None => out.code == 0,
        };
        if ok {
            return Ok(Outcome::Done);
        }
        let why = match &result {
            Some(r) => format!("step {}, node log {}", r["failed_step"], r["log"]),
            None => format!("exit {}", out.code),
        };
        Ok(Outcome::Failed(format!("refresh failed ({why}). Log: {}\n{}", log.display(), tail(&out.stdout, 40))))
    }

    /// Give a remote node the release commit and its pinned artifacts.
    fn push(&self, node: &str, to: &str, log: &Path) -> Result<(), String> {
        let url = self.fleet.push_url(node, &self.project);
        if !url.is_empty() {
            let cmd = format!("git -C {} push --porcelain {} {to}:refs/{}/release", q(&self.bare.to_string_lossy()), q(&url), self.node);
            let r = run(&cmd, &self.bare, "push", log, PUSH_BUDGET, &HashMap::new())?;
            if !r.ok() {
                return Err(format!("push to {url} failed. Log: {}\n{}", log.display(), tail(&r.stdout, 20)));
            }
        }
        let dest = self.fleet.artifacts_dest(node);
        if dest.is_empty() {
            return Ok(());
        }
        let store = self.awm_dir.join("artifacts");
        for art in artifacts::pinned(&self.bare, to)? {
            artifacts::check(&store, &art)?;
            let res = artifacts::push(&store, &art, &dest);
            if !res.ok() {
                return Err(format!("push of {} to {dest} failed\n{}", art.file, tail(&res.stdout, 20)));
            }
        }
        Ok(())
    }

    /// The node's refresh: its own command from the install config when it names one, else an
    /// ssh that moves its checkout and runs the project's refresh.
    pub fn refresh_command(&self, node: &str, cfg: &DeployConfig, to: &str) -> String {
        let mut steps = vec![];
        if !cfg.refresh.sync.is_empty() {
            steps.push(format!("AWM_REFRESH_TO={to} {}", cfg.refresh.sync));
        }
        if !cfg.refresh.command.is_empty() {
            steps.push(format!("AWM_REFRESH_RESTART={} bash {} {to}", q(&self.restart), q(&cfg.refresh.command)));
        }
        if steps.is_empty() {
            return "true".into();
        }
        let body = steps.join(" && ");
        if node == self.node {
            let root = if self.project == PROJECT { self.workspace.clone() } else { self.workspace.join("projects").join(&self.project).join("release") };
            return format!("cd {} && {body}", q(&root.to_string_lossy()));
        }
        let own = self.fleet.refresh_command(node);
        if !own.is_empty() {
            return own;
        }
        let root = if self.project == PROJECT { "agentic_workspace".to_string() } else { format!("agentic_workspace/projects/{}/release", self.project) };
        format!("ssh -o BatchMode=yes {} {}", q(node), q(&format!("cd {root} && {body}")))
    }

    fn refresh_env(&self, node: &str, to: &str) -> HashMap<String, String> {
        [("AWM_REFRESH_NODE", node), ("AWM_REFRESH_PROJECT", self.project.as_str()), ("AWM_REFRESH_TO", to), ("AWM_NODE_NAME", node), ("AWM_REFRESH_RESTART", self.restart.as_str())]
            .into_iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }
}

/// Print one line per node, then each failure's detail. Returns whether every node was fine.
pub fn report(outcomes: &[(String, Outcome)], to: &str) -> bool {
    say(&format!("\nrelease {}:", short(to)));
    for (node, o) in outcomes {
        say(&match o {
            Outcome::Done => format!("  {node}: done"),
            Outcome::Asleep => format!("  {node}: asleep, takes the release at its next refresh"),
            Outcome::Failed(_) => format!("  {node}: FAILED"),
        });
    }
    let mut clean = true;
    for (node, o) in outcomes {
        if let Outcome::Failed(why) = o {
            say(&format!("\n{node}: {why}"));
            clean = false;
        }
    }
    clean
}

/// The `rollout` subcommand, as `scripts/rollout.sh` runs it.
#[derive(Debug, clap::Args)]
pub struct RolloutArgs {
    /// What must restart on each node: plugin names (a,b), gateway, or none.
    #[arg(long)]
    pub restart: String,
    /// The release commit to deploy.
    pub sha: String,
}

/// Returns the exit code: 0, or 1 when a node failed or the rollout refused to start.
pub fn main(args: RolloutArgs) -> i32 {
    match rollout_main(args) {
        Ok(true) => 0,
        Ok(false) => 1,
        Err(e) => {
            eprintln!("{e}");
            1
        }
    }
}

fn rollout_main(a: RolloutArgs) -> Result<bool, String> {
    let workspace = awm_plugin_sdk::config::canonical_workspace();
    let awm_dir = awm_plugin_sdk::config::awm_dir();
    let bare = workspace.join("projects").join(PROJECT).join(".bare");
    let to = sh::git(&bare, &["rev-parse", "--verify", "-q", &format!("{}^{{commit}}", a.sha)], &HashMap::new(), None).stdout.trim().to_string();
    if to.is_empty() {
        return Err(format!("no commit {} in {}", a.sha, bare.display()));
    }
    if sh::git(&bare, &["merge-base", "--is-ancestor", &to, "refs/heads/release"], &HashMap::new(), None).code != 0 {
        return Err(format!("{} is not on release", short(&to)));
    }
    let install = std::env::var_os("AWM_INSTALL_CONFIG").filter(|c| !c.is_empty()).map(PathBuf::from).unwrap_or_else(|| awm_dir.join(fleetconf::FILE));
    let fleet = fleetconf::load(&install).map_err(|e| e.0)?;
    let rollout = Rollout { project: PROJECT.into(), restart: restart_list(&a.restart)?, workspace, bare, awm_dir, node: awm_plugin_sdk::config::node_name(), fleet };
    Ok(report(&rollout.run(&to)?, &to))
}
