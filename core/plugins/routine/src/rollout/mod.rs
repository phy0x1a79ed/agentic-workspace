//! Node rollout: make each node of the fleet run a release commit.
//!
//! The `ship` skill moves `release` and then runs this. A failed node does not stop the others:
//! the run reports every failure and exits non-zero. Rerunning is the recovery, because
//! `node-refresh.sh` skips a node that already took the commit and retries one that failed.

mod artifacts;
mod deployconf;
pub mod fleetconf;
mod sh;

use std::collections::HashMap;
use std::fs::File;
use std::io::Write;
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use deployconf::DeployConfig;
use serde_json::Value;
use sh::Ran;

use crate::runner::git;
use crate::timers::quote as q;

/// The checked pins of the release, or why they could not be, once per run.
type Pins = Result<Vec<artifacts::Artifact>, String>;

/// The last line scripts/node-refresh.sh prints.
pub const RESULT_MARK: &str = "AWM-REFRESH-RESULT ";
const PROJECT: &str = "awm";
const PUSH_BUDGET: f64 = 300.0;
const REFRESH_BUDGET: f64 = 1800.0;

fn parse_result(output: &str) -> Option<Value> {
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

/// The restart list as `--restart` gives it: plugin names, `gateway`, or `none`, which a
/// union of trailers may carry beside real names.
pub fn restart_list(text: &str) -> Result<String, String> {
    let names: Vec<&str> = text.split(',').map(str::trim).filter(|n| !n.is_empty()).collect();
    if names.is_empty() {
        return Err("--restart names the plugins to restart, `gateway`, or `none`".into());
    }
    Ok(names.into_iter().filter(|n| *n != "none").collect::<Vec<_>>().join(","))
}

fn answers(node: &str) -> bool {
    // A sleeper that does not answer stays asleep: its ssh wake hook reads AWM_EDGE_WAKE.
    sh::bash(&format!("AWM_EDGE_WAKE=0 ssh -o BatchMode=yes -o ConnectTimeout=8 {} true", q(node)), Path::new("/"), Duration::from_secs(20), &HashMap::new()).ok()
}

/// Run `cmd`, its output appended to `log`. Only a timeout is an error here.
fn run(cmd: &str, cwd: &Path, label: &str, log: &Path, timeout: f64, env: &HashMap<String, String>) -> Result<Ran, String> {
    let t0 = Instant::now();
    let mut f = std::fs::OpenOptions::new().create(true).append(true).open(log).map_err(|e| format!("{}: {e}", log.display()))?;
    let _ = writeln!(f, "$ {cmd}");
    let _ = f.flush();
    let r = sh::bash(cmd, cwd, Duration::from_secs_f64(timeout), env);
    let _ = f.write_all(r.stdout.as_bytes());
    if r.timed_out {
        return Err(format!("{label} ran past its {timeout}s budget. Log: {}", log.display()));
    }
    say(&format!("  {label}: exit {} in {:.0}s (log {})", r.code, t0.elapsed().as_secs_f64(), log.display()));
    Ok(r)
}

#[derive(Debug, PartialEq)]
pub enum Outcome {
    Done,
    Asleep,
    Failed(String),
}

pub struct Rollout {
    /// This node's checkout of the release.
    pub checkout: PathBuf,
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
        let mut nodes = self.fleet.rollout(PROJECT);
        if nodes.is_empty() {
            nodes = vec![self.node.clone()];
        }
        let store = self.awm_dir.join("artifacts");
        let pins = if nodes.iter().any(|n| !self.fleet.artifacts_dest(n).is_empty()) {
            artifacts::pinned(&self.bare, &store, to, &cfg.artifact_pins)
        } else {
            Ok(vec![])
        };
        let mut out = vec![];
        for node in nodes {
            say(&format!("== {node} {}", crate::now()));
            let outcome = self.refresh_node(&node, to, &cfg, &pins, &logs.join(format!("{node}.log"))).unwrap_or_else(Outcome::Failed);
            out.push((node, outcome));
        }
        Ok(out)
    }

    /// A held `flock`, which the kernel releases when the process ends, however it ends.
    fn lock(&self, dir: &Path) -> Result<File, String> {
        let f = File::create(dir.join("lock")).map_err(|e| e.to_string())?;
        // SAFETY: flock on a descriptor we own.
        if unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err("another rollout is running on this node".into());
        }
        Ok(f)
    }

    fn config(&self, to: &str) -> Result<DeployConfig, String> {
        match artifacts::show(&self.bare, &format!("{to}:{}", deployconf::FILE)) {
            Some(text) => deployconf::parse(&text),
            None => Ok(DeployConfig::default()),
        }
    }

    fn refresh_node(&self, node: &str, to: &str, cfg: &DeployConfig, pins: &Pins, log: &Path) -> Result<Outcome, String> {
        if node != self.node {
            if self.fleet.sleepers.iter().any(|s| s == node) && !answers(node) {
                return Ok(Outcome::Asleep);
            }
            self.push(node, to, pins, log)?;
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
    fn push(&self, node: &str, to: &str, pins: &Pins, log: &Path) -> Result<(), String> {
        let url = self.fleet.push_url(node, PROJECT);
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
        for art in pins.as_ref().map_err(String::clone)? {
            let res = artifacts::push(&store, art, &dest);
            if !res.ok() {
                return Err(format!("push of {} to {dest} failed\n{}", art.file, tail(&res.stdout, 20)));
            }
        }
        Ok(())
    }

    /// The node's refresh: its own command from the install config when it names one, else an
    /// ssh that moves its checkout and runs the project's refresh.
    fn refresh_command(&self, node: &str, cfg: &DeployConfig, to: &str) -> String {
        let own = self.fleet.refresh_command(node);
        if node != self.node && !own.is_empty() {
            return own;
        }
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
            return format!("cd {} && {body}", q(&self.checkout.to_string_lossy()));
        }
        format!("ssh -o BatchMode=yes {} {}", q(node), q(&format!("cd agentic_workspace && {body}")))
    }

    fn refresh_env(&self, node: &str, to: &str) -> HashMap<String, String> {
        [("AWM_REFRESH_NODE", node), ("AWM_REFRESH_TO", to), ("AWM_NODE_NAME", node), ("AWM_REFRESH_RESTART", self.restart.as_str())]
            .into_iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }
}

/// Print one line per node, then each failure's detail. Returns whether every node was fine.
fn report(outcomes: &[(String, Outcome)], to: &str) -> bool {
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
    let to = git(&bare, &["rev-parse", "--verify", "-q", &format!("{}^{{commit}}", a.sha)]).ok_or_else(|| format!("no commit {} in {}", a.sha, bare.display()))?;
    if git(&bare, &["merge-base", "--is-ancestor", &to, "refs/heads/release"]).is_none() {
        return Err(format!("{} is not on release", short(&to)));
    }
    let install = std::env::var_os("AWM_INSTALL_CONFIG").filter(|c| !c.is_empty()).map(PathBuf::from).unwrap_or_else(|| awm_dir.join(fleetconf::FILE));
    let fleet = fleetconf::load(&install)?;
    let rollout = Rollout { restart: restart_list(&a.restart)?, checkout: workspace, bare, awm_dir, node: awm_plugin_sdk::config::node_name(), fleet };
    Ok(report(&rollout.run(&to)?, &to))
}
