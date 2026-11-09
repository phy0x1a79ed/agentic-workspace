//! The land queue: agents bring their own branches live, one batch at a time.
//!
//! `submit` queues a branch with its merge brief. One agent at a time, the merger, holds the
//! project's lease and runs `batch`: merge, pins, check, promote, backup, then the rollout
//! across the fleet. A failed step stops the batch with its state kept, so the merger fixes it
//! and runs `batch` again. Each run renews the lease, and `hold` renews it while the merger
//! works. A lease that runs out passes the batch to the next waiting agent. `wait` gives a
//! submitter its verdict, or names it the merger. `bounce` returns a branch to its owner.
//!
//! The state under `<AWM_DIR>/land/<project>/` is the one `land.py` kept, written as it wrote
//! it, so either reads the other's tickets, batches and lease. The skills `ci/land` and
//! `ci/merge` carry the protocol for agents.

pub mod artifacts;
pub mod deployconf;
pub mod fleetconf;
pub mod json;
pub mod sh;

use std::collections::{BTreeSet, HashMap};
use std::fs::File;
use std::io::Write;
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use deployconf::DeployConfig;
use json::J;
use sh::Ran;

use crate::timers::quote as q;

pub const DEV: &str = "dev";
pub const RELEASE: &str = "release";
const SETTLED: [&str; 2] = ["landed", "bounced"];
pub const LEASE: f64 = 1800.0;
/// The last line scripts/node-refresh.sh prints.
pub const RESULT_MARK: &str = "AWM-REFRESH-RESULT ";
const ID: [(&str, &str); 4] = [
    ("GIT_AUTHOR_NAME", "awm land"),
    ("GIT_AUTHOR_EMAIL", "land@awm.invalid"),
    ("GIT_COMMITTER_NAME", "awm land"),
    ("GIT_COMMITTER_EMAIL", "land@awm.invalid"),
];

/// Why a land verb ended early.
#[derive(Debug, thiserror::Error)]
pub enum LandError {
    /// A step failed. The batch keeps its state for the merger.
    #[error("{0}")]
    Stop(String),
    /// The verb refuses: Python's `SystemExit(message)`, exit 1.
    #[error("{0}")]
    Exit(String),
}

pub type Result<T> = std::result::Result<T, LandError>;

fn exit<T>(msg: impl Into<String>) -> Result<T> {
    Err(LandError::Exit(msg.into()))
}

fn stop<T>(msg: impl Into<String>) -> Result<T> {
    Err(LandError::Stop(msg.into()))
}

pub fn parse_result(output: &str) -> Option<J> {
    for line in output.lines().rev() {
        if let Some(rest) = line.strip_prefix(RESULT_MARK) {
            let res = json::loads(rest).ok()?;
            return (matches!(res, J::Obj(_)) && res.get("ok").is_some()).then_some(res);
        }
    }
    None
}

pub fn now() -> String {
    crate::now()
}

fn epoch() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

pub fn now_plus(seconds: f64) -> String {
    crate::iso(chrono::Utc::now() + chrono::Duration::milliseconds((seconds * 1000.0) as i64))
}

fn say(msg: &str) {
    let mut out = std::io::stdout().lock();
    let _ = writeln!(out, "{msg}");
    let _ = out.flush();
}

fn step(name: &str) {
    say(&format!("== {name} {}", now()));
}

pub fn tail(text: &str, n: usize) -> String {
    let lines: Vec<&str> = text.trim_end().lines().collect();
    lines[lines.len().saturating_sub(n)..].join("\n")
}

fn last_chars(s: &str, n: usize) -> String {
    let count = s.chars().count();
    s.chars().skip(count.saturating_sub(n)).collect()
}

fn short(sha: &str) -> &str {
    &sha[..sha.len().min(12)]
}

fn id_env() -> HashMap<String, String> {
    ID.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect()
}

fn git(cwd: &Path, args: &[&str]) -> Ran {
    sh::git(cwd, args, &HashMap::new(), None)
}

fn git_env(cwd: &Path, args: &[&str], env: &HashMap<String, String>) -> Ran {
    sh::git(cwd, args, env, None)
}

/// `git args` that must pass: a failure stops the batch.
fn git_check(cwd: &Path, args: &[&str], env: &HashMap<String, String>) -> Result<Ran> {
    let r = sh::git(cwd, args, env, None);
    if r.code != 0 {
        let why = if r.stderr.trim().is_empty() { &r.stdout } else { &r.stderr };
        return stop(format!("git {}: {}", args.join(" "), last_chars(why.trim(), 800)));
    }
    Ok(r)
}

pub fn rev(cwd: &Path, reference: &str) -> String {
    let r = git(cwd, &["rev-parse", "--verify", "-q", &format!("{reference}^{{commit}}")]);
    if r.code == 0 {
        r.stdout.trim().to_string()
    } else {
        String::new()
    }
}

pub fn is_ancestor(cwd: &Path, a: &str, b: &str) -> bool {
    git(cwd, &["merge-base", "--is-ancestor", a, b]).code == 0
}

fn read_json(path: &Path) -> Result<J> {
    let text = std::fs::read_to_string(path).map_err(|e| LandError::Exit(format!("{}: {e}", path.display())))?;
    json::loads(&text).map_err(|e| LandError::Exit(format!("{}: {e}", path.display())))
}

/// Write `data` as `json.dumps(data, indent=1)`, through a `.tmp` sibling and a rename.
pub fn write_json(path: &Path, data: &J) -> std::io::Result<()> {
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, json::dumps(data))?;
    std::fs::rename(&tmp, path)
}

fn io(e: std::io::Error) -> LandError {
    LandError::Exit(e.to_string())
}

/// What a submitter says must restart: plugin names, `gateway`, or `none`.
pub fn restart_list(text: &str) -> Result<Vec<String>> {
    let names: Vec<String> = text.split(',').map(str::trim).filter(|n| !n.is_empty()).map(str::to_string).collect();
    if names.is_empty() {
        return exit("--restart names the plugins to restart, `gateway`, or `none`");
    }
    Ok(if names == ["none"] { vec![] } else { names })
}

/// The union of the batch's restart lists. A gateway restart adopts the running plugins, so
/// `gateway` and plugin names both stay in the list.
pub fn batch_restart(tickets: &[J]) -> String {
    let all: BTreeSet<&str> = tickets.iter().flat_map(|t| t.get("restart").map(J::as_arr).unwrap_or(&[])).filter_map(J::as_str).collect();
    all.into_iter().collect::<Vec<_>>().join(",")
}

fn answers(node: &str) -> bool {
    let r = sh::bash(
        &format!("ssh -o BatchMode=yes -o ConnectTimeout=8 {} true", q(node)),
        Path::new("/"),
        Duration::from_secs(20),
        &HashMap::new(),
        Duration::from_secs(1),
    );
    r.ok()
}

/// Run one step's command, its output appended to `log`. A nonzero exit or a timeout stops
/// the batch, unless `any_code`.
fn run(cmd: &str, cwd: &Path, label: &str, log: &Path, timeout: f64, env: &HashMap<String, String>, any_code: bool) -> Result<Ran> {
    let t0 = Instant::now();
    let mut f = std::fs::OpenOptions::new().create(true).append(true).open(log).map_err(io)?;
    let _ = writeln!(f, "$ {cmd}");
    let _ = f.flush();
    let r = sh::bash(cmd, cwd, Duration::from_secs_f64(timeout), env, Duration::from_secs(5));
    let _ = f.write_all(r.stdout.as_bytes());
    if r.timed_out {
        return stop(format!("{label}: ran past its {}s budget. Log: {}\n{}", crate::fmt_g(timeout), log.display(), tail(&r.stdout, 40)));
    }
    say(&format!("  {label}: exit {} in {:.0}s (log {})", r.code, t0.elapsed().as_secs_f64(), log.display()));
    if !any_code && r.code != 0 {
        return stop(format!("{label}: exit {}. Log: {}\n{}", r.code, log.display(), tail(&r.stdout, 40)));
    }
    Ok(r)
}

/// A held `flock`, released on drop.
struct Flock(File);

impl Drop for Flock {
    fn drop(&mut self) {
        // SAFETY: flock on a descriptor we own.
        unsafe {
            libc::flock(self.0.as_raw_fd(), libc::LOCK_UN);
        }
    }
}

pub struct Land {
    pub project: String,
    pub workspace: PathBuf,
    pub bare: PathBuf,
    pub awm_dir: PathBuf,
    pub node: String,
    pub fleet: fleetconf::Fleet,
    restart: String,
    pub dir: PathBuf,
    pub tickets: PathBuf,
    pub tree: PathBuf,
    pub batch_file: PathBuf,
}

impl Land {
    pub fn new(project: &str, workspace: &Path, awm_dir: &Path, node: &str, fleet: fleetconf::Fleet) -> Result<Self> {
        let bare = workspace.join("projects").join(project).join(".bare");
        if !bare.is_dir() {
            return exit(format!("no repo for {project} at {}", bare.display()));
        }
        let dir = awm_dir.join("land").join(project);
        let tickets = dir.join("tickets");
        std::fs::create_dir_all(&tickets).map_err(io)?;
        Ok(Self {
            project: project.into(),
            workspace: workspace.into(),
            bare,
            awm_dir: awm_dir.into(),
            node: node.into(),
            fleet,
            restart: String::new(),
            tree: dir.join("tree"),
            batch_file: dir.join("batch.json"),
            tickets,
            dir,
        })
    }

    // -- state ----------------------------------------------------------------

    pub fn ticket(&self, tid: &str) -> Result<J> {
        let p = self.tickets.join(format!("{tid}.json"));
        if !p.is_file() {
            return exit(format!("no ticket {tid} in {}", self.project));
        }
        read_json(&p)
    }

    pub fn save(&self, t: &J) -> Result<()> {
        write_json(&self.tickets.join(format!("{}.json", t.str("id"))), t).map_err(io)
    }

    pub fn queued(&self) -> Result<Vec<J>> {
        let mut out = vec![];
        for entry in std::fs::read_dir(&self.tickets).map_err(io)? {
            let p = entry.map_err(io)?.path();
            if p.extension().is_some_and(|e| e == "json") {
                let t = read_json(&p)?;
                if t.str("state") == "queued" {
                    out.push(t);
                }
            }
        }
        out.sort_by(|a, b| (a.str("submitted"), a.str("id")).cmp(&(b.str("submitted"), b.str("id"))));
        Ok(out)
    }

    pub fn batch(&self) -> Result<Option<J>> {
        if self.batch_file.is_file() {
            read_json(&self.batch_file).map(Some)
        } else {
            Ok(None)
        }
    }

    pub fn save_batch(&self, b: &J) -> Result<()> {
        write_json(&self.batch_file, b).map_err(io)
    }

    pub fn lease(&self) -> Result<Option<J>> {
        let p = self.dir.join("lease.json");
        if !p.is_file() {
            return Ok(None);
        }
        let lease = read_json(&p)?;
        let until = lease.get("until").and_then(J::as_f64).unwrap_or(0.0);
        Ok((lease.truthy() && until > epoch()).then_some(lease))
    }

    pub fn take_lease(&self, ticket: &str) -> Result<()> {
        write_json(&self.dir.join("lease.json"), &J::from([("ticket", J::from(ticket)), ("until", J::from(epoch() + LEASE))])).map_err(io)
    }

    pub fn drop_lease(&self) {
        let _ = std::fs::remove_file(self.dir.join("lease.json"));
    }

    /// The ticket whose agent merges now, or "" when the role is free.
    pub fn merger(&self) -> Result<String> {
        if let Some(lease) = self.lease()? {
            return Ok(lease.str("ticket").to_string());
        }
        Ok(if self.lock_free()? { String::new() } else { "(a running batch)".into() })
    }

    /// The project's land lock, so two batch runs never overlap. None when another process
    /// holds it.
    fn lock(&self) -> Result<Option<Flock>> {
        let f = File::create(self.dir.join("lock")).map_err(io)?;
        // SAFETY: flock on a descriptor we own.
        let rc = unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
        Ok((rc == 0).then_some(Flock(f)))
    }

    fn lock_free(&self) -> Result<bool> {
        Ok(self.lock()?.is_some())
    }

    // -- verbs ----------------------------------------------------------------

    pub fn submit(&self, cwd: &Path, brief: &str, restart: &str) -> Result<J> {
        let branch = git_check(cwd, &["rev-parse", "--abbrev-ref", "HEAD"], &HashMap::new())?.stdout.trim().to_string();
        if [DEV, RELEASE, "HEAD"].contains(&branch.as_str()) {
            return exit(format!("submit a feature branch, not {branch}"));
        }
        if !git(cwd, &["status", "--porcelain", "--untracked-files=no"]).stdout.trim().is_empty() {
            return exit("commit your work first: the worktree has uncommitted changes");
        }
        let sha = git_check(cwd, &["rev-parse", "HEAD"], &HashMap::new())?.stdout.trim().to_string();
        if rev(&self.bare, &sha).is_empty() {
            return exit(format!("{} is not in {}. Push {branch} there first", short(&sha), self.bare.display()));
        }
        let base = self.workspace.join("projects").join(&self.project);
        let (cwd_abs, base_abs) = (cwd.canonicalize().map_err(io)?, base.canonicalize().map_err(io)?);
        let rel = cwd_abs.strip_prefix(&base_abs).map_err(|_| LandError::Exit(format!("{} is not under {}", cwd_abs.display(), base_abs.display())))?;
        let restart = restart_list(restart)?;
        let t = J::from([
            ("id", J::from(crate::new_id())),
            ("project", J::from(self.project.as_str())),
            ("scope", J::from(if rel.as_os_str().is_empty() { ".".to_string() } else { rel.to_string_lossy().into_owned() })),
            ("branch", J::from(branch.as_str())),
            ("sha", J::from(sha.as_str())),
            ("brief", J::from(brief)),
            ("state", J::from("queued")),
            ("restart", J::Arr(restart.into_iter().map(J::from).collect())),
            ("submitted", J::from(now())),
            ("verdict", J::from("")),
            ("reason", J::from("")),
            ("batch", J::from("")),
        ]);
        self.save(&t)?;
        let role = if self.merger()?.is_empty() { "merger" } else { "queued" };
        Ok(J::from([("ticket", J::from(t.str("id"))), ("role", J::from(role)), ("branch", J::from(branch)), ("sha", J::from(sha))]))
    }

    pub fn wait(&self, tid: &str, poll: f64) -> Result<J> {
        loop {
            let t = self.ticket(tid)?;
            if SETTLED.contains(&t.str("state")) {
                return Ok(J::from([
                    ("ticket", J::from(tid)),
                    ("verdict", J::from(t.str("state"))),
                    ("reason", t.get("reason").cloned().unwrap_or(J::Null)),
                    ("release", t.get("release").cloned().unwrap_or(J::from(""))),
                ]));
            }
            let m = self.merger()?;
            if m.is_empty() || m == tid {
                return Ok(J::from([
                    ("ticket", J::from(tid)),
                    ("verdict", J::from("merger")),
                    ("reason", J::from(format!("no agent merges now. Load ci/merge and run `land.sh batch --ticket {tid}`."))),
                ]));
            }
            std::thread::sleep(Duration::from_secs_f64(poll.max(0.0)));
        }
    }

    fn own(&self, ticket: &str) -> Result<()> {
        let m = self.merger()?;
        if !m.is_empty() && m != ticket {
            return exit(format!("ticket {m} merges now. Run `land.sh wait {ticket}`"));
        }
        Ok(())
    }

    pub fn hold(&self, ticket: &str) -> Result<J> {
        self.own(ticket)?;
        self.take_lease(ticket)?;
        Ok(J::from([("ticket", J::from(ticket)), ("lease_until", J::from(now_plus(LEASE)))]))
    }

    pub fn bounce(&self, tid: &str, why: &str, merger: &str) -> Result<J> {
        self.own(merger)?;
        let Some(_held) = self.lock()? else { return exit("a batch is running. Bounce between batch runs") };
        self.take_lease(merger)?;
        let mut t = self.ticket(tid)?;
        t.set("state", "bounced");
        t.set("reason", why);
        t.set("settled", now());
        self.save(&t)?;
        if let Some(mut b) = self.batch()? {
            if b.get("tickets").map(J::as_arr).unwrap_or(&[]).iter().any(|x| x.as_str() == Some(tid)) {
                // The tree may hold the branch. Start the merge over without it.
                git(&self.tree, &["merge", "--abort"]);
                git(&self.tree, &["reset", "-q", "--hard", b.str("base")]);
                git(&self.tree, &["clean", "-fdq"]);
                b.set("steps", J::obj());
                b.set("nodes", J::Arr(vec![]));
                self.save_batch(&b)?;
            }
        }
        Ok(J::from([("ticket", J::from(tid)), ("verdict", J::from("bounced")), ("reason", J::from(why))]))
    }

    pub fn status(&self) -> Result<J> {
        let queued = self
            .queued()?
            .iter()
            .map(|t| J::from([("id", J::from(t.str("id"))), ("branch", J::from(t.str("branch"))), ("scope", J::from(t.str("scope"))), ("submitted", J::from(t.str("submitted")))]))
            .collect();
        Ok(J::from([
            ("merger", J::from(self.merger()?)),
            ("lease", self.lease()?.unwrap_or(J::Null)),
            ("batch", self.batch()?.unwrap_or(J::Null)),
            ("queued", J::Arr(queued)),
        ]))
    }

    // -- the batch ------------------------------------------------------------

    pub fn run_batch(&mut self, ticket: &str) -> Result<i32> {
        let m = self.merger()?;
        if !m.is_empty() && m != ticket {
            say(&format!("ticket {m} merges now. Run `land.sh wait {ticket}`."));
            return Ok(3);
        }
        let Some(_held) = self.lock()? else {
            say(&format!("a batch is running. Run `land.sh wait {ticket}`."));
            return Ok(3);
        };
        self.take_lease(ticket)?;
        let b = match self.batch()? {
            Some(b) => Some(b),
            None => self.open()?,
        };
        let Some(mut b) = b else {
            say("nothing queued");
            return Ok(0);
        };
        let mut names = vec![];
        for t in b.get("tickets").map(J::as_arr).unwrap_or(&[]) {
            let id = t.as_str().unwrap_or("");
            names.push(format!("{} ({id})", self.ticket(id)?.str("branch")));
        }
        say(&format!("batch {} on release {}: {}", b.str("id"), short(b.str("base")), names.join(", ")));
        match self.steps(&mut b) {
            Ok(()) => {}
            Err(LandError::Stop(e)) => {
                b.set("failed", last_chars(&e, 4000));
                self.save_batch(&b)?;
                self.take_lease(ticket)?;
                say(&format!(
                    "\n✗ {e}\nThe batch keeps its state, and you hold the merge lease until {}. Fix it, then run batch again. Run `land.sh hold --ticket {ticket}` to keep the lease while you work.",
                    now_plus(LEASE)
                ));
                return Ok(1);
            }
            Err(e) => return Err(e),
        }
        self.settle(&mut b)?;
        self.drop_lease();
        Ok(0)
    }

    fn open(&self) -> Result<Option<J>> {
        let waiting = self.queued()?;
        if waiting.is_empty() {
            return Ok(None);
        }
        let base = rev(&self.bare, &format!("refs/heads/{RELEASE}"));
        let tree = self.tree.to_string_lossy().into_owned();
        if self.tree.exists() {
            git(&self.bare, &["worktree", "remove", "--force", &tree]);
        }
        git(&self.bare, &["worktree", "prune"]);
        git_check(&self.bare, &["worktree", "add", "-q", "--detach", &tree, &base], &HashMap::new()).map_err(|e| LandError::Exit(e.to_string()))?;
        let b = J::from([
            ("id", J::from(crate::new_id())),
            ("base", J::from(base.as_str())),
            ("started", J::from(now())),
            ("tickets", J::Arr(waiting.iter().map(|t| J::from(t.str("id"))).collect())),
            ("steps", J::obj()),
            ("nodes", J::Arr(vec![])),
        ]);
        for mut t in waiting {
            t.set("state", "batched");
            t.set("batch", b.str("id"));
            self.save(&t)?;
        }
        self.save_batch(&b)?;
        Ok(Some(b))
    }

    pub fn live(&self, b: &J) -> Result<Vec<J>> {
        let mut out = vec![];
        for t in b.get("tickets").map(J::as_arr).unwrap_or(&[]) {
            let t = self.ticket(t.as_str().unwrap_or(""))?;
            if t.str("state") != "bounced" {
                out.push(t);
            }
        }
        Ok(out)
    }

    fn step_done(b: &J, name: &str) -> bool {
        b.get("steps").and_then(|s| s.get(name)).is_some_and(J::truthy)
    }

    fn mark(b: &mut J, name: &str) {
        if b.get("steps").is_none_or(|s| !matches!(s, J::Obj(_))) {
            b.set("steps", J::obj());
        }
        if let Some(s) = b.get_mut("steps") {
            s.set(name, true);
        }
    }

    fn steps(&mut self, b: &mut J) -> Result<()> {
        let cfg = self.merge(b)?;
        let head = b.str("head").to_string();
        if !head.is_empty() && rev(&self.tree, "HEAD") != head {
            // The merger fixed the code after promote: a new release, rolled out anew.
            say(&format!("the batch moved past release {}: check, promote and roll out again", short(&head)));
            b.set("steps", J::obj());
            b.set("nodes", J::Arr(vec![]));
        }
        if !Self::step_done(b, "pins") {
            self.pins(b, &cfg)?;
        }
        if !Self::step_done(b, "check") {
            self.check(b, &cfg)?;
        }
        if !Self::step_done(b, "promote") {
            self.promote(b, &cfg)?;
        }
        self.rollout(b, &cfg)
    }

    fn merge(&self, b: &mut J) -> Result<DeployConfig> {
        step("merge");
        if git(&self.tree, &["rev-parse", "-q", "--verify", "MERGE_HEAD"]).code == 0 {
            return stop(format!("merge: a merge is in progress in {}. Resolve it and commit", self.tree.display()));
        }
        for t in self.live(b)? {
            if is_ancestor(&self.tree, t.str("sha"), "HEAD") {
                continue;
            }
            let msg = format!("land: merge {} ({})", t.str("branch"), t.str("scope"));
            let r = git_env(&self.tree, &["merge", "--no-edit", "-m", &msg, t.str("sha")], &id_env());
            if r.code != 0 {
                let files: Vec<String> = git(&self.tree, &["diff", "--name-only", "--diff-filter=U"]).stdout.split_whitespace().map(str::to_string).collect();
                let what = if files.is_empty() { last_chars(r.stdout.trim(), 400) } else { files.join(", ") };
                return stop(format!(
                    "merge: {} (ticket {}) conflicts with the batch in {what}. The merge is left in progress in {}: resolve it and commit, or bounce the ticket",
                    t.str("branch"),
                    t.str("id"),
                    self.tree.display()
                ));
            }
            say(&format!("merged {}", t.str("branch")));
            if let Some(s) = b.get_mut("steps") {
                s.remove("pins");
                s.remove("check");
            }
        }
        let p = self.tree.join(deployconf::FILE);
        if !p.is_file() {
            return Ok(DeployConfig::default());
        }
        let text = std::fs::read_to_string(&p).map_err(io)?;
        deployconf::parse(&text).map_err(|e| LandError::Stop(format!("merge: {e}")))
    }

    fn deploy_env(&self, b: &J, with_dir: bool) -> HashMap<String, String> {
        let mut env = HashMap::from([("AWM_DEPLOY_BASE".to_string(), b.str("base").to_string()), ("AWM_DEPLOY_HEAD".to_string(), rev(&self.tree, "HEAD"))]);
        if with_dir {
            env.insert("AWM_DIR".into(), self.awm_dir.to_string_lossy().into_owned());
        }
        env
    }

    fn pins(&self, b: &mut J, cfg: &DeployConfig) -> Result<()> {
        if !cfg.artifact_refresh.is_empty() {
            step("pins");
            run(&cfg.artifact_refresh, &self.tree, "pins", &self.log(b, "pins")?, cfg.artifact_refresh_timeout, &self.deploy_env(b, true), false)?;
            let mut args = vec!["status", "--porcelain", "--"];
            args.extend(cfg.artifact_pins.iter().map(String::as_str));
            let changed: Vec<String> = git(&self.tree, &args).stdout.lines().filter(|l| !l.is_empty()).map(|l| l.chars().skip(3).collect()).collect();
            if !changed.is_empty() {
                let mut add = vec!["add", "--"];
                add.extend(changed.iter().map(String::as_str));
                git_check(&self.tree, &add, &HashMap::new())?;
                git_check(&self.tree, &["commit", "-q", "-m", "deploy: refresh pins"], &id_env())?;
                say(&format!("committed new pins: {}", changed.join(", ")));
            }
        }
        Self::mark(b, "pins");
        self.save_batch(b)
    }

    fn check(&self, b: &mut J, cfg: &DeployConfig) -> Result<()> {
        if !cfg.check.is_empty() {
            step("check");
            run(&cfg.check, &self.tree, "check", &self.log(b, "check")?, cfg.check_timeout, &self.deploy_env(b, false), false)?;
        }
        Self::mark(b, "check");
        self.save_batch(b)
    }

    fn promote(&self, b: &mut J, cfg: &DeployConfig) -> Result<()> {
        step("promote");
        let head = rev(&self.tree, "HEAD");
        let (rel, dev) = (rev(&self.bare, &format!("refs/heads/{RELEASE}")), rev(&self.bare, &format!("refs/heads/{DEV}")));
        let expected = match b.str("head") {
            "" => b.str("base").to_string(),
            h => h.to_string(),
        };
        if rel != expected {
            return stop(format!(
                "promote: release moved from {} to {} under the batch. Merge release into {}, then run batch again",
                short(&expected),
                short(&rel),
                self.tree.display()
            ));
        }
        if !dev.is_empty() && !is_ancestor(&self.bare, &dev, &head) {
            return stop(format!("promote: dev at {} holds commits release does not. Land them through the queue first", short(&dev)));
        }
        if !cfg.backup.is_empty() {
            self.backup_in_line(&cfg.backup, &head)?;
        }
        let clean = self.clean_checkouts();
        let zero = "0".repeat(40);
        let input = format!(
            "start\nupdate refs/heads/{DEV} {head} {}\nupdate refs/heads/{RELEASE} {head} {rel}\ncommit\n",
            if dev.is_empty() { zero.as_str() } else { dev.as_str() }
        );
        let r = sh::git(&self.bare, &["update-ref", "--stdin"], &HashMap::new(), Some(&input));
        if r.code != 0 {
            return stop(format!("promote: {}", last_chars(r.stderr.trim(), 500)));
        }
        for (path, old) in clean {
            git(&path, &["read-tree", "-m", "-u", &old, &head]);
        }
        say(&format!("release {} -> {}", short(&rel), short(&head)));
        if !cfg.backup.is_empty() {
            let r = git(&self.bare, &["push", "--porcelain", &cfg.backup, &format!("{head}:refs/heads/{RELEASE}")]);
            if r.code != 0 {
                let why = if r.stderr.trim().is_empty() { &r.stdout } else { &r.stderr };
                return stop(format!("backup: push to {} failed: {}", cfg.backup, last_chars(why.trim(), 500)));
            }
            say(&format!("backed up to {}", cfg.backup));
        }
        Self::mark(b, "promote");
        b.set("head", head);
        self.save_batch(b)
    }

    fn backup_in_line(&self, remote: &str, head: &str) -> Result<()> {
        let r = git(&self.bare, &["ls-remote", "--exit-code", remote, &format!("refs/heads/{RELEASE}")]);
        if r.code == 2 {
            return Ok(());
        }
        if r.code != 0 {
            return stop(format!("backup: cannot read {RELEASE} on {remote}: {}", last_chars(r.stderr.trim(), 300)));
        }
        let tip = r.stdout.split_whitespace().next().unwrap_or("").to_string();
        if rev(&self.bare, &tip).is_empty() || !is_ancestor(&self.bare, &tip, head) {
            return stop(format!(
                "backup: {RELEASE} on {remote} is at {}, which the batch does not contain. Bring {remote} in line by hand. Never force",
                short(&tip)
            ));
        }
        Ok(())
    }

    fn clean_checkouts(&self) -> Vec<(PathBuf, String)> {
        let (mut found, mut path, mut head) = (vec![], None::<PathBuf>, String::new());
        for line in git(&self.bare, &["worktree", "list", "--porcelain"]).stdout.lines() {
            if let Some(p) = line.strip_prefix("worktree ") {
                path = Some(PathBuf::from(p));
            } else if let Some(h) = line.strip_prefix("HEAD ") {
                head = h.to_string();
            } else if line == format!("branch refs/heads/{DEV}") || line == format!("branch refs/heads/{RELEASE}") {
                if let Some(p) = &path {
                    if git(p, &["status", "--porcelain"]).stdout.trim().is_empty() {
                        found.push((p.clone(), head.clone()));
                    }
                }
            }
        }
        found
    }

    // -- rollout --------------------------------------------------------------

    fn rollout(&mut self, b: &mut J, cfg: &DeployConfig) -> Result<()> {
        let (to, frm) = (b.str("head").to_string(), b.str("base").to_string());
        self.restart = batch_restart(&self.live(b)?);
        say(&format!("restart: {}", if self.restart.is_empty() { "none" } else { &self.restart }));
        let done: BTreeSet<String> = b
            .get("nodes")
            .map(J::as_arr)
            .unwrap_or(&[])
            .iter()
            .filter(|n| n.get("ok").is_some_and(J::truthy))
            .map(|n| n.str("node").to_string())
            .collect();
        let mut nodes = self.fleet.rollout(&self.project);
        if nodes.is_empty() {
            nodes = vec![self.node.clone()];
        }
        let mut pinned: Option<Vec<artifacts::Artifact>> = None;
        for node in nodes {
            if done.contains(&node) {
                continue;
            }
            step(&format!("rollout {node}"));
            if node != self.node && self.fleet.sleepers.contains(&node) && !answers(&node) {
                say(&format!("{node} is asleep: skipped. It takes the release at its next refresh"));
                push_node(b, J::from([("node", J::from(node.as_str())), ("ok", J::from(true)), ("detail", J::from("asleep"))]));
                self.save_batch(b)?;
                continue;
            }
            let log = self.log(b, &format!("rollout-{node}"))?;
            if node != self.node {
                let url = self.fleet.push_url(&node, &self.project);
                if !url.is_empty() {
                    let cmd = format!("git -C {} push --porcelain {} {to}:refs/{}/release", q(&self.bare.to_string_lossy()), q(&url), self.node);
                    run(&cmd, &self.bare, &format!("push to {node}"), &log, 300.0, &HashMap::new(), false)?;
                }
                let dest = self.fleet.artifacts_dest(&node);
                if !dest.is_empty() {
                    if pinned.is_none() {
                        pinned = Some(self.pinned(&to)?);
                    }
                    for art in pinned.as_deref().unwrap_or(&[]) {
                        let res = artifacts::push(&self.awm_dir.join("artifacts"), art, &dest);
                        if !res.ok() {
                            return stop(format!("rollout {node}: push of {} to {dest} failed\n{}", art.file, last_chars(&res.stdout, 1500)));
                        }
                    }
                }
            }
            let out = run(&self.refresh_command(&node, cfg, &to), Path::new("/"), &format!("rollout {node}"), &log, 1800.0, &self.refresh_env(&node, &frm, &to), true)?;
            let result = parse_result(&out.stdout);
            let ok = match &result {
                Some(r) => r.get("ok").is_some_and(J::truthy),
                None => out.code == 0,
            };
            let mut entry = J::from([("node", J::from(node.as_str())), ("ok", J::from(ok)), ("log", J::from(log.to_string_lossy().into_owned()))]);
            if let Some(r) = &result {
                entry.set("result", r.clone());
            }
            push_node(b, entry);
            self.save_batch(b)?;
            if !ok {
                let where_ = match &result {
                    Some(r) => format!("step {}, log {} on {node}", py_repr(r.get("failed_step")), py_repr(r.get("log"))),
                    None => format!("exit {}", out.code),
                };
                return stop(format!(
                    "rollout {node}: the refresh failed ({where_}). Its output is in {}. The nodes after {node} still run the old release\n{}",
                    log.display(),
                    tail(&out.stdout, 40)
                ));
            }
            say(&format!("{node} runs {}", short(&to)));
        }
        Ok(())
    }

    fn pinned(&self, to: &str) -> Result<Vec<artifacts::Artifact>> {
        let arts = artifacts::pinned(&self.bare, to).map_err(|e| LandError::Stop(format!("rollout: artifacts: {e}")))?;
        for art in &arts {
            artifacts::check(&self.awm_dir.join("artifacts"), art).map_err(|e| LandError::Stop(format!("rollout: artifacts: {e}")))?;
        }
        Ok(arts)
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
            let root = if self.project == "awm" { self.workspace.clone() } else { self.workspace.join("projects").join(&self.project).join("release") };
            return format!("cd {} && {body}", q(&root.to_string_lossy()));
        }
        let own = self.fleet.refresh_command(node);
        if !own.is_empty() {
            return own;
        }
        let root = if self.project == "awm" { "agentic_workspace".to_string() } else { format!("agentic_workspace/projects/{}/release", self.project) };
        format!("ssh -o BatchMode=yes {} {}", q(node), q(&format!("cd {root} && {body}")))
    }

    fn refresh_env(&self, node: &str, frm: &str, to: &str) -> HashMap<String, String> {
        [
            ("AWM_REFRESH_NODE", node),
            ("AWM_REFRESH_PROJECT", self.project.as_str()),
            ("AWM_REFRESH_FROM", frm),
            ("AWM_REFRESH_TO", to),
            ("AWM_NODE_NAME", node),
            ("AWM_REFRESH_RESTART", self.restart.as_str()),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
    }

    // -- settle ---------------------------------------------------------------

    fn settle(&self, b: &mut J) -> Result<()> {
        step("settle");
        let head = b.str("head").to_string();
        for mut t in self.live(b)? {
            t.set("state", "landed");
            t.set("release", head.as_str());
            t.set("settled", now());
            self.save(&t)?;
            say(&format!("✓ {} ({}) landed in release {}", t.str("branch"), t.str("id"), short(&head)));
        }
        for n in b.get("nodes").map(J::as_arr).unwrap_or(&[]) {
            let ok = if n.get("ok").is_some_and(J::truthy) { "ok" } else { "failed" };
            let detail = match n.str("detail") {
                "" => String::new(),
                d => format!(" ({d})"),
            };
            say(&format!("  {}: {ok}{detail}", n.str("node")));
        }
        let hist = self.dir.join("batches");
        std::fs::create_dir_all(&hist).map_err(io)?;
        b.set("ended", now());
        write_json(&hist.join(format!("{}.json", b.str("id"))), b).map_err(io)?;
        std::fs::remove_file(&self.batch_file).map_err(io)?;
        git(&self.bare, &["worktree", "remove", "--force", &self.tree.to_string_lossy()]);
        Ok(())
    }

    fn log(&self, b: &J, name: &str) -> Result<PathBuf> {
        let d = self.dir.join("logs").join(b.str("id"));
        std::fs::create_dir_all(&d).map_err(io)?;
        Ok(d.join(format!("{name}.log")))
    }
}

fn push_node(b: &mut J, entry: J) {
    if b.get("nodes").and_then(|n| if let J::Arr(_) = n { Some(()) } else { None }).is_none() {
        b.set("nodes", J::Arr(vec![]));
    }
    if let Some(nodes) = b.get_mut("nodes").and_then(J::as_arr_mut) {
        nodes.push(entry);
    }
}

/// How Python's f-string shows a JSON value: a string bare, a missing value as `None`.
fn py_repr(v: Option<&J>) -> String {
    match v {
        None | Some(J::Null) => "None".into(),
        Some(J::Str(s)) => s.clone(),
        Some(J::Bool(b)) => if *b { "True" } else { "False" }.into(),
        Some(other) => json::dumps(other),
    }
}

fn project_of(cwd: &Path, workspace: &Path) -> Result<String> {
    let projects = workspace.join("projects");
    let (cwd, projects) = (cwd.canonicalize().unwrap_or(cwd.to_path_buf()), projects.canonicalize().unwrap_or(projects));
    match cwd.strip_prefix(&projects).ok().and_then(|r| r.iter().next()) {
        Some(p) => Ok(p.to_string_lossy().into_owned()),
        None => exit("run this from a scope worktree under projects/, or pass --project"),
    }
}

/// The `land` subcommand, as `scripts/land.sh` runs it.
#[derive(Debug, clap::Args)]
pub struct LandArgs {
    /// Default: the project of the working directory.
    #[arg(long)]
    pub project: Option<String>,
    #[command(subcommand)]
    pub verb: LandVerb,
}

#[derive(Debug, clap::Subcommand)]
pub enum LandVerb {
    /// Queue this worktree's branch with its merge brief.
    Submit {
        /// The merge brief file.
        #[arg(long)]
        brief: PathBuf,
        /// What must restart on each node: plugin names (a,b), gateway, or none.
        #[arg(long)]
        restart: String,
    },
    /// Wait for a ticket's verdict: landed, bounced or merger.
    Wait {
        ticket: String,
        #[arg(long, default_value_t = 10.0)]
        poll: f64,
    },
    /// Run the batch as its merger.
    Batch {
        /// Your own ticket.
        #[arg(long)]
        ticket: String,
    },
    /// Keep the merge lease while you fix the batch.
    Hold {
        /// Your own ticket.
        #[arg(long)]
        ticket: String,
    },
    /// Return a ticket to its owner.
    Bounce {
        ticket: String,
        #[arg(long)]
        why: String,
        /// Your own ticket.
        #[arg(long = "ticket")]
        merger: String,
    },
    /// The lock, the batch and the queue.
    Status,
}

/// Run one land verb. Returns the exit code: 0, 1 for a failed batch step or a refusal, 3
/// when another agent merges now.
pub fn main(args: LandArgs) -> i32 {
    match run_main(args) {
        Ok(code) => code,
        Err(e) => {
            eprintln!("{e}");
            1
        }
    }
}

fn run_main(a: LandArgs) -> Result<i32> {
    let ws = awm_plugin_sdk::config::canonical_workspace();
    let awm_dir = awm_plugin_sdk::config::awm_dir();
    let cwd = std::env::var_os("AWM_LAND_CWD").filter(|c| !c.is_empty()).map(PathBuf::from).unwrap_or_else(|| std::env::current_dir().unwrap_or_default());
    let project = match a.project {
        Some(p) => p,
        None => project_of(&cwd, &ws)?,
    };
    let install = std::env::var_os("AWM_INSTALL_CONFIG").filter(|c| !c.is_empty()).map(PathBuf::from).unwrap_or_else(|| awm_dir.join(fleetconf::FILE));
    let fleet = fleetconf::load(&install).map_err(|e| LandError::Exit(e.0))?;
    let mut land = Land::new(&project, &ws, &awm_dir, &awm_plugin_sdk::config::node_name(), fleet)?;
    let res = match a.verb {
        LandVerb::Batch { ticket } => return land.run_batch(&ticket),
        LandVerb::Submit { brief, restart } => {
            let text = std::fs::read_to_string(&brief).map_err(|e| LandError::Exit(format!("{}: {e}", brief.display())))?;
            land.submit(&cwd, &text, &restart)?
        }
        LandVerb::Wait { ticket, poll } => land.wait(&ticket, poll)?,
        LandVerb::Bounce { ticket, why, merger } => land.bounce(&ticket, &why, &merger)?,
        LandVerb::Hold { ticket } => land.hold(&ticket)?,
        LandVerb::Status => land.status()?,
    };
    say(&json::dumps(&res));
    Ok(0)
}
