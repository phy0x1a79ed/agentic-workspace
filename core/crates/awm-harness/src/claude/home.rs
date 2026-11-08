//! Where Claude Code keeps its files. One value resolves every path, so no reader hard-codes one.

use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClaudeHome {
    /// `$CLAUDE_CONFIG_DIR`, else `~/.claude`.
    pub root: PathBuf,
    /// `~/.claude.json`, where Claude records per-directory trust.
    pub trust_file: PathBuf,
}

fn home_dir() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
}

impl ClaudeHome {
    /// The home this process's Claude Code uses: `$CLAUDE_CONFIG_DIR`, else `~/.claude`.
    pub fn from_env() -> Self {
        let root = std::env::var_os("CLAUDE_CONFIG_DIR")
            .filter(|v| !v.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| home_dir().join(".claude"));
        Self { root, trust_file: home_dir().join(".claude.json") }
    }

    pub fn at(root: impl Into<PathBuf>, trust_file: impl Into<PathBuf>) -> Self {
        Self { root: root.into(), trust_file: trust_file.into() }
    }

    pub fn daemon_dir(&self) -> PathBuf {
        self.root.join("daemon")
    }

    /// `daemon/roster.json`: the daemon's workers.
    pub fn roster(&self) -> PathBuf {
        self.daemon_dir().join("roster.json")
    }

    /// `jobs/`: one directory per background job, holding `state.json`.
    pub fn jobs(&self) -> PathBuf {
        self.root.join("jobs")
    }

    pub fn job_state(&self, job: &str) -> PathBuf {
        self.jobs().join(job).join("state.json")
    }

    /// `jobs/pins.json`: the jobs the daemon's idle retirement passes over.
    pub fn pins(&self) -> PathBuf {
        self.jobs().join("pins.json")
    }

    /// `sessions/`: one `<pid>.json` per running session.
    pub fn sessions(&self) -> PathBuf {
        self.root.join("sessions")
    }

    pub fn session_record(&self, pid: u32) -> PathBuf {
        self.sessions().join(format!("{pid}.json"))
    }

    /// `projects/`: the transcripts, one folder per working directory.
    pub fn projects(&self) -> PathBuf {
        self.root.join("projects")
    }

    pub fn daemon_log(&self) -> PathBuf {
        self.root.join("daemon.log")
    }

    pub fn settings(&self) -> PathBuf {
        self.root.join("settings.json")
    }

    /// The daemon's control socket: `/tmp/cc-daemon-<uid>/<sha256(root)[:8]>/control.sock`.
    pub fn control_socket(&self) -> PathBuf {
        let digest = Sha256::digest(self.root.to_string_lossy().as_bytes());
        let hex: String = digest.iter().take(4).map(|b| format!("{b:02x}")).collect();
        // SAFETY: getuid cannot fail.
        let uid = unsafe { libc::getuid() };
        Path::new("/tmp").join(format!("cc-daemon-{uid}")).join(hex).join("control.sock")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paths_hang_off_root() {
        let h = ClaudeHome::at("/x/.claude", "/x/.claude.json");
        assert_eq!(h.roster(), Path::new("/x/.claude/daemon/roster.json"));
        assert_eq!(h.job_state("ab12cd34"), Path::new("/x/.claude/jobs/ab12cd34/state.json"));
        assert_eq!(h.session_record(7), Path::new("/x/.claude/sessions/7.json"));
        assert_eq!(h.pins(), Path::new("/x/.claude/jobs/pins.json"));
        let sock = h.control_socket();
        assert!(sock.ends_with("control.sock"));
        assert_eq!(sock.parent().unwrap().file_name().unwrap().len(), 8);
    }
}
