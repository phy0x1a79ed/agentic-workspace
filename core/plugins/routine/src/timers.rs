//! systemd user timers that fire routines (routines.md R38).
//!
//! The timer only fires. Its service calls `awm routine trigger`, so the routine service
//! keeps the run record, the one-live-run guard and the watch to a verdict.

use std::path::{Path, PathBuf};
use std::process::Command;

use chrono::{DateTime, NaiveDateTime, Utc};
use serde_json::{json, Value};

pub const DEFAULT_JITTER_S: i64 = 30;

/// Runs `systemctl --user <args>`. Tests pass a recorder.
pub type Systemctl = dyn Fn(&[&str]) -> anyhow::Result<()> + Send + Sync;

pub fn real_systemctl(args: &[&str]) -> anyhow::Result<()> {
    let out = Command::new("systemctl").arg("--user").args(args).output()?;
    if !out.status.success() {
        anyhow::bail!("systemctl --user {}: {}", args.join(" "), String::from_utf8_lossy(&out.stderr).trim());
    }
    Ok(())
}

pub fn unit_dir() -> PathBuf {
    if let Some(d) = std::env::var_os("AWM_ROUTINE_UNIT_DIR").filter(|d| !d.is_empty()) {
        return PathBuf::from(d);
    }
    let base = std::env::var_os("XDG_CONFIG_HOME")
        .filter(|d| !d.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".config"));
    base.join("systemd/user")
}

pub fn unit_name(project: &str, name: &str) -> String {
    let safe: String = format!("{project}-{name}").chars().map(|c| if c.is_ascii_alphanumeric() || "_.-".contains(c) { c } else { '_' }).collect();
    format!("awm-routine-{safe}")
}

/// Python's `shlex.quote`.
pub fn quote(s: &str) -> String {
    if s.is_empty() {
        return "''".into();
    }
    if s.chars().all(|c| c.is_ascii_alphanumeric() || "@%+=:,./-_".contains(c)) {
        return s.into();
    }
    format!("'{}'", s.replace('\'', "'\"'\"'"))
}

pub fn trigger_command(project: &str, name: &str) -> String {
    let awm = std::env::var("AWM_ROUTINE_AWM_BIN").unwrap_or_else(|_| "awm".into());
    format!("{} routine trigger --project {} --name {} --trigger timer", quote(&awm), quote(project), quote(name))
}

/// The (.service, .timer) unit texts for one routine. Every timer is `Persistent`, so a fire
/// missed while the node was down comes at the next boot; `catchup_s` bounds how late.
pub fn render(project: &str, name: &str, on_calendar: &str, jitter_s: i64) -> (String, String) {
    let desc = format!("awm routine {project}/{name}");
    let service = format!(
        "[Unit]\nDescription={desc} (fires a trigger)\n\n[Service]\nType=oneshot\nExecStart=/bin/bash -lc {}\n",
        quote(&trigger_command(project, name))
    );
    let timer = format!(
        "[Unit]\nDescription={desc}\n\n[Timer]\nOnCalendar={on_calendar}\nPersistent=true\nRandomizedDelaySec={jitter_s}\n\n[Install]\nWantedBy=timers.target\n"
    );
    (service, timer)
}

fn analyze(args: &[&str]) -> Option<std::process::Output> {
    Command::new("systemd-analyze").args(args).output().ok()
}

/// Reject a bad OnCalendar spec before it reaches a unit file.
pub fn validate_calendar(spec: &str) -> Result<(), String> {
    if spec.is_empty() || spec.contains('\n') {
        return Err("timer must be one OnCalendar expression".into());
    }
    // No systemd-analyze here: systemd itself rejects it at enable.
    let Some(out) = analyze(&["calendar", spec]) else { return Ok(()) };
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        let err: String = err.trim().chars().take(200).collect();
        return Err(format!("bad OnCalendar '{spec}': {err}"));
    }
    Ok(())
}

/// The first elapse of `spec` at or after `base`, from `systemd-analyze calendar --base-time`.
/// None when systemd cannot say: an old systemd, or a spec with no elapse left.
pub fn next_elapse(spec: &str, base: DateTime<Utc>) -> Option<DateTime<Utc>> {
    let base = format!("{} UTC", base.format("%Y-%m-%d %H:%M:%S"));
    let out = analyze(&["calendar", "--iterations=1", &format!("--base-time={base}"), spec])?;
    if !out.status.success() {
        return None;
    }
    parse_elapse(&String::from_utf8_lossy(&out.stdout))
}

fn parse_elapse(text: &str) -> Option<DateTime<Utc>> {
    let utc = |line: &str| {
        let (_, v) = line.split_once(':')?;
        let v = v.trim().strip_suffix(" UTC")?;
        let v = v.split_once(' ').map(|(_, rest)| rest).unwrap_or(v);
        NaiveDateTime::parse_from_str(v, "%Y-%m-%d %H:%M:%S").ok().map(|n| n.and_utc())
    };
    let lines: Vec<&str> = text.lines().map(str::trim).collect();
    let next = lines.iter().position(|l| l.starts_with("Next elapse:"))?;
    lines.get(next + 1).filter(|l| l.starts_with("(in UTC):")).and_then(|l| utc(l)).or_else(|| utc(lines[next]))
}

pub struct Timers<'a> {
    pub dir: &'a Path,
    pub systemctl: &'a (dyn Fn(&[&str]) -> anyhow::Result<()> + Send + Sync + 'a),
}

impl Timers<'_> {
    pub fn schedule(&self, project: &str, name: &str, on_calendar: &str, jitter_s: i64, catchup_s: Option<i64>) -> anyhow::Result<Value> {
        validate_calendar(on_calendar).map_err(anyhow::Error::msg)?;
        std::fs::create_dir_all(self.dir)?;
        let unit = unit_name(project, name);
        let (service, timer) = render(project, name, on_calendar, jitter_s);
        std::fs::write(self.dir.join(format!("{unit}.service")), service)?;
        std::fs::write(self.dir.join(format!("{unit}.timer")), timer)?;
        (self.systemctl)(&["daemon-reload"])?;
        (self.systemctl)(&["enable", "--now", &format!("{unit}.timer")])?;
        Ok(json!({"unit": format!("{unit}.timer"), "dir": self.dir, "on_calendar": on_calendar, "jitter_s": jitter_s, "catchup_s": catchup_s}))
    }

    pub fn unschedule(&self, project: &str, name: &str) -> anyhow::Result<Value> {
        let unit = unit_name(project, name);
        let timer = self.dir.join(format!("{unit}.timer"));
        let present = timer.exists();
        if present {
            // Already stopped or never loaded: the files still go.
            let _ = (self.systemctl)(&["disable", "--now", &format!("{unit}.timer")]);
        }
        for suffix in [".timer", ".service"] {
            match std::fs::remove_file(self.dir.join(format!("{unit}{suffix}"))) {
                Err(e) if e.kind() != std::io::ErrorKind::NotFound => return Err(e.into()),
                _ => {}
            }
        }
        if present {
            (self.systemctl)(&["daemon-reload"])?;
        }
        Ok(json!({"unit": format!("{unit}.timer"), "removed": present}))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    #[test]
    fn schedule_writes_units_and_enables() {
        let tmp = tempfile::tempdir().unwrap();
        let calls = Mutex::new(Vec::<String>::new());
        let rec = |a: &[&str]| {
            calls.lock().unwrap().push(a.join(" "));
            Ok(())
        };
        let t = Timers { dir: tmp.path(), systemctl: &rec };
        let res = t.schedule("awm", "full-tests", "*-*-* 03:00:00", 45, Some(3600)).unwrap();
        let unit = "awm-routine-awm-full-tests";
        assert_eq!(res["unit"], format!("{unit}.timer"));
        let timer = std::fs::read_to_string(tmp.path().join(format!("{unit}.timer"))).unwrap();
        let service = std::fs::read_to_string(tmp.path().join(format!("{unit}.service"))).unwrap();
        assert!(timer.contains("OnCalendar=*-*-* 03:00:00\nPersistent=true\nRandomizedDelaySec=45\n"));
        assert!(service.contains("routine trigger --project awm --name full-tests --trigger timer"));
        assert_eq!(*calls.lock().unwrap(), ["daemon-reload", &format!("enable --now {unit}.timer")]);
        calls.lock().unwrap().clear();
        let res = t.unschedule("awm", "full-tests").unwrap();
        assert_eq!(res["removed"], true);
        assert_eq!(std::fs::read_dir(tmp.path()).unwrap().count(), 0);
        assert_eq!(*calls.lock().unwrap(), [format!("disable --now {unit}.timer"), "daemon-reload".into()]);
    }

    #[test]
    fn the_units_match_python() {
        // The texts Python's timers.render wrote, with its fixed 30 s delay.
        let (service, timer) = render("awm", "nightly", "daily", 30);
        assert_eq!(
            service,
            "[Unit]\nDescription=awm routine awm/nightly (fires a trigger)\n\n[Service]\nType=oneshot\nExecStart=/bin/bash -lc 'awm routine trigger --project awm --name nightly --trigger timer'\n"
        );
        assert_eq!(timer, "[Unit]\nDescription=awm routine awm/nightly\n\n[Timer]\nOnCalendar=daily\nPersistent=true\nRandomizedDelaySec=30\n\n[Install]\nWantedBy=timers.target\n");
    }

    #[test]
    fn reads_the_elapse_in_utc() {
        let text = "  Original form: daily\nNormalized form: *-*-* 00:00:00\n    Next elapse: Sun 2026-11-01 00:00:00 PDT\n       (in UTC): Sun 2026-11-01 07:00:00 UTC\n       From now: 6 days ago\n";
        assert_eq!(parse_elapse(text).unwrap().to_rfc3339(), "2026-11-01T07:00:00+00:00");
        let utc = "    Next elapse: Sun 2026-11-01 07:00:00 UTC\n       From now: 6 days ago\n";
        assert_eq!(parse_elapse(utc).unwrap().to_rfc3339(), "2026-11-01T07:00:00+00:00");
        assert_eq!(parse_elapse("    Next elapse: never\n"), None);
    }

}
