//! The git and DVC wiring that makes a code merge move the data. Everything git-side
//! lands in the common git dir, which every worktree shares and nothing commits.

use std::fs;
use std::io::{self, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

use serde_json::{json, Map, Value};

use crate::git;
use crate::hooks::HookKind;
use crate::provision::Layout;
use crate::scope::config_value;

pub const MERGE_DRIVER_NAME: &str = "dvc";
pub const GIT_ATTRIBUTES_LINE: &str = "*.dvc merge=dvc";
pub const AWM_HOOK_MARKER: &str = "# AWM-managed.";
/// Where the first native wiring keeps the Python hook it replaced, for a rollback.
pub const PY_BACKUP_SUFFIX: &str = ".awm-py.bak";
/// The driver line the Python layer wrote, kept beside the hooks the same way.
pub const DRIVER_BACKUP: &str = "merge-driver.awm-py.bak";
pub const LINK_TYPES: &str = "hardlink,symlink";

fn shell_word(p: &Path) -> String {
    let s = p.to_string_lossy();
    if s.chars().all(|c| c.is_ascii_alphanumeric() || "/._-+=@:".contains(c)) {
        s.into_owned()
    } else {
        format!("'{}'", s.replace('\'', "'\\''"))
    }
}

/// The hook file: a shim that hands git's arguments to `<bin> hook <name>`.
pub fn hook_body(kind: HookKind, bin: &Path) -> String {
    format!("#!/bin/sh\n{AWM_HOOK_MARKER} {}\nexec {} hook {} \"$@\"\n", kind.purpose(), shell_word(bin), kind.name())
}

pub fn driver_command(bin: &Path) -> String {
    format!("{} merge-driver --ancestor %O --our %A --their %B", shell_word(bin))
}

/// Point a worktree at its cache through the untracked `.dvc/config.local`, with an
/// absolute path. A tracked relative path resolves elsewhere in a non-scope checkout and
/// silently starts a second cache there. A `type` already set locally survives.
pub fn ensure_cache_config(repo: &Path, layout: &Layout) -> io::Result<()> {
    let dvc_dir = repo.join(".dvc");
    fs::create_dir_all(&dvc_dir)?;
    let cache = layout.cache_dir(Some(repo));
    fs::create_dir_all(&cache)?;
    let local = dvc_dir.join("config.local");
    let prior = fs::read_to_string(&local).ok();
    let mut body = format!(
        "# AWM-managed, untracked. Absolute on purpose: a tracked relative path\n\
         # would resolve differently in a non-scope checkout and silently start a\n\
         # second cache there.\n\
         [cache]\n    dir = {}\n",
        cache.display()
    );
    if let Some(link) = prior.as_deref().and_then(|t| config_value(t, "cache", "type")) {
        body.push_str(&format!("    type = {link}\n"));
    }
    if let Some(site) = layout.site_cache_dir() {
        body.push_str(&format!("[core]\n    site_cache_dir = {}\n", site.display()));
    }
    if prior.as_deref() != Some(body.as_str()) {
        fs::write(&local, &body)?;
    }
    // The link type is portable, so it belongs in the tracked config every checkout reads.
    let tracked = dvc_dir.join("config");
    let text = fs::read_to_string(&tracked).unwrap_or_default();
    if config_value(&text, "cache", "type").as_deref() != Some(LINK_TYPES) {
        fs::write(&tracked, set_config_value(&text, "cache", "type", &format!("\"{LINK_TYPES}\"")))?;
    }
    Ok(())
}

/// Set `key = value` in `[section]` of a DVC config text, keeping every other line.
pub fn set_config_value(text: &str, section: &str, key: &str, value: &str) -> String {
    let mut lines: Vec<String> = text.lines().map(str::to_string).collect();
    let mut current = None::<String>;
    let mut section_end = None;
    for (i, raw) in lines.clone().iter().enumerate() {
        let line = raw.trim();
        if line.starts_with('[') && line.ends_with(']') {
            current = Some(line[1..line.len() - 1].trim().trim_matches('"').to_string());
            if current.as_deref() == Some(section) {
                section_end = Some(i + 1);
            }
            continue;
        }
        if current.as_deref() == Some(section) {
            if line.split_once('=').is_some_and(|(k, _)| k.trim() == key) {
                lines[i] = format!("    {key} = {value}");
                return lines.join("\n") + "\n";
            }
            if !line.is_empty() {
                section_end = Some(i + 1);
            }
        }
    }
    match section_end {
        Some(at) => lines.insert(at, format!("    {key} = {value}")),
        None => {
            lines.push(format!("[{section}]"));
            lines.push(format!("    {key} = {value}"));
        }
    }
    lines.join("\n") + "\n"
}

fn write_hook(path: &Path, body: &str) -> io::Result<()> {
    fs::write(path, body)?;
    let mode = fs::metadata(path)?.permissions().mode();
    fs::set_permissions(path, fs::Permissions::from_mode(mode | 0o111))
}

/// Install the merge driver, the attribute that selects it, and the three hooks.
///
/// A hook this layer did not write is never overwritten: the result names the conflict.
/// The first native wiring copies the Python hook it replaces to `<hook>.awm-py.bak`,
/// and the Python driver line to `hooks/merge-driver.awm-py.bak`.
pub fn ensure_repo_wiring(repo: &Path, bin: &Path) -> Value {
    let Some(common) = git::common_dir(repo) else {
        return json!({"result": "error", "detail": format!("{} is not a git worktree", repo.display())});
    };
    match wire(repo, &common, bin) {
        Ok(actions) => json!({"result": "ok", "actions": actions, "git_common_dir": common.to_string_lossy()}),
        Err(e) => json!({"result": "error", "detail": e.to_string()}),
    }
}

fn wire(repo: &Path, common: &Path, bin: &Path) -> io::Result<Map<String, Value>> {
    let mut actions = Map::new();
    let hooks = common.join("hooks");
    fs::create_dir_all(&hooks)?;

    let key = format!("merge.{MERGE_DRIVER_NAME}.driver");
    let want = driver_command(bin);
    let have = git::out(repo, &["config", "--get", &key]).unwrap_or_default();
    if have != want {
        let backup = hooks.join(DRIVER_BACKUP);
        if have.contains("git-hook merge-driver") && !backup.exists() {
            fs::write(&backup, format!("{have}\n"))?;
        }
        git::run(repo, &["config", &format!("merge.{MERGE_DRIVER_NAME}.name"), "DVC merge driver"])?;
        git::run(repo, &["config", &key, &want])?;
        actions.insert("merge_driver".into(), "installed".into());
    }

    let info = common.join("info");
    fs::create_dir_all(&info)?;
    let attrs = info.join("attributes");
    let prior = fs::read_to_string(&attrs).unwrap_or_default();
    if !prior.lines().any(|l| l == GIT_ATTRIBUTES_LINE) {
        let mut f = fs::OpenOptions::new().create(true).append(true).open(&attrs)?;
        if !prior.is_empty() && !prior.ends_with('\n') {
            f.write_all(b"\n")?;
        }
        f.write_all(format!("{GIT_ATTRIBUTES_LINE}\n").as_bytes())?;
        actions.insert("attributes".into(), "installed".into());
    }

    for kind in HookKind::ALL {
        let hook = hooks.join(kind.name());
        let body = hook_body(kind, bin);
        if let Ok(existing) = fs::read_to_string(&hook) {
            if existing == body {
                continue;
            }
            if !existing.contains(AWM_HOOK_MARKER) {
                actions.insert(format!("{}_hook", kind.name()), "conflict:foreign-hook-left-in-place".into());
                continue;
            }
            let backup = hooks.join(format!("{}{PY_BACKUP_SUFFIX}", kind.name()));
            if existing.contains("\nDVC=") && !backup.exists() {
                fs::write(&backup, &existing)?;
                fs::set_permissions(&backup, fs::Permissions::from_mode(0o644))?;
            }
        }
        write_hook(&hook, &body)?;
        actions.insert(format!("{}_hook", kind.name()), "installed".into());
    }
    Ok(actions)
}
