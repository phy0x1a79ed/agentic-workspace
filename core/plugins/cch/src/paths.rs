//! Where cch reads and writes, and the knobs that shape the warm pool: the one place cch reads
//! an `AWM_*` environment variable.

use std::path::{Path, PathBuf};
use std::time::Duration;

use awm_harness::claude::launch::default_claude_bin;
use awm_harness::claude::records::read_json;
use awm_harness::claude::ClaudeHome;
use serde_json::{Map, Value};

/// The flags that let a pool session run without permission prompts.
pub const SKIP_PERMISSION_FLAGS: [&str; 2] = ["--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"];

/// cch's paths and knobs, read once. Every field is public so a test can aim one elsewhere.
#[derive(Debug, Clone, PartialEq)]
pub struct Paths {
    /// Claude Code's home. Its roster, jobs, sessions and transcripts are read through it only.
    pub home: ClaudeHome,
    /// `AWM_CX_STATE`, else `<claude home>/cx`: the lineage records and the MCP config. The
    /// gateway's `resolve` reads the lineage through the same value, so writer and reader agree.
    pub cx_state: PathBuf,
    /// `AWM_DIR`, else `<workspace>/.awm`.
    pub awm_dir: PathBuf,
    /// cch's own state directory, `<AWM_DIR>/services/cch`.
    pub service_dir: PathBuf,
    /// `AWM_CX_CLAUDE`, else the `claude` on `PATH`, else `~/.local/bin/claude`. Resolved, not
    /// looked up at use: under systemd `PATH` lacks `~/.local/bin`, and a `claude` that is not
    /// found there fails silently and leaves the pool empty while everything reports healthy.
    pub claude_bin: PathBuf,
    /// `AWM_CX_SEED_DIR`, else `~/.local/state/awm/seed`. It must hold no CLAUDE.md: a move adds
    /// the destination's file and keeps the origin's, so seeding beside one carries it everywhere.
    pub seed_dir: PathBuf,
    /// `AWM_CX_MODEL`, else `opus`.
    pub model: String,
    /// `AWM_CX_EFFORT`, else `high`.
    pub effort: String,
    /// `AWM_CX_RESTRICTED_SETTINGS`, else `<claude home>/hooks/awm-operations-settings.json`.
    pub restricted_settings: PathBuf,
    /// `AWM_CX_WANT`, else 1: how many sessions to keep warm. Zero stops the pool, not cch.
    pub want: u64,
    /// `AWM_CX_ROTATE_AGE_S`, else 2400. The daemon retires an idle session at about 61
    /// minutes, so a replacement must be seeded and verified well before that.
    pub rotate_age_s: f64,
    /// `AWM_CX_TICK_S`, else 5.
    pub tick_s: f64,
    /// False when `AWM_CX_LOOP` is `0`, so a shadow run does not seed against the base's home.
    pub loop_enabled: bool,
    /// `AWM_CX_POOL` when it is `1` or `0`; it overrides `enabled.json`.
    pub pool_forced: Option<bool>,
    /// `AWM_CX_PROJECTS`; None means `<canonical workspace>/projects`.
    pub projects: Option<PathBuf>,
    /// `AWM_CX_MCP_SOURCE`; None means `<canonical workspace>/.mcp.json`.
    pub mcp_source: Option<PathBuf>,
    /// `AWM_CX_LOBBY`, else `~/.local/state/awm/lobby`: where a session with no scope starts,
    /// outside every git checkout so it can commit nothing (scopes.md R40).
    pub lobby_dir: PathBuf,
    /// The workspace; None asks the SDK at use, since the gateway names it only on register.
    pub workspace: Option<PathBuf>,
    /// The daemon keeper's knobs.
    pub keep: KeepKnobs,
    /// The isolated sessions' knobs.
    pub isolated: IsolatedKnobs,
    /// False when `AWM_CCH_HOOKS` is `off`, so a sandbox copy leaves `settings.json` alone.
    pub hooks_reconcile: bool,
    /// `AWM_SOCIAL_PEER`: the node whose social plugin carries the low-memory notice.
    pub social_peer: Option<String>,
    /// `AWM_OPERATOR_DM_ACCOUNT` and `AWM_OPERATOR_DM_CHANNEL`: where the low-memory notice goes.
    /// None when either is unset, and the notice is then only logged.
    pub operator_dm: Option<(String, String)>,
}

/// The daemon keeper's knobs.
#[derive(Debug, Clone, PartialEq)]
pub struct KeepKnobs {
    /// False when `AWM_KEEP` is `0`: this copy leaves the daemon alone.
    pub enabled: bool,
    /// `AWM_KEEP_CLAUDE`, else [`Paths::claude_bin`]'s default.
    pub claude_bin: PathBuf,
    /// `AWM_KEEP_UNIT`, else `awm-claude-daemon`.
    pub unit: String,
    /// `AWM_KEEP_MEMORY_LOW`, else 0.12: the free-memory fraction below which memwatch counts.
    pub memory_low: f64,
    /// `AWM_KEEP_MEMORY_HIGH`, the unit's `MemoryHigh`.
    pub memory_high: Option<String>,
    /// `AWM_KEEP_MEMORY_MAX`, the unit's `MemoryMax`.
    pub memory_max: Option<String>,
    /// `AWM_SESSION_PATH`: a colon list of directories the sessions' `PATH` gains ahead of the
    /// system ones. None means the keeper takes them from the running daemon unit's `PATH`.
    pub session_path: Option<Vec<String>>,
    /// `AWM_WORKSPACE` and `CLAUDE_CONFIG_DIR` as this process sees them, passed to the daemon.
    pub passed_env: Vec<(String, String)>,
}

/// The isolated sessions' knobs.
#[derive(Debug, Clone, PartialEq)]
pub struct IsolatedKnobs {
    /// `AWM_ISOLATED_DIR`, else `<service dir>/isolated`: tokens and one folder per session.
    pub dir: PathBuf,
    /// `AWM_ISOLATED_DOCKER_HOST`; None means the user's rootless socket.
    pub docker_host: Option<String>,
    /// `AWM_ISOLATED_HUB_URL`, else `http://host.docker.internal:8765`: the door, seen from inside.
    pub hub_url: String,
    /// `AWM_ISOLATED_IMAGE`, else `awm-isolated:1`.
    pub image: String,
    /// `AWM_ISOLATED_NETWORK`, else `bridge`.
    pub network: String,
    /// `AWM_ISOLATED_CPUS`, else `2`.
    pub cpus: String,
    /// `AWM_ISOLATED_MEMORY`, else `4g`.
    pub memory: String,
    /// `AWM_ISOLATED_PIDS`, else 512.
    pub pids: u64,
}

/// `text` with Python's numeric-literal underscores removed, or None where Python would refuse them.
fn without_underscores(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    let ok = bytes.iter().enumerate().all(|(i, b)| {
        *b != b'_'
            || (i > 0 && bytes[i - 1].is_ascii_digit() && bytes.get(i + 1).is_some_and(u8::is_ascii_digit))
    });
    ok.then(|| text.replace('_', ""))
}

/// Python's `int(text)` for a decimal literal.
fn py_int(text: &str) -> Option<i64> {
    let t = without_underscores(text.trim())?;
    let digits = t.strip_prefix(['+', '-']).unwrap_or(&t);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    t.parse().ok()
}

/// Python's `float(text)`.
fn py_float(text: &str) -> Option<f64> {
    without_underscores(text.trim())?.parse().ok()
}

/// Python's `max(0, int(raw or default))`, falling back to `default` when `raw` is not an integer.
fn count(raw: Option<String>, default: u64) -> u64 {
    match raw {
        None => default,
        Some(text) => py_int(&text).map_or(default, |n| u64::try_from(n).unwrap_or(0)),
    }
}

fn seconds(raw: Option<String>, default: f64) -> f64 {
    raw.map_or(default, |text| py_float(&text).unwrap_or(default))
}

fn forced(raw: Option<String>) -> Option<bool> {
    match raw.as_deref() {
        Some("1") => Some(true),
        Some("0") => Some(false),
        _ => None,
    }
}

impl Paths {
    /// The paths and knobs this process's environment names.
    pub fn from_env() -> Self {
        Self::from_vars(ClaudeHome::from_env(), awm_plugin_sdk::config::awm_dir(), |name| std::env::var(name).ok())
    }

    /// The paths and knobs that `lookup` names, over Claude's `home` and the AWM dir `awm_dir`.
    /// An empty variable counts as unset, as `os.environ.get(name) or default` reads it.
    pub fn from_vars(home: ClaudeHome, awm_dir: PathBuf, lookup: impl Fn(&str) -> Option<String>) -> Self {
        let var = |name: &str| lookup(name).filter(|v| !v.is_empty());
        let path = |name: &str| var(name).map(PathBuf::from);
        let home = match path("AWM_CX_TRUST_FILE") {
            Some(trust) => ClaudeHome::at(home.root, trust),
            None => home,
        };
        let user = path("HOME").unwrap_or_else(|| PathBuf::from("/"));
        Self {
            cx_state: path("AWM_CX_STATE").unwrap_or_else(|| home.root.join("cx")),
            service_dir: awm_dir.join("services").join("cch"),
            claude_bin: path("AWM_CX_CLAUDE").unwrap_or_else(default_claude_bin),
            seed_dir: path("AWM_CX_SEED_DIR").unwrap_or_else(|| user.join(".local").join("state").join("awm").join("seed")),
            model: var("AWM_CX_MODEL").unwrap_or_else(|| "opus".into()),
            effort: var("AWM_CX_EFFORT").unwrap_or_else(|| "high".into()),
            restricted_settings: path("AWM_CX_RESTRICTED_SETTINGS")
                .unwrap_or_else(|| home.root.join("hooks").join("awm-operations-settings.json")),
            want: count(var("AWM_CX_WANT"), 1),
            rotate_age_s: seconds(var("AWM_CX_ROTATE_AGE_S"), 2400.0),
            tick_s: seconds(var("AWM_CX_TICK_S"), 5.0),
            loop_enabled: var("AWM_CX_LOOP").as_deref() != Some("0"),
            pool_forced: forced(var("AWM_CX_POOL")),
            projects: path("AWM_CX_PROJECTS"),
            mcp_source: path("AWM_CX_MCP_SOURCE"),
            lobby_dir: path("AWM_CX_LOBBY")
                .unwrap_or_else(|| user.join(".local").join("state").join("awm").join("lobby")),
            workspace: None,
            keep: KeepKnobs {
                enabled: var("AWM_KEEP").as_deref() != Some("0"),
                claude_bin: path("AWM_KEEP_CLAUDE").unwrap_or_else(default_claude_bin),
                unit: var("AWM_KEEP_UNIT").unwrap_or_else(|| "awm-claude-daemon".into()),
                memory_low: seconds(var("AWM_KEEP_MEMORY_LOW"), 0.12),
                memory_high: var("AWM_KEEP_MEMORY_HIGH").map(|v| v.trim().to_string()).filter(|v| !v.is_empty()),
                memory_max: var("AWM_KEEP_MEMORY_MAX").map(|v| v.trim().to_string()).filter(|v| !v.is_empty()),
                session_path: var("AWM_SESSION_PATH").map(|v| crate::keeper::path_dirs(&v)),
                passed_env: ["AWM_WORKSPACE", "CLAUDE_CONFIG_DIR"]
                    .into_iter()
                    .filter_map(|k| var(k).map(|v| (k.to_string(), v)))
                    .collect(),
            },
            isolated: IsolatedKnobs {
                dir: path("AWM_ISOLATED_DIR").unwrap_or_else(|| awm_dir.join("services").join("cch").join("isolated")),
                docker_host: var("AWM_ISOLATED_DOCKER_HOST"),
                hub_url: var("AWM_ISOLATED_HUB_URL").unwrap_or_else(|| "http://host.docker.internal:8765".into()),
                image: var("AWM_ISOLATED_IMAGE").unwrap_or_else(|| "awm-isolated:1".into()),
                network: var("AWM_ISOLATED_NETWORK").unwrap_or_else(|| "bridge".into()),
                cpus: var("AWM_ISOLATED_CPUS").unwrap_or_else(|| "2".into()),
                memory: var("AWM_ISOLATED_MEMORY").unwrap_or_else(|| "4g".into()),
                pids: count(var("AWM_ISOLATED_PIDS"), 512),
            },
            hooks_reconcile: var("AWM_CCH_HOOKS").as_deref() != Some("off"),
            social_peer: var("AWM_SOCIAL_PEER"),
            operator_dm: var("AWM_OPERATOR_DM_ACCOUNT").zip(var("AWM_OPERATOR_DM_CHANNEL")),
            awm_dir,
            home,
        }
    }

    /// Everything under `root`, with the default knobs and no environment read: Claude's home at
    /// `root/.claude`, the AWM dir at `root/.awm`, the workspace at `root/workspace`, and `root`
    /// standing in for the user's home.
    pub fn under(root: &Path) -> Self {
        let home = ClaudeHome::at(root.join(".claude"), root.join(".claude.json"));
        let awm_dir = root.join(".awm");
        Self {
            cx_state: home.root.join("cx"),
            service_dir: awm_dir.join("services").join("cch"),
            claude_bin: root.join(".local").join("bin").join("claude"),
            seed_dir: root.to_path_buf(),
            model: "opus".into(),
            effort: "high".into(),
            restricted_settings: home.root.join("hooks").join("awm-operations-settings.json"),
            want: 1,
            rotate_age_s: 2400.0,
            tick_s: 5.0,
            loop_enabled: true,
            pool_forced: None,
            projects: None,
            mcp_source: None,
            lobby_dir: root.join(".local").join("state").join("awm").join("lobby"),
            workspace: Some(root.join("workspace")),
            keep: KeepKnobs {
                enabled: true,
                claude_bin: root.join(".local").join("bin").join("claude"),
                unit: "awm-claude-daemon".into(),
                memory_low: 0.12,
                memory_high: None,
                memory_max: None,
                session_path: None,
                passed_env: Vec::new(),
            },
            isolated: IsolatedKnobs {
                dir: awm_dir.join("services").join("cch").join("isolated"),
                docker_host: None,
                hub_url: "http://host.docker.internal:8765".into(),
                image: "awm-isolated:1".into(),
                network: "bridge".into(),
                cpus: "2".into(),
                memory: "4g".into(),
                pids: 512,
            },
            hooks_reconcile: true,
            social_peer: None,
            operator_dm: None,
            awm_dir,
            home,
        }
    }

    /// One JSON lineage record per started session, `<job>.json`, plus the `pending-*` records
    /// written before a launch. It is the only record of who started a session and in what mode.
    pub fn starts_dir(&self) -> PathBuf {
        self.cx_state.join("starts")
    }

    /// Where the record of a start in flight lives: `starts/pending-<token>.json`. The token is
    /// random, because the session id is known only once the job appears.
    pub fn pending_path(&self, token: &str) -> PathBuf {
        self.starts_dir().join(format!("pending-{token}.json"))
    }

    /// The lineage record of the session with canonical id `id`.
    pub fn lineage_path(&self, id: &str) -> PathBuf {
        self.starts_dir().join(format!("{id}.json"))
    }

    /// The promise store, `<service dir>/pending`. [`Paths::legacy_promises_dir`] is where the
    /// Python cch kept it; the start-up migration copies it here once.
    pub fn promises_dir(&self) -> PathBuf {
        self.service_dir.join("pending")
    }

    /// Reflection's old `pending/` dir, the promise store before the Rust cch.
    pub fn legacy_promises_dir(&self) -> PathBuf {
        self.awm_dir.join("services").join("reflection").join("pending")
    }

    /// The keeper's hold record, `<service dir>/hold.json`.
    pub fn hold_path(&self) -> PathBuf {
        self.service_dir.join("hold.json")
    }

    /// The pool ledger, `<service dir>/pool.json`: `{canonical session id: spare | claimed | kept}`
    /// for every session the pool made.
    pub fn ledger_path(&self) -> PathBuf {
        self.service_dir.join("pool.json")
    }

    /// The old keep service's hold record, which the start-up migration copies to [`Paths::hold_path`].
    pub fn legacy_hold_path(&self) -> PathBuf {
        self.awm_dir.join("services").join("keep").join("hold.json")
    }

    /// `<AWM_DIR>/services/enabled.json`, the gateway's `{service: enabled}` table.
    pub fn enabled_file(&self) -> PathBuf {
        self.awm_dir.join("services").join("enabled.json")
    }

    /// Whether this node keeps a warm session: `AWM_CX_POOL`, else the `cx` key of
    /// `enabled.json`, else false. cch runs on every node, so the pool keeps the gate cx had.
    pub fn pool_wanted(&self) -> bool {
        self.pool_forced.unwrap_or_else(|| read_json(&self.enabled_file()).get("cx") == Some(&Value::Bool(true)))
    }

    /// The seconds between pool ticks, as a duration a sleep accepts.
    pub fn tick(&self) -> Duration {
        Duration::try_from_secs_f64(self.tick_s.max(0.0)).unwrap_or(if self.tick_s > 0.0 { Duration::MAX } else { Duration::ZERO })
    }

    /// The environment a pool session is seeded with.
    pub fn seed_env(&self) -> Vec<(String, String)> {
        vec![("ANTHROPIC_MODEL".into(), self.model.clone())]
    }

    /// The flags a pool session is seeded with, in the space-separated form `mode_of` does not
    /// count as a cx start.
    pub fn seed_flags(&self) -> Vec<String> {
        let mut flags: Vec<String> = SKIP_PERMISSION_FLAGS.iter().map(|f| f.to_string()).collect();
        flags.extend(["--effort".to_string(), self.effort.clone()]);
        flags
    }

    /// The workspace the gateway named on register, else the one the SDK finds.
    pub fn workspace_dir(&self) -> PathBuf {
        self.workspace.clone().unwrap_or_else(awm_plugin_sdk::config::canonical_workspace)
    }

    /// Where scope worktrees live: `<workspace>/projects/<project>/<scope>`.
    pub fn projects_dir(&self) -> PathBuf {
        self.projects.clone().unwrap_or_else(|| self.workspace_dir().join("projects"))
    }

    /// Claude Code's transcripts: `<dir per working directory>/<session id>.jsonl`.
    pub fn transcripts_dir(&self) -> PathBuf {
        self.home.projects()
    }

    /// The workspace's own MCP config, which names how to start the awm server.
    pub fn mcp_source(&self) -> PathBuf {
        self.mcp_source.clone().unwrap_or_else(|| self.workspace_dir().join(".mcp.json"))
    }

    /// The `awm` entry of the workspace MCP config, keeping only `type`, `command`, `args` and
    /// `env`; None when it has no such entry with a string `command`. Errors when the config
    /// cannot be read or parsed.
    pub fn awm_mcp_server(&self) -> anyhow::Result<Option<Map<String, Value>>> {
        let data: Value = serde_json::from_slice(&std::fs::read(self.mcp_source())?)?;
        let server = data.get("mcpServers").and_then(|s| s.get("awm")).and_then(Value::as_object);
        let Some(server) = server.filter(|s| s.get("command").is_some_and(Value::is_string)) else {
            return Ok(None);
        };
        Ok(Some(
            ["type", "command", "args", "env"]
                .into_iter()
                .filter_map(|k| server.get(k).map(|v| (k.to_string(), v.clone())))
                .collect(),
        ))
    }

    /// The MCP config cch writes for strict and lobby starts.
    pub fn mcp_config_path(&self) -> PathBuf {
        self.cx_state.join("mcp-awm.json")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tempfile::TempDir;

    #[test]
    fn the_pending_path_is_filed_under_its_token() {
        let p = Paths::under(Path::new("/r"));
        assert_eq!(p.pending_path("cc0123"), Path::new("/r/.claude/cx/starts/pending-cc0123.json"));
        assert_eq!(p.lineage_path("ccabababab000040008000000000000000"), Path::new("/r/.claude/cx/starts/ccabababab000040008000000000000000.json"));
    }

    #[test]
    fn test_the_paths_follow_cx_environment_overrides() {
        let vars: std::collections::HashMap<&str, &str> = [
            ("AWM_CX_STATE", "/t/st"),
            ("AWM_CX_CLAUDE", "/t/claude"),
            ("AWM_CX_TRUST_FILE", "/t/trust.json"),
            ("AWM_CX_ROTATE_AGE_S", "60"),
            ("AWM_CX_LOOP", "0"),
            ("HOME", "/u"),
        ]
        .into();
        let home = ClaudeHome::at("/c", "/c.json");
        let p = Paths::from_vars(home, PathBuf::from("/a"), |k| vars.get(k).map(|v| v.to_string()));
        assert_eq!(p.cx_state, Path::new("/t/st"));
        assert_eq!(p.starts_dir(), Path::new("/t/st/starts"));
        assert_eq!(p.home, ClaudeHome::at("/c", "/t/trust.json"));
        assert_eq!(p.claude_bin, Path::new("/t/claude"));
        assert_eq!((p.rotate_age_s, p.loop_enabled), (60.0, false));
        assert_eq!(p.service_dir, Path::new("/a/services/cch"));
        assert_eq!((p.seed_dir.as_path(), p.lobby_dir.as_path()), (Path::new("/u/.local/state/awm/seed"), Path::new("/u/.local/state/awm/lobby")));
    }

    #[test]
    fn the_awm_server_entry_keeps_only_how_to_start_it() {
        let dir = TempDir::new().unwrap();
        let mut p = Paths::under(dir.path());
        let src = dir.path().join("mcp.json");
        p.mcp_source = Some(src.clone());
        assert!(p.awm_mcp_server().is_err());
        std::fs::write(&src, json!({"mcpServers": {"awm": {"type": "stdio", "command": "awm", "args": ["mcp"], "x": 1}}}).to_string()).unwrap();
        let entry = p.awm_mcp_server().unwrap().unwrap();
        assert_eq!(Value::Object(entry), json!({"type": "stdio", "command": "awm", "args": ["mcp"]}));
        std::fs::write(&src, json!({"mcpServers": {"awm": {"command": 7}}}).to_string()).unwrap();
        assert_eq!(p.awm_mcp_server().unwrap(), None);
        std::fs::write(&src, "[]").unwrap();
        assert_eq!(p.awm_mcp_server().unwrap(), None);
    }
}
