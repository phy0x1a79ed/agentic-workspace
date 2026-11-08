//! Where tether's pieces live on a host, and which role this host plays.
//!
//! The unit names and the control socket derive from the state path, so they must be the
//! ones the Python adapter derived: a moved path orphans the running binary and its sessions.

use std::path::{Path, PathBuf};

use sha1::{Digest, Sha1};

/// The tether release this plugin is pinned to.
pub fn pinned() -> &'static str {
    include_str!("../../RELEASE").trim()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    /// A member node: `tether-operator`, which mints machine sessions.
    Operator,
    /// The public host: `tether-relay`, which carries tether sessions and rooms.
    Relay,
}

impl Role {
    pub fn as_str(self) -> &'static str {
        match self {
            Role::Operator => "operator",
            Role::Relay => "relay",
        }
    }
}

#[derive(Debug, Clone)]
pub struct Paths {
    pub role: Role,
    pub state_dir: PathBuf,
    pub bin_dir: PathBuf,
    /// `AWM_TETHER_BIN`: a developer's own build. The fetch then does nothing.
    pub bin_override: Option<PathBuf>,
    pub assets_dir: PathBuf,
}

fn env_path(name: &str) -> Option<PathBuf> {
    std::env::var(name).ok().map(|v| v.trim().to_string()).filter(|v| !v.is_empty()).map(PathBuf::from)
}

impl Paths {
    /// The paths under `<awm_dir>/services/tether`, with the environment's overrides.
    pub fn new(role: Role, awm_dir: &Path) -> Self {
        let state_dir = awm_dir.join("services").join("tether");
        let bin_override = env_path("AWM_TETHER_BIN");
        Self {
            role,
            bin_dir: bin_override.clone().unwrap_or_else(|| state_dir.join("bin")),
            assets_dir: env_path("AWM_TETHER_ASSETS").unwrap_or_else(|| state_dir.join("assets")),
            bin_override,
            state_dir,
        }
    }

    pub fn binary(&self) -> PathBuf {
        self.bin_dir.join(match self.role {
            Role::Operator => "tether-operator",
            Role::Relay => "tether-relay",
        })
    }

    /// The transient user unit the role's binary runs in: `awm-tether-<role>-<sha1(state)[:8]>`.
    pub fn unit(&self) -> String {
        let digest = hex::encode(Sha1::digest(self.state_dir.to_string_lossy().as_bytes()));
        format!("awm-tether-{}-{}", self.role.as_str(), &digest[..8])
    }

    pub fn control_socket(&self) -> PathBuf {
        self.state_dir.join("operator.sock")
    }
    pub fn daemon_log(&self) -> PathBuf {
        self.state_dir.join("daemon.log")
    }
    pub fn launch_file(&self) -> PathBuf {
        self.state_dir.join("daemon.json")
    }
    pub fn env_file(&self) -> PathBuf {
        self.state_dir.join("daemon.env")
    }
    pub fn stream_state(&self) -> PathBuf {
        self.state_dir.join("stream.json")
    }
    pub fn sessions_dir(&self) -> PathBuf {
        self.state_dir.join("sessions")
    }
    pub fn room_state(&self) -> PathBuf {
        self.state_dir.join("rooms")
    }
    pub fn fetch_log(&self) -> PathBuf {
        self.state_dir.join("fetch.log")
    }
    pub fn version_file(&self) -> PathBuf {
        self.bin_dir.join("VERSION")
    }

    /// The tag the last complete fetch stamped beside the binaries.
    pub fn installed(&self) -> Option<String> {
        std::fs::read_to_string(self.version_file()).ok().map(|s| s.trim().to_string()).filter(|s| !s.is_empty())
    }

    /// The release this host is pinned to beside the one it has. An installed tag behind the
    /// pin means the last fetch did not finish, and the fetch log's last line says why.
    pub fn release_report(&self) -> serde_json::Value {
        use serde_json::json;
        if let Some(dir) = &self.bin_override {
            return json!({"pinned": pinned(), "source": format!("AWM_TETHER_BIN={}", dir.display())});
        }
        let installed = self.installed();
        let in_sync = installed.as_deref() == Some(pinned());
        let mut report = json!({"pinned": pinned(), "installed": installed, "in_sync": in_sync});
        if !in_sync {
            if let Some(last) = std::fs::read_to_string(self.fetch_log()).ok().and_then(|t| t.lines().last().map(str::to_string)) {
                report["last_fetch"] = json!(last);
            }
        }
        report
    }
}
