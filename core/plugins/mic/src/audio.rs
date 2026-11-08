//! PulseAudio upkeep: the daemon and the `virtmic` null-sink.
//!
//! Every recorder on the host (`arecord`, `/voice`, mic's own `pacat`) reads the sink's
//! `.monitor`, so a missing sink records silence with no error anywhere. Three layers keep it:
//!
//! 1. `daemon.conf` sets `exit-idle-time = -1`, so the socket-activated daemon stops exiting
//!    20 s after its last client and taking the runtime-loaded sink with it.
//! 2. `default.pa` declares the sink, so a daemon restart rebuilds it unaided.
//! 3. [`Audio::ensure_once`], run by the health loop, before each stream and by `mic_ensure_sink`,
//!    catches what the first two miss.
//!
//! Every subprocess goes through a [`Runner`], so the tests model a PulseAudio world with no
//! sound server.

use std::future::Future;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tokio::time::Instant;

pub const DEFAULT_SINK: &str = "virtmic";

/// The markers fence each stanza so a rerun appends nothing. Hosts already carry these exact
/// strings: `remote-audio bridge` from the first mic, the other two from the virtmic service.
pub const ASOUNDRC_MARKER: &str = "remote-audio bridge";
pub const DAEMON_MARKER: &str = "awm virtmic: keep PulseAudio resident";
pub const DEFAULT_PA_MARKER: &str = "awm virtmic: declare the virtual microphone sink";

/// Bounds each subprocess, so a wedged PulseAudio cannot hang a pass.
const COMMAND_TIMEOUT: Duration = Duration::from_secs(10);
const UNITS: [&str; 2] = ["pulseaudio.service", "pulseaudio.socket"];

/// A finished subprocess. A missing program is code 127 and a timeout 124.
#[derive(Debug, Clone, Default)]
pub struct Out {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

pub type BoxFut<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Runs one command. [`System`] runs it for real.
pub trait Runner: Send + Sync {
    fn run<'a>(&'a self, cmd: &'a [String]) -> BoxFut<'a, Out>;
}

/// The real runner: no stdin, output captured, killed after [`COMMAND_TIMEOUT`].
pub struct System;

impl Runner for System {
    fn run<'a>(&'a self, cmd: &'a [String]) -> BoxFut<'a, Out> {
        Box::pin(async move {
            let mut c = tokio::process::Command::new(&cmd[0]);
            c.args(&cmd[1..]).stdin(std::process::Stdio::null()).kill_on_drop(true);
            match tokio::time::timeout(COMMAND_TIMEOUT, c.output()).await {
                Ok(Ok(o)) => Out {
                    code: o.status.code().unwrap_or(-1),
                    stdout: String::from_utf8_lossy(&o.stdout).into_owned(),
                    stderr: String::from_utf8_lossy(&o.stderr).into_owned(),
                },
                Ok(Err(e)) if e.kind() == std::io::ErrorKind::NotFound => {
                    tracing::debug!("not installed: {}", cmd[0]);
                    Out { code: 127, stdout: String::new(), stderr: "not found".into() }
                }
                Ok(Err(e)) => Out { code: 126, stdout: String::new(), stderr: e.to_string() },
                Err(_) => {
                    tracing::debug!("timed out: {}", cmd.join(" "));
                    Out { code: 124, stdout: String::new(), stderr: "timeout".into() }
                }
            }
        })
    }
}

/// Point `XDG_RUNTIME_DIR` at the user's runtime dir when it is unset. The gateway's systemd
/// environment has none, and without it libpulse cannot find `$XDG_RUNTIME_DIR/pulse/native`.
/// Call it before any thread starts: every child inherits it.
pub fn ensure_runtime_env() {
    if std::env::var_os("XDG_RUNTIME_DIR").is_none_or(|v| v.is_empty()) {
        std::env::set_var("XDG_RUNTIME_DIR", default_runtime_dir());
    }
}

pub fn default_runtime_dir() -> String {
    // SAFETY: getuid cannot fail.
    format!("/run/user/{}", unsafe { libc::getuid() })
}

/// The rolling provisioning record that `mic_status` reports.
#[derive(Debug, Clone, Default)]
pub struct Record {
    pub last_ok_ts: Option<f64>,
    pub last_heal_ts: Option<f64>,
    pub last_error: Option<String>,
    pub passes: u64,
    pub heals: u64,
}

/// A pass that could not repair the audio path, as a verb error's class and message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Failure {
    pub class: &'static str,
    pub message: String,
}

impl std::fmt::Display for Failure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl From<std::io::Error> for Failure {
    fn from(e: std::io::Error) -> Self {
        Failure { class: "OSError", message: e.to_string() }
    }
}

pub fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// The PulseAudio and ALSA upkeep of one host.
pub struct Audio {
    runner: Arc<dyn Runner>,
    home: PathBuf,
    /// Where `/etc/pulse` is looked for: `/` on a host, a temp dir in tests.
    system_root: PathBuf,
    record: Mutex<Record>,
    /// One pass at a time: the health loop, a stream start and the verb never interleave.
    pass: tokio::sync::Mutex<()>,
}

impl Audio {
    pub fn new(runner: Arc<dyn Runner>, home: PathBuf, system_root: PathBuf) -> Self {
        Self { runner, home, system_root, record: Mutex::new(Record::default()), pass: tokio::sync::Mutex::new(()) }
    }

    /// This host's PulseAudio, in `$HOME`.
    pub fn system() -> Self {
        #[allow(deprecated)]
        let home = std::env::home_dir().unwrap_or_else(|| PathBuf::from("/"));
        Self::new(Arc::new(System), home, PathBuf::from("/"))
    }

    pub fn record(&self) -> Record {
        self.record.lock().unwrap_or_else(|p| p.into_inner()).clone()
    }

    fn update(&self, f: impl FnOnce(&mut Record)) {
        f(&mut self.record.lock().unwrap_or_else(|p| p.into_inner()))
    }

    async fn run(&self, cmd: &[&str]) -> Out {
        let cmd: Vec<String> = cmd.iter().map(|s| s.to_string()).collect();
        self.runner.run(&cmd).await
    }

    pub async fn pactl_ok(&self) -> bool {
        self.run(&["pactl", "info"]).await.code == 0
    }

    /// The default capture source, or None when PulseAudio does not answer.
    pub async fn default_source(&self) -> Option<String> {
        let r = self.run(&["pactl", "get-default-source"]).await;
        (r.code == 0).then(|| r.stdout.trim().to_string()).filter(|s| !s.is_empty())
    }

    pub async fn sink_names(&self) -> Vec<String> {
        let r = self.run(&["pactl", "list", "short", "sinks"]).await;
        if r.code != 0 {
            return vec![];
        }
        r.stdout.lines().filter_map(|l| l.split('\t').nth(1)).map(str::to_string).collect()
    }

    async fn unit_prop(&self, unit: &str, prop: &str) -> String {
        let r = self.run(&["systemctl", "--user", "show", "-p", prop, "--value", unit]).await;
        if r.code == 0 {
            r.stdout.trim().to_string()
        } else {
            String::new()
        }
    }

    /// Whether a systemd user unit owns PulseAudio: whether it exists, not whether it is up. A
    /// stopped or failed unit still needs `systemctl`, because a bare `pulseaudio --start` would
    /// race the socket unit for the socket and lose.
    pub async fn systemd_managed(&self) -> bool {
        self.unit_prop("pulseaudio.service", "LoadState").await == "loaded"
    }

    pub async fn unit_state(&self) -> String {
        self.unit_prop("pulseaudio.service", "ActiveState").await
    }

    async fn units_failed(&self) -> bool {
        for u in UNITS {
            if self.unit_prop(u, "ActiveState").await == "failed" {
                return true;
            }
        }
        false
    }

    /// systemd parks both units in `failed` after a rapid restart loop, and socket activation
    /// stops firing. A standalone daemon squatting the socket causes that loop, so kill it first,
    /// or the reset only re-enters the loop.
    async fn clear_failed_units(&self) {
        tracing::debug!("pulseaudio units are failed; clearing and restarting");
        self.run(&["pkill", "-x", "pulseaudio"]).await;
        tokio::time::sleep(Duration::from_millis(500)).await;
        let mut cmd = vec!["systemctl", "--user", "reset-failed"];
        cmd.extend(UNITS);
        self.run(&cmd).await;
    }

    fn pulse_dir(&self) -> PathBuf {
        self.home.join(".config/pulse")
    }

    /// Append a marker-fenced stanza unless the file already holds the marker. True when it wrote.
    ///
    /// **CAUTION** A user-level PulseAudio file replaces the system one rather than layering on
    /// it, so a file created here first includes `system_include` when that exists.
    fn append_stanza(&self, path: &Path, marker: &str, body: &str, system_include: Option<&str>) -> std::io::Result<bool> {
        let existing = match std::fs::read(path) {
            Ok(b) => Some(b),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
            Err(e) => return Err(e),
        };
        if let Some(b) = &existing {
            if contains(b, marker.as_bytes()) {
                return Ok(false);
            }
        }
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let mut out = String::new();
        let include = system_include.filter(|p| self.system_root.join(p.trim_start_matches('/')).exists());
        match (&existing, include) {
            (None, Some(inc)) => out.push_str(&format!(
                "# Created by the awm mic service. A user config file replaces\n\
                 # the system one outright, so pull the system defaults back in\n\
                 # first and override below.\n\
                 .include {inc}\n"
            )),
            (Some(b), _) if !b.is_empty() && !b.ends_with(b"\n") => out.push('\n'),
            _ => {}
        }
        out.push_str(&format!("\n### {marker}\n{}\n", body.trim_end()));
        std::fs::OpenOptions::new().create(true).append(true).open(path)?.write_all(out.as_bytes())?;
        tracing::info!("wrote {} ({marker})", path.display());
        Ok(true)
    }

    /// Layer 1: no idle exit. The running daemon reads it only after a restart.
    pub fn ensure_daemon_conf(&self) -> std::io::Result<bool> {
        self.append_stanza(
            &self.pulse_dir().join("daemon.conf"),
            DAEMON_MARKER,
            "# Without this PulseAudio exits ~20s after the last client\n\
             # disconnects, destroying the runtime-loaded virtmic sink.\n\
             exit-idle-time = -1\n",
            Some("/etc/pulse/daemon.conf"),
        )
    }

    /// Layer 2: the daemon's startup script declares the sink.
    pub fn ensure_default_pa(&self, sink: &str) -> std::io::Result<bool> {
        self.append_stanza(
            &self.pulse_dir().join("default.pa"),
            DEFAULT_PA_MARKER,
            &format!(
                "# Rebuilds the virtual microphone on every daemon start, so a\n\
                 # restart can't leave recorders reading a dead default source.\n\
                 load-module module-null-sink sink_name={sink} sink_properties=device.description=VirtualMic\n\
                 set-default-source {sink}.monitor\n"
            ),
            Some("/etc/pulse/default.pa"),
        )
    }

    /// ALSA's default device through PulseAudio, so `arecord` hears the sink.
    pub fn ensure_asoundrc(&self) -> std::io::Result<bool> {
        self.append_stanza(&self.home.join(".asoundrc"), ASOUNDRC_MARKER, "pcm.!default { type pulse }\nctl.!default { type pulse }\n", None)
    }

    /// Write all three files. True when a PulseAudio file changed, so the daemon must restart.
    pub fn ensure_config(&self, sink: &str) -> std::io::Result<bool> {
        let daemon = self.ensure_daemon_conf()?;
        let default = self.ensure_default_pa(sink)?;
        self.ensure_asoundrc()?;
        Ok(daemon || default)
    }

    async fn wait_reachable(&self) -> bool {
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            if self.pactl_ok().await {
                return true;
            }
            tokio::time::sleep(Duration::from_millis(200)).await;
        }
        self.pactl_ok().await
    }

    /// Start PulseAudio unless it answers. True once it does.
    pub async fn start_pulse(&self) -> bool {
        if self.pactl_ok().await {
            return true;
        }
        tracing::debug!("pulseaudio unreachable; starting it");
        if self.systemd_managed().await {
            if self.units_failed().await {
                self.clear_failed_units().await;
            }
            self.run(&["systemctl", "--user", "start", "pulseaudio.service"]).await;
        } else {
            self.run(&["pulseaudio", "--start", "--exit-idle-time=-1"]).await;
        }
        self.wait_reachable().await
    }

    /// Restart PulseAudio so new config takes effect, once per config change.
    pub async fn restart_pulse(&self) -> bool {
        tracing::info!("restarting pulseaudio to apply new config");
        if self.systemd_managed().await {
            if self.units_failed().await {
                self.clear_failed_units().await;
            }
            self.run(&["systemctl", "--user", "restart", "pulseaudio.service"]).await;
        } else {
            self.run(&["pulseaudio", "-k"]).await;
            tokio::time::sleep(Duration::from_millis(300)).await;
            self.run(&["pulseaudio", "--start", "--exit-idle-time=-1"]).await;
        }
        self.wait_reachable().await
    }

    fn fail(&self, message: String) -> Failure {
        self.update(|r| r.last_error = Some(message.clone()));
        Failure { class: "RuntimeError", message }
    }

    /// One reconcile pass: config, daemon, sink, default source. A healthy host sees no change
    /// and `healed: false`. Fails only when PulseAudio cannot be reached or the sink not loaded.
    pub async fn ensure_once(&self, sink: &str) -> Result<Value, Failure> {
        let _one = self.pass.lock().await;
        self.update(|r| r.passes += 1);
        let mut repairs: Vec<&str> = vec![];

        if self.ensure_config(sink)? {
            repairs.push("config");
            if !self.restart_pulse().await {
                return Err(self.fail("pulseaudio unreachable after config restart".into()));
            }
        }

        if !self.pactl_ok().await {
            repairs.push("daemon");
            if !self.start_pulse().await {
                return Err(self.fail("pulseaudio unreachable".into()));
            }
        }

        if !self.sink_names().await.iter().any(|n| n == sink) {
            tracing::debug!("creating null sink {sink:?}");
            let name = format!("sink_name={sink}");
            let r = self.run(&["pactl", "load-module", "module-null-sink", &name, "sink_properties=device.description=VirtualMic"]).await;
            if r.code != 0 {
                return Err(self.fail(format!("load-module failed: {}", r.stderr.trim())));
            }
            repairs.push("sink");
        }

        let want = format!("{sink}.monitor");
        if self.default_source().await.as_deref() != Some(want.as_str()) {
            self.run(&["pactl", "set-default-source", &want]).await;
            repairs.push("default_source");
        }

        // A suspended monitor drops the first second of audio.
        let mods = self.run(&["pactl", "list", "short", "modules"]).await.stdout;
        if let Some(id) = mods.lines().find(|l| l.contains("module-suspend-on-idle") && l.contains('\t')).and_then(|l| l.split('\t').next()) {
            self.run(&["pactl", "unload-module", id]).await;
            repairs.push("suspend-on-idle");
        }

        let t = now();
        self.update(|r| {
            r.last_ok_ts = Some(t);
            r.last_error = None;
            if !repairs.is_empty() {
                r.last_heal_ts = Some(t);
                r.heals += 1;
            }
        });
        if !repairs.is_empty() {
            tracing::info!("virtmic healed: {}", repairs.join(", "));
        }
        Ok(json!({
            "sink": sink,
            "sink_present": true,
            "default_source": self.default_source().await,
            "healed": !repairs.is_empty(),
            "repairs": repairs,
        }))
    }

    /// A read-only snapshot. It never repairs.
    pub async fn status(&self, sink: &str) -> Value {
        let reachable = self.pactl_ok().await;
        let names = if reachable { self.sink_names().await } else { vec![] };
        let src = if reachable { self.default_source().await } else { None };
        let has = |path: PathBuf, marker: &str| std::fs::read(path).map(|b| contains(&b, marker.as_bytes())).unwrap_or(false);
        let unit = self.unit_state().await;
        let rec = self.record();
        let src_ok = src.as_deref() == Some(format!("{sink}.monitor").as_str());
        json!({
            "sink": sink,
            "pulseaudio_reachable": reachable,
            "systemd_managed": self.systemd_managed().await,
            "pulseaudio_unit_state": (!unit.is_empty()).then_some(unit),
            "sink_present": names.iter().any(|n| n == sink),
            "default_source": src,
            "default_source_ok": src_ok,
            "idle_timeout_disabled": has(self.pulse_dir().join("daemon.conf"), DAEMON_MARKER),
            "sink_declared_in_default_pa": has(self.pulse_dir().join("default.pa"), DEFAULT_PA_MARKER),
            "asoundrc_bridged": has(self.home.join(".asoundrc"), ASOUNDRC_MARKER),
            "last_ok_ts": rec.last_ok_ts,
            "last_heal_ts": rec.last_heal_ts,
            "last_error": rec.last_error,
            "passes": rec.passes,
            "heals": rec.heals,
        })
    }
}

fn contains(hay: &[u8], needle: &[u8]) -> bool {
    needle.is_empty() || hay.windows(needle.len()).any(|w| w == needle)
}
