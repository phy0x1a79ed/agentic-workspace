//! Readers of `/proc`, for plugins that judge other processes by their parent, start time,
//! command line or unit. Each answers None (or empty) for a process it cannot read.

use std::collections::{HashMap, HashSet};
use std::path::Path;

fn stat_fields(pid: u32) -> Option<Vec<String>> {
    let raw = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    // comm (field 2) is parenthesized and may hold spaces or parentheses, so split after the last `) `.
    let (_, rest) = raw.rsplit_once(") ")?;
    Some(rest.split_whitespace().map(str::to_string).collect())
}

/// Field 22 of `/proc/<pid>/stat`: the start time in clock ticks since boot, as text.
pub fn start_ticks(pid: u32) -> Option<String> {
    if pid == 0 {
        return None;
    }
    stat_fields(pid)?.get(19).cloned()
}

/// The parent pid.
pub fn ppid(pid: u32) -> Option<u32> {
    stat_fields(pid)?.get(1)?.parse().ok()
}

fn pids() -> Vec<u32> {
    std::fs::read_dir("/proc")
        .map(|rd| rd.flatten().filter_map(|e| e.file_name().to_str()?.parse().ok()).collect())
        .unwrap_or_default()
}

/// Every process's children, keyed by parent pid.
pub fn children() -> HashMap<u32, Vec<u32>> {
    let mut kids: HashMap<u32, Vec<u32>> = HashMap::new();
    for pid in pids() {
        if let Some(parent) = ppid(pid) {
            kids.entry(parent).or_default().push(pid);
        }
    }
    kids
}

/// Whether `target` is `root` or anywhere beneath it in `kids`. A cycle in `kids` ends the walk.
pub fn subtree_contains(root: u32, target: u32, kids: &HashMap<u32, Vec<u32>>) -> bool {
    let (mut stack, mut seen) = (vec![root], HashSet::new());
    while let Some(pid) = stack.pop() {
        if pid == target {
            return true;
        }
        if seen.insert(pid) {
            stack.extend(kids.get(&pid).into_iter().flatten().copied());
        }
    }
    false
}

/// `pid`'s parent, grandparent and so on, up to `max_hops` of them, stopping before pid 1.
pub fn ancestors(pid: u32, max_hops: usize) -> Vec<u32> {
    let mut out = Vec::new();
    let mut cur = pid;
    while out.len() < max_hops {
        match ppid(cur) {
            Some(p) if p > 1 && !out.contains(&p) => {
                out.push(p);
                cur = p;
            }
            _ => break,
        }
    }
    out
}

/// The command line, split on NUL with the empty trailing field dropped, or None when unreadable.
pub fn cmdline(pid: u32) -> Option<Vec<String>> {
    let raw = std::fs::read(format!("/proc/{pid}/cmdline")).ok()?;
    Some(raw.split(|b| *b == 0).filter(|a| !a.is_empty()).map(|a| String::from_utf8_lossy(a).into_owned()).collect())
}

/// The environment, or None when unreadable (another user's process).
pub fn environ(pid: u32) -> Option<HashMap<String, String>> {
    let raw = std::fs::read(format!("/proc/{pid}/environ")).ok()?;
    Some(
        raw.split(|b| *b == 0)
            .filter_map(|kv| {
                let kv = String::from_utf8_lossy(kv);
                let (k, v) = kv.split_once('=')?;
                Some((k.to_string(), v.to_string()))
            })
            .collect(),
    )
}

/// The executable path, without the ` (deleted)` suffix of a replaced binary.
pub fn exe(pid: u32) -> Option<String> {
    let link = std::fs::read_link(format!("/proc/{pid}/exe")).ok()?;
    let text = link.to_string_lossy();
    Some(text.strip_suffix(" (deleted)").unwrap_or(&text).to_string())
}

/// The last component of the process's cgroup v2 path: its systemd unit or scope.
pub fn cgroup_unit(pid: u32) -> Option<String> {
    let text = std::fs::read_to_string(format!("/proc/{pid}/cgroup")).ok()?;
    let line = text.lines().find(|l| l.starts_with("0::"))?;
    line.rsplit('/').next().filter(|u| !u.is_empty()).map(str::to_string)
}

/// The boot time in seconds since the epoch, from `/proc/stat`, or 0 when unreadable.
pub fn boot_time() -> f64 {
    std::fs::read_to_string("/proc/stat")
        .ok()
        .and_then(|t| t.lines().find_map(|l| l.strip_prefix("btime ")?.trim().parse().ok()))
        .unwrap_or(0.0)
}

/// When the process started, in seconds since the epoch.
pub fn started_at_epoch(pid: u32) -> Option<f64> {
    let ticks: f64 = start_ticks(pid)?.parse().ok()?;
    // SAFETY: sysconf takes a plain integer.
    let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
    Some(boot_time() + ticks / if hz > 0 { hz as f64 } else { 100.0 })
}

/// Whether `pid` shares this process's mount namespace. None when either link is unreadable.
pub fn same_mount_ns(pid: u32) -> Option<bool> {
    let theirs = std::fs::read_link(format!("/proc/{pid}/ns/mnt")).ok()?;
    let ours = std::fs::read_link(Path::new("/proc/self/ns/mnt")).ok()?;
    Some(theirs == ours)
}

/// Every pid now in `/proc`, with its command line, skipping the unreadable ones.
pub fn all_cmdlines() -> Vec<(u32, Vec<String>)> {
    pids().into_iter().filter_map(|p| Some((p, cmdline(p)?))).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_this_process() {
        let me = std::process::id();
        assert!(start_ticks(me).is_some());
        // SAFETY: getppid cannot fail.
        let parent = unsafe { libc::getppid() } as u32;
        assert_eq!(ppid(me), Some(parent));
        assert!(cmdline(me).is_some_and(|a| !a.is_empty()));
        assert!(exe(me).is_some());
        assert_eq!(same_mount_ns(me), Some(true));
        assert!(started_at_epoch(me).is_some_and(|t| t > 1.0e9));
        assert!(subtree_contains(parent, me, &children()));
        assert_eq!(ancestors(me, 1), vec![parent].into_iter().filter(|p| *p > 1).collect::<Vec<_>>());
    }

    #[test]
    fn subtree_survives_a_cycle() {
        let kids = HashMap::from([(1, vec![2]), (2, vec![3]), (3, vec![2])]);
        assert!(subtree_contains(1, 3, &kids));
        assert!(!subtree_contains(1, 9, &kids));
    }

    #[test]
    fn unreadable_pid_is_none() {
        assert_eq!(ppid(0), None);
        assert_eq!(start_ticks(0), None);
        assert_eq!(cmdline(u32::MAX), None);
    }
}
