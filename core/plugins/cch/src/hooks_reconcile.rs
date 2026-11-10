//! Keeps the awm hook entries in Claude Code's `settings.json` pointing at this binary: replaces
//! the Python hooks' entries in place, adds the missing ones, and retires the old scripts.
//!
//! Replace, never add beside: a running session may keep the hook set it read at start, and one
//! that sees both entries fires each hook twice. A double subagent-cap reservation denies Agent
//! calls early, and a double plan-mode walk presses Shift+Tab past bypass.

use std::io;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use chrono::{DateTime, Local, Utc};
use regex::Regex;
use serde::Serialize;
use serde_json::{json, Map, Value};
use tokio::io::AsyncWriteExt;

use crate::paths::Paths;
use crate::settings_guard::SETTINGS_LOCK;

/// One awm hook: its `cch hook` name, the Python script it replaces, and its settings entries
/// as `(event, matcher, timeout in seconds)`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Hook {
    pub name: &'static str,
    pub legacy_file: &'static str,
    pub entries: &'static [(&'static str, Option<&'static str>, u64)],
}

/// The hooks, with the events, matchers and timeouts `deploy-hooks.sh` installed.
pub const HOOKS: [Hook; 5] = [
    Hook { name: "name", legacy_file: "awm-cx-name-hook.py", entries: &[("UserPromptSubmit", None, 5)] },
    Hook {
        name: "plan-mode",
        legacy_file: "awm-reflection-mode.py",
        entries: &[("PostToolUse", Some("ExitPlanMode"), 20)],
    },
    Hook { name: "brief", legacy_file: "awm-brief-hook.py", entries: &[("SessionStart", None, 6)] },
    Hook {
        name: "subagent-cap",
        legacy_file: "awm-subagent-cap.py",
        entries: &[("PreToolUse", Some("Agent|Task"), 5), ("SubagentStart", None, 5), ("SubagentStop", None, 5)],
    },
    Hook {
        name: "report",
        legacy_file: LEGACY_REPORT,
        entries: &[
            ("PostToolUse", Some("ExitPlanMode|TaskCreate|TaskUpdate|TodoWrite"), 15),
            ("UserPromptSubmit", None, 15),
            ("SessionStart", Some("clear"), 15),
        ],
    },
];

/// The hook script operations shipped. A running session keeps the hook set it read at start,
/// so the script execs `cch hook report` until it is retired.
const LEGACY_REPORT: &str = "awm-operations-hook.sh";

/// How many of its own `settings.json` backups the reconcile keeps.
pub const KEPT_BACKUPS: usize = 5;
/// The old scripts stay this long after their entries were replaced, for a session that still
/// runs the old command.
pub const RETIRE_AFTER: chrono::Duration = chrono::Duration::days(7);
/// The file in cch's service dir that records when the reconcile first replaced the old entries.
pub const REPLACED_FILE: &str = "hooks-replaced";
/// The suffix that marks a `backups/settings.json.<ts>` as one this reconcile wrote, so the
/// rotation never deletes a backup another tool made.
pub const BACKUP_SUFFIX: &str = ".cch";

/// What one reconcile did. It never fails: an error stops it and lands in `error`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct Reconciled {
    /// `AWM_CCH_HOOKS=off`: nothing was read or written.
    pub off: bool,
    /// One `<event> <hook>: added|replaced` per entry that changed.
    pub changes: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub backup: Option<PathBuf>,
    /// The old scripts removed from `hooks/`.
    pub removed: Vec<PathBuf>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// The settings command for hook `name`: `"<AWM_DIR>/bin/cch" hook <name> 2>/dev/null || true`.
pub fn command(paths: &Paths, name: &str) -> String {
    let bin = paths.awm_dir.join("bin").join("cch").to_string_lossy().into_owned();
    let mut quoted = String::with_capacity(bin.len());
    for c in bin.chars() {
        if matches!(c, '"' | '\\' | '$' | '`') {
            quoted.push('\\');
        }
        quoted.push(c);
    }
    format!("\"{quoted}\" hook {name} 2>/dev/null || true")
}

fn entry(paths: &Paths, hook: &Hook, matcher: Option<&str>, timeout: u64) -> Value {
    let mut out = Map::new();
    if let Some(m) = matcher {
        out.insert("matcher".into(), json!(m));
    }
    out.insert("hooks".into(), json!([{"type": "command", "command": command(paths, hook.name), "timeout": timeout}]));
    Value::Object(out)
}

/// Whether `command` runs `hook`: the old script by its file name, or any earlier `cch hook <name>`.
struct Matcher {
    legacy: &'static str,
    ported: Regex,
}

impl Matcher {
    fn new(hook: &Hook) -> Self {
        let pattern = format!(r#"(?:^|[/\s"'-])cch["']?\s+hook\s+{}(?:["'\s;|&)]|$)"#, regex::escape(hook.name));
        Self { legacy: hook.legacy_file, ported: Regex::new(&pattern).expect("a fixed pattern compiles") }
    }

    fn ours(&self, hook: &Value) -> bool {
        hook.get("command").and_then(Value::as_str).is_some_and(|c| c.contains(self.legacy) || self.ported.is_match(c))
    }

    fn legacy(&self, hook: &Value) -> bool {
        hook.get("command").and_then(Value::as_str).is_some_and(|c| c.contains(self.legacy))
    }
}

/// What placing one wanted entry into an event's list did.
#[derive(Debug, PartialEq, Eq)]
enum Placed {
    Unchanged,
    Added,
    Replaced { legacy: bool },
}

/// Puts `want` where the first entry running this hook was, drops this hook from every other
/// entry, and leaves each other hook in its entry. Appends `want` when no entry ran the hook.
fn place(list: &mut Vec<Value>, want: Value, matcher: &Matcher) -> Placed {
    let before = list.clone();
    let mut out = Vec::with_capacity(list.len() + 1);
    let (mut placed, mut legacy) = (false, false);
    for item in list.drain(..) {
        let Some(hooks) = item.get("hooks").and_then(Value::as_array).filter(|h| h.iter().any(|h| matcher.ours(h))) else {
            out.push(item);
            continue;
        };
        legacy |= hooks.iter().any(|h| matcher.legacy(h));
        if !placed {
            out.push(want.clone());
            placed = true;
        }
        let rest: Vec<Value> = hooks.iter().filter(|h| !matcher.ours(h)).cloned().collect();
        if !rest.is_empty() {
            let mut kept = item.clone();
            kept["hooks"] = Value::Array(rest);
            out.push(kept);
        }
    }
    if !placed {
        out.push(want);
    }
    *list = out;
    match (*list == before, placed) {
        (true, _) => Placed::Unchanged,
        (false, true) => Placed::Replaced { legacy },
        (false, false) => Placed::Added,
    }
}

/// Brings every awm entry in `doc` to today's command. Answers the changes and whether an old
/// script's entry was replaced.
fn patch(paths: &Paths, doc: &mut Map<String, Value>) -> Result<(Vec<String>, bool), String> {
    let hooks = doc.entry("hooks").or_insert_with(|| json!({}));
    let Value::Object(hooks) = hooks else { return Err("settings.json `hooks` is not an object".into()) };
    let (mut changes, mut replaced_legacy) = (Vec::new(), false);
    for hook in &HOOKS {
        let matcher = Matcher::new(hook);
        for &(event, m, timeout) in hook.entries {
            let Value::Array(list) = hooks.entry(event).or_insert_with(|| json!([])) else {
                return Err(format!("settings.json `hooks.{event}` is not a list"));
            };
            match place(list, entry(paths, hook, m, timeout), &matcher) {
                Placed::Unchanged => {}
                Placed::Added => changes.push(format!("{event} {}: added", hook.name)),
                Placed::Replaced { legacy } => {
                    replaced_legacy |= legacy;
                    changes.push(format!("{event} {}: replaced", hook.name));
                }
            }
        }
    }
    Ok((changes, replaced_legacy))
}

/// Python's `str.splitlines` boundaries, as `settings_guard` splits on them.
const LINE_BREAKS: [char; 10] = ['\n', '\r', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e', '\u{85}', '\u{2028}', '\u{2029}'];

/// The file's own indent unit: a tab, or as many spaces as the first indented line has. A copy of
/// `settings_guard`'s private helper.
fn indent(text: &str) -> String {
    for line in text.split(LINE_BREAKS).skip(1) {
        let stripped = line.trim_start_matches([' ', '\t']);
        if !stripped.is_empty() && stripped.len() != line.len() {
            if line.starts_with('\t') {
                return "\t".into();
            }
            return " ".repeat(line.len() - stripped.len());
        }
    }
    "  ".into()
}

/// `doc` in `text`'s indent and trailing newline. A copy of `settings_guard`'s private `dumps`.
fn render(doc: &Map<String, Value>, text: &str) -> String {
    let mut out = Vec::new();
    let unit = indent(text);
    let fmt = serde_json::ser::PrettyFormatter::with_indent(unit.as_bytes());
    let mut ser = serde_json::Serializer::with_formatter(&mut out, fmt);
    doc.serialize(&mut ser).expect("a JSON map always serializes");
    let mut out = String::from_utf8(out).expect("serde_json writes UTF-8");
    if text.is_empty() || text.ends_with('\n') {
        out.push('\n');
    }
    out
}

/// Writes `body` over `path` through a temp file renamed into place, with `mode`.
async fn replace(path: &Path, body: &str, mode: u32) -> io::Result<()> {
    let dir = path.parent().unwrap_or(Path::new("."));
    let tmp = dir.join(format!(".settings-hooks-{}", uuid::Uuid::new_v4().simple()));
    let result = async {
        let mut fh = tokio::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(&tmp).await?;
        fh.write_all(body.as_bytes()).await?;
        fh.flush().await?;
        drop(fh);
        tokio::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(mode)).await?;
        tokio::fs::rename(&tmp, path).await
    }
    .await;
    if result.is_err() {
        let _ = tokio::fs::remove_file(&tmp).await;
    }
    result
}

/// Copies `settings` to `backups/settings.json.<ts>.cch`, then drops all but the newest
/// [`KEPT_BACKUPS`] of those. Answers the new backup.
async fn back_up(settings: &Path, backups: &Path, now: DateTime<Utc>) -> io::Result<PathBuf> {
    tokio::fs::create_dir_all(backups).await?;
    let stamp = now.with_timezone(&Local).format("%Y%m%d-%H%M%S");
    let to = backups.join(format!("settings.json.{stamp}{BACKUP_SUFFIX}"));
    tokio::fs::copy(settings, &to).await?;
    let mut ours = Vec::new();
    let mut dir = tokio::fs::read_dir(backups).await?;
    while let Some(item) = dir.next_entry().await? {
        let name = item.file_name().to_string_lossy().into_owned();
        if name.starts_with("settings.json.") && name.ends_with(BACKUP_SUFFIX) {
            ours.push(name);
        }
    }
    ours.sort();
    for name in &ours[..ours.len().saturating_sub(KEPT_BACKUPS)] {
        tokio::fs::remove_file(backups.join(name)).await?;
    }
    Ok(to)
}

async fn replaced_at(marker: &Path) -> Option<DateTime<Utc>> {
    let text = tokio::fs::read_to_string(marker).await.ok()?;
    DateTime::parse_from_rfc3339(text.trim()).ok().map(|t| t.with_timezone(&Utc))
}

/// Points what still names the report hook at this binary: the old script, for sessions that
/// read their hooks before this start, and the settings a restricted session starts with.
async fn keep_report_paths(paths: &Paths) -> io::Result<()> {
    let hook = HOOKS.iter().find(|h| h.name == "report").expect("the report hook is listed");
    let script = paths.home.root.join("hooks").join(LEGACY_REPORT);
    if tokio::fs::symlink_metadata(&script).await.is_ok() {
        tokio::fs::write(&script, format!("#!/bin/sh\nexec {}\n", command(paths, hook.name))).await?;
    }
    let mut events = Map::new();
    for &(event, matcher, timeout) in hook.entries {
        events.insert(event.to_string(), json!([entry(paths, hook, matcher, timeout)]));
    }
    let doc = json!({"awaySummaryEnabled": false, "hooks": events});
    if let Some(dir) = paths.restricted_settings.parent() {
        tokio::fs::create_dir_all(dir).await?;
    }
    tokio::fs::write(&paths.restricted_settings, format!("{}\n", serde_json::to_string_pretty(&doc).unwrap_or_default())).await
}

/// Records the first replacement, then removes each old script no entry names once
/// [`RETIRE_AFTER`] has passed since. Touches no other file in `hooks/`.
async fn retire(paths: &Paths, doc: &Map<String, Value>, replaced_legacy: bool, now: DateTime<Utc>) -> io::Result<Vec<PathBuf>> {
    let dir = paths.home.root.join("hooks");
    let named = doc.get("hooks").map(Value::to_string).unwrap_or_default();
    let mut present = Vec::new();
    for hook in &HOOKS {
        let path = dir.join(hook.legacy_file);
        if tokio::fs::symlink_metadata(&path).await.is_ok() {
            present.push((path, named.contains(hook.legacy_file)));
        }
    }
    let marker = paths.service_dir.join(REPLACED_FILE);
    let Some(first) = replaced_at(&marker).await else {
        if replaced_legacy || !present.is_empty() {
            tokio::fs::create_dir_all(&paths.service_dir).await?;
            tokio::fs::write(&marker, format!("{}\n", now.to_rfc3339())).await?;
        }
        return Ok(Vec::new());
    };
    let mut removed = Vec::new();
    if now - first >= RETIRE_AFTER {
        for (path, still_named) in present {
            if !still_named {
                tokio::fs::remove_file(&path).await?;
                removed.push(path);
            }
        }
    }
    Ok(removed)
}

/// Brings the awm hook entries in `<claude home>/settings.json` to `cch hook <name>`, once at
/// start, unless `paths.hooks_reconcile` is off. Holds [`SETTINGS_LOCK`] across the whole
/// read-modify-write, and writes at most once, after a backup, only when something changed.
pub async fn reconcile(paths: &Paths) -> Reconciled {
    reconcile_at(paths, Utc::now()).await
}

/// [`reconcile`] at the time `now`.
pub async fn reconcile_at(paths: &Paths, now: DateTime<Utc>) -> Reconciled {
    let mut out = Reconciled { off: !paths.hooks_reconcile, ..Reconciled::default() };
    if out.off {
        return out;
    }
    let _held = SETTINGS_LOCK.lock().await;
    if let Err(e) = run(paths, now, &mut out).await {
        out.error = Some(e);
    }
    match &out.error {
        Some(e) => tracing::warn!("cch: the hooks reconcile stopped: {e}"),
        None if !out.changes.is_empty() => tracing::info!("cch: hooks reconciled: {}", out.changes.join(", ")),
        None => {}
    }
    out
}

async fn run(paths: &Paths, now: DateTime<Utc>, out: &mut Reconciled) -> Result<(), String> {
    let settings = paths.home.settings();
    let fail = |e: io::Error| format!("{}: {e}", settings.display());
    let (text, mode) = match tokio::fs::read_to_string(&settings).await {
        Ok(text) => {
            let mode = tokio::fs::metadata(&settings).await.map_err(fail)?.permissions().mode() & 0o7777;
            (text, Some(mode))
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound && paths.home.root.is_dir() => (String::new(), None),
        Err(e) => return Err(fail(e)),
    };
    let mut doc = match text.trim() {
        "" if mode.is_none() => Map::new(),
        _ => match serde_json::from_str(&text) {
            Ok(Value::Object(doc)) => doc,
            _ => return Err(format!("{} is not a JSON object", settings.display())),
        },
    };
    let (changes, replaced_legacy) = patch(paths, &mut doc)?;
    if !changes.is_empty() {
        if mode.is_some() {
            out.backup = Some(back_up(&settings, &paths.home.root.join("backups"), now).await.map_err(fail)?);
        }
        replace(&settings, &render(&doc, &text), mode.unwrap_or(0o600)).await.map_err(fail)?;
        out.changes = changes;
    }
    keep_report_paths(paths).await.map_err(|e| format!("the report hook's files: {e}"))?;
    out.removed = retire(paths, &doc, replaced_legacy, now).await.map_err(|e| format!("retiring the old hooks: {e}"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn setup() -> (tempfile::TempDir, Paths) {
        let tmp = tempfile::tempdir().unwrap();
        let paths = Paths::under(tmp.path());
        std::fs::create_dir_all(&paths.home.root).unwrap();
        (tmp, paths)
    }

    fn legacy(file: &str) -> String {
        format!("python3 -S /home/user/.claude/hooks/{file} 2>/dev/null || true")
    }

    fn legacy_entry(file: &str, matcher: Option<&str>, timeout: u64) -> Value {
        let mut e = json!({"hooks": [{"type": "command", "command": legacy(file), "timeout": timeout}]});
        if let Some(m) = matcher {
            e = json!({"matcher": m, "hooks": e["hooks"]});
        }
        e
    }

    #[tokio::test]
    async fn the_report_hook_keeps_old_sessions_and_restricted_ones_working() {
        let (_t, paths) = setup();
        let hooks = paths.home.root.join("hooks");
        std::fs::create_dir_all(&hooks).unwrap();
        std::fs::write(hooks.join(LEGACY_REPORT), "#!/bin/sh\nexec old\n").unwrap();
        write(&paths, &json!({"hooks": {"UserPromptSubmit": [legacy_entry(LEGACY_REPORT, None, 15)]}}));
        reconcile_at(&paths, Utc::now()).await;
        assert!(std::fs::read_to_string(hooks.join(LEGACY_REPORT)).unwrap().contains("hook report"), "the old path now runs cch");
        let restricted: Value = serde_json::from_str(&std::fs::read_to_string(&paths.restricted_settings).unwrap()).unwrap();
        for event in ["PostToolUse", "UserPromptSubmit", "SessionStart"] {
            assert!(restricted["hooks"][event][0]["hooks"][0]["command"].as_str().unwrap().contains("hook report"), "{event}");
        }
    }

    fn foreign(command: &str) -> Value {
        json!({"hooks": [{"type": "command", "command": command}]})
    }

    /// The settings `deploy-hooks.sh` left on a node, plus one foreign hook per event.
    fn deployed() -> Value {
        json!({
            "theme": "dark",
            "hooks": {
                "UserPromptSubmit": [legacy_entry("awm-cx-name-hook.py", None, 5)],
                "PostToolUse": [foreign("/usr/local/bin/notify"), legacy_entry("awm-reflection-mode.py", Some("ExitPlanMode"), 20)],
                "SessionStart": [legacy_entry("awm-brief-hook.py", None, 6), foreign("echo started")],
                "PreToolUse": [legacy_entry("awm-subagent-cap.py", Some("Agent|Task"), 5), legacy_entry("awm-operations-hook.py", None, 10)],
                "SubagentStart": [legacy_entry("awm-subagent-cap.py", None, 5)],
                "SubagentStop": [legacy_entry("awm-subagent-cap.py", None, 5)],
            },
            "model": "opus",
        })
    }

    fn write(paths: &Paths, doc: &Value) {
        std::fs::write(paths.home.settings(), serde_json::to_string_pretty(doc).unwrap() + "\n").unwrap();
    }

    fn read(paths: &Paths) -> Value {
        serde_json::from_str(&std::fs::read_to_string(paths.home.settings()).unwrap()).unwrap()
    }

    fn commands(list: &Value) -> Vec<String> {
        list.as_array()
            .unwrap()
            .iter()
            .flat_map(|e| e["hooks"].as_array().unwrap().iter().map(|h| h["command"].as_str().unwrap().to_string()))
            .collect()
    }

    fn day(n: i64) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-11-01T12:00:00Z").unwrap().with_timezone(&Utc) + chrono::Duration::days(n)
    }

    #[tokio::test]
    async fn the_old_entries_are_replaced_in_place_never_added_beside() {
        let (_tmp, paths) = setup();
        write(&paths, &deployed());
        let done = reconcile_at(&paths, day(0)).await;
        assert_eq!(done.error, None);
        let (added, replaced): (Vec<_>, Vec<_>) = done.changes.iter().partition(|c| c.contains(" report: added"));
        assert_eq!((added.len(), replaced.len()), (3, 6), "{:?}", done.changes);
        assert!(replaced.iter().all(|c| c.ends_with("replaced")));
        let doc = read(&paths);
        let hooks = &doc["hooks"];
        assert_eq!(commands(&hooks["SessionStart"]), [command(&paths, "brief"), "echo started".into(), command(&paths, "report")]);
        assert_eq!(commands(&hooks["PostToolUse"]), ["/usr/local/bin/notify".to_string(), command(&paths, "plan-mode"), command(&paths, "report")]);
        assert_eq!(hooks["PostToolUse"][1]["matcher"], "ExitPlanMode");
        assert_eq!(hooks["PostToolUse"][1]["hooks"][0]["timeout"], 20);
        assert_eq!(hooks["PreToolUse"][0]["matcher"], "Agent|Task");
        for (event, entries) in [("SubagentStart", 1), ("SubagentStop", 1), ("UserPromptSubmit", 2)] {
            assert_eq!(hooks[event].as_array().unwrap().len(), entries, "{event}");
        }
        let text = doc.to_string();
        for hook in &HOOKS {
            assert!(!text.contains(hook.legacy_file), "{} still named", hook.legacy_file);
        }
        assert_eq!(command(&paths, "brief"), format!("\"{}/.awm/bin/cch\" hook brief 2>/dev/null || true", _tmp.path().display()));

        let again = reconcile_at(&paths, day(0)).await;
        assert_eq!(again, Reconciled::default(), "a second run is a no-op");
        assert_eq!(read(&paths), doc);
    }

    #[tokio::test]
    async fn an_absent_settings_file_is_created_with_the_hooks() {
        let (_tmp, paths) = setup();
        let done = reconcile_at(&paths, day(0)).await;
        assert_eq!(done.error, None);
        assert_eq!(done.backup, None);
        assert_eq!(read(&paths)["hooks"]["SessionStart"], json!([entry(&paths, &HOOKS[2], None, 6), entry(&paths, &HOOKS[4], Some("clear"), 15)]));
    }

    #[tokio::test]
    async fn a_foreign_hook_and_every_other_key_are_untouched() {
        let (_tmp, paths) = setup();
        write(&paths, &deployed());
        reconcile_at(&paths, day(0)).await;
        let (before, after) = (deployed(), read(&paths));
        assert_eq!(after["hooks"]["PreToolUse"][1], before["hooks"]["PreToolUse"][1], "the operations hook rides deploy-hooks.sh");
        assert_eq!(after["hooks"]["SessionStart"][1], before["hooks"]["SessionStart"][1]);
        assert_eq!(after["hooks"]["PostToolUse"][0], before["hooks"]["PostToolUse"][0]);
        let keys = |v: &Value| v.as_object().unwrap().keys().cloned().collect::<Vec<_>>();
        assert_eq!(keys(&after), keys(&before));
        assert_eq!(after["model"], "opus");
    }

    #[tokio::test]
    async fn the_off_switch_reads_and_writes_nothing() {
        let (_tmp, mut paths) = setup();
        paths.hooks_reconcile = false;
        write(&paths, &deployed());
        let hooks = paths.home.root.join("hooks");
        std::fs::create_dir_all(&hooks).unwrap();
        std::fs::write(hooks.join("awm-brief-hook.py"), "#").unwrap();
        let before = std::fs::read(paths.home.settings()).unwrap();
        let done = reconcile_at(&paths, day(30)).await;
        assert_eq!(done, Reconciled { off: true, ..Reconciled::default() });
        assert_eq!(std::fs::read(paths.home.settings()).unwrap(), before);
        assert!(hooks.join("awm-brief-hook.py").exists());
        assert!(!paths.home.root.join("backups").exists());
    }

    #[tokio::test]
    async fn a_hooks_value_that_is_not_an_object_is_left_alone() {
        let (_tmp, paths) = setup();
        write(&paths, &json!({"hooks": "nope"}));
        let before = std::fs::read(paths.home.settings()).unwrap();
        let done = reconcile_at(&paths, day(0)).await;
        assert!(done.error.unwrap().contains("not an object"));
        assert_eq!(std::fs::read(paths.home.settings()).unwrap(), before);
    }
}
