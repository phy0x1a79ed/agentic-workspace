//! Where cch reads and writes, and the knobs that shape the warm pool: the one place cch reads
//! an `AWM_*` environment variable.

use std::path::{Path, PathBuf};
use std::time::Duration;

use awm_harness::claude::launch::default_claude_bin;
use awm_harness::claude::records::read_json;
use awm_harness::claude::ClaudeHome;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

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
    /// `AWM_CX_PREFIX`, else `<warm `. The prefix is how the pool tells its own sessions from
    /// one started by hand, so a renamed session is out of the pool by that alone. It differs
    /// from the shell pool's `<spare ` on purpose: a leftover `<spare ` session is never ours.
    pub name_prefix: String,
    /// `AWM_CX_CLAIMED_PREFIX`, else `claimed `: what a session is renamed to when a terminal
    /// takes it. Claude Code titles only a session whose record has no name, so the rename also
    /// lets the built-in namer replace it later.
    pub claimed_prefix: String,
    /// `AWM_CX_SEED_DIR`, else the user's home. It must hold no CLAUDE.md: a move adds the
    /// destination's file and keeps the origin's, so seeding beside one carries it everywhere.
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
            name_prefix: var("AWM_CX_PREFIX").unwrap_or_else(|| "<warm ".into()),
            claimed_prefix: var("AWM_CX_CLAIMED_PREFIX").unwrap_or_else(|| "claimed ".into()),
            seed_dir: path("AWM_CX_SEED_DIR").unwrap_or_else(|| user.clone()),
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
            name_prefix: "<warm ".into(),
            claimed_prefix: "claimed ".into(),
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
            awm_dir,
            home,
        }
    }

    /// One JSON lineage record per started session, `<job>.json`, plus the `pending-*` records
    /// written before a launch. It is the only record of who started a session and in what mode.
    pub fn starts_dir(&self) -> PathBuf {
        self.cx_state.join("starts")
    }

    /// Where the record of a start in flight for the session `name` lives:
    /// `starts/pending-<sha256(name)[:16]>.json`.
    pub fn pending_path(&self, name: &str) -> PathBuf {
        let digest = Sha256::digest(name.as_bytes());
        let hex: String = digest.iter().take(8).map(|b| format!("{b:02x}")).collect();
        self.starts_dir().join(format!("pending-{hex}.json"))
    }

    /// The lineage record of `job`.
    pub fn lineage_path(&self, job: &str) -> PathBuf {
        self.starts_dir().join(format!("{job}.json"))
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
    fn the_pending_path_is_a_short_digest_of_the_name() {
        let p = Paths::under(Path::new("/r"));
        let digest = Sha256::digest(b"rep");
        let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(p.pending_path("rep"), Path::new("/r/.claude/cx/starts").join(format!("pending-{}.json", &hex[..16])));
        assert_ne!(p.pending_path("rep"), p.pending_path("rep2"));
        assert_eq!(p.lineage_path("abababab"), Path::new("/r/.claude/cx/starts/abababab.json"));
    }

    #[test]
    fn the_default_paths_sit_under_the_claude_home() {
        let p = Paths::under(Path::new("/r"));
        assert_eq!(p.starts_dir(), Path::new("/r/.claude/cx/starts"));
        assert_eq!(p.home.roster(), Path::new("/r/.claude/daemon/roster.json"));
        assert_eq!(p.mcp_config_path(), Path::new("/r/.claude/cx/mcp-awm.json"));
        assert_eq!(p.projects_dir(), Path::new("/r/workspace/projects"));
        assert_eq!(p.mcp_source(), Path::new("/r/workspace/.mcp.json"));
        assert_eq!(p.transcripts_dir(), Path::new("/r/.claude/projects"));
    }

    #[test]
    fn the_paths_follow_cx_environment_overrides() {
        let vars: std::collections::HashMap<&str, &str> = [
            ("AWM_CX_STATE", "/t/st"),
            ("AWM_CX_CLAUDE", "/t/claude"),
            ("AWM_CX_TRUST_FILE", "/t/trust.json"),
            ("AWM_CX_ROTATE_AGE_S", "60"),
            ("AWM_CX_LOOP", "0"),
            ("AWM_CX_PREFIX", ""),
            ("HOME", "/u"),
        ]
        .into();
        let home = ClaudeHome::at("/c", "/c.json");
        let p = Paths::from_vars(home, PathBuf::from("/a"), |k| vars.get(k).map(|v| v.to_string()));
        assert_eq!(p.cx_state, Path::new("/t/st"));
        assert_eq!(p.starts_dir(), Path::new("/t/st/starts"));
        assert_eq!(p.home, ClaudeHome::at("/c", "/t/trust.json"));
        assert_eq!(p.claude_bin, Path::new("/t/claude"));
        assert_eq!((p.rotate_age_s, p.loop_enabled, p.name_prefix.as_str()), (60.0, false, "<warm "));
        assert_eq!(p.service_dir, Path::new("/a/services/cch"));
        assert_eq!((p.seed_dir.as_path(), p.lobby_dir.as_path()), (Path::new("/u"), Path::new("/u/.local/state/awm/lobby")));
    }

    #[test]
    fn knobs_parse_as_python_parses_them() {
        assert_eq!(count(None, 1), 1);
        assert_eq!(count(Some("3".into()), 1), 3);
        assert_eq!(count(Some(" 1_0 ".into()), 1), 10);
        assert_eq!(count(Some("0".into()), 1), 0);
        assert_eq!(count(Some("-2".into()), 1), 0);
        assert_eq!(count(Some("two".into()), 1), 1);
        assert_eq!(count(Some("1.5".into()), 1), 1);
        assert_eq!(seconds(Some("60".into()), 2400.0), 60.0);
        assert_eq!(seconds(Some("1e1".into()), 5.0), 10.0);
        assert_eq!(seconds(Some("soon".into()), 5.0), 5.0);
        assert_eq!(seconds(Some("1__0".into()), 5.0), 5.0);
        assert_eq!(forced(Some("1".into())), Some(true));
        assert_eq!(forced(Some("0".into())), Some(false));
        assert_eq!(forced(Some("yes".into())), None);
    }

    #[test]
    fn the_pool_is_wanted_by_override_else_by_enabled_json() {
        let dir = TempDir::new().unwrap();
        let mut p = Paths::under(dir.path());
        assert!(!p.pool_wanted());
        std::fs::create_dir_all(p.enabled_file().parent().unwrap()).unwrap();
        std::fs::write(p.enabled_file(), json!({"cx": true}).to_string()).unwrap();
        assert!(p.pool_wanted());
        std::fs::write(p.enabled_file(), json!({"cx": 1}).to_string()).unwrap();
        assert!(!p.pool_wanted());
        std::fs::write(p.enabled_file(), "{torn").unwrap();
        assert!(!p.pool_wanted());
        p.pool_forced = Some(true);
        assert!(p.pool_wanted());
        std::fs::write(p.enabled_file(), json!({"cx": true}).to_string()).unwrap();
        p.pool_forced = Some(false);
        assert!(!p.pool_wanted());
    }

    #[test]
    fn the_seed_is_the_pool_model_and_effort() {
        let p = Paths::under(Path::new("/r"));
        assert_eq!(p.seed_env(), [("ANTHROPIC_MODEL".to_string(), "opus".to_string())]);
        assert_eq!(
            p.seed_flags(),
            ["--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--effort", "high"]
        );
    }

    #[test]
    fn a_tick_outside_a_sleeps_range_saturates() {
        let mut p = Paths::under(Path::new("/r"));
        assert_eq!(p.tick(), Duration::from_secs(5));
        p.tick_s = -1.0;
        assert_eq!(p.tick(), Duration::ZERO);
        p.tick_s = f64::INFINITY;
        assert_eq!(p.tick(), Duration::MAX);
        p.tick_s = f64::NAN;
        assert_eq!(p.tick(), Duration::ZERO);
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
