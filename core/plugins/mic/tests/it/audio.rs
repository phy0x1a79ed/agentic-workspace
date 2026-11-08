//! The PulseAudio upkeep against a fake PulseAudio world, ported from `test_audio.py`.
//! The clock is paused, so the reachability waits cost nothing.

use std::path::Path;
use std::sync::{Arc, Mutex};

use awm_plugin_mic::audio::{self, Audio, BoxFut, Out, Runner};

struct World {
    running: bool,
    sinks: Vec<String>,
    default_source: String,
    systemd: bool,
    modules: String,
    starts_ok: bool,
    unit_state: String,
    calls: Vec<Vec<String>>,
}

#[derive(Clone)]
struct FakePulse(Arc<Mutex<World>>);

impl FakePulse {
    fn new() -> Self {
        Self(Arc::new(Mutex::new(World {
            running: true,
            sinks: vec!["auto_null".into()],
            default_source: "auto_null.monitor".into(),
            systemd: true,
            modules: "0\tmodule-suspend-on-idle\t\n".into(),
            starts_ok: true,
            unit_state: "active".into(),
            calls: vec![],
        })))
    }

    fn with<T>(&self, f: impl FnOnce(&mut World) -> T) -> T {
        f(&mut self.0.lock().unwrap())
    }

    fn calls(&self) -> Vec<Vec<String>> {
        self.with(|w| w.calls.clone())
    }

    fn answer(w: &mut World, cmd: &[String]) -> Out {
        let ok = |out: &str| Out { code: 0, stdout: out.into(), stderr: String::new() };
        let rc = |code: i32| Out { code, ..Out::default() };
        let c: Vec<&str> = cmd.iter().map(String::as_str).collect();
        match c[0] {
            "systemctl" => {
                if !w.systemd {
                    return rc(1);
                }
                match c[2] {
                    "show" => match c[4] {
                        "LoadState" => ok("loaded\n"),
                        "ActiveState" => ok(&format!("{}\n", w.unit_state)),
                        _ => ok("\n"),
                    },
                    "reset-failed" => {
                        w.unit_state = "inactive".into();
                        ok("")
                    }
                    "start" | "restart" => {
                        if w.unit_state == "failed" {
                            return rc(1);
                        }
                        w.running = w.starts_ok;
                        w.unit_state = if w.running { "active" } else { "failed" }.into();
                        // A real restart reloads default.pa, which rebuilds the sink.
                        if w.running && !w.sinks.iter().any(|s| s == "virtmic") {
                            w.sinks.push("virtmic".into());
                        }
                        ok("")
                    }
                    _ => ok(""),
                }
            }
            "pkill" => {
                w.running = false;
                ok("")
            }
            "pulseaudio" => {
                w.running = if c.contains(&"-k") { false } else { w.starts_ok };
                ok("")
            }
            "pactl" if !w.running => rc(1),
            "pactl" => match c[1] {
                "info" => ok("Server String: /run/user/1000/pulse/native\n"),
                "get-default-source" => ok(&format!("{}\n", w.default_source)),
                "set-default-source" => {
                    w.default_source = c[2].into();
                    ok("")
                }
                "list" => match *c.last().unwrap() {
                    "sinks" => ok(&w.sinks.iter().enumerate().map(|(i, n)| format!("{i}\t{n}\tmodule-null-sink.c\ts16le\tIDLE\n")).collect::<String>()),
                    "modules" => ok(&w.modules.clone()),
                    _ => ok(""),
                },
                "load-module" => {
                    if let Some(n) = c.iter().find_map(|a| a.strip_prefix("sink_name=")) {
                        w.sinks.push(n.into());
                    }
                    ok("42\n")
                }
                "unload-module" => {
                    w.modules.clear();
                    ok("")
                }
                _ => ok(""),
            },
            _ => rc(127),
        }
    }
}

impl Runner for FakePulse {
    fn run<'a>(&'a self, cmd: &'a [String]) -> BoxFut<'a, Out> {
        let out = self.with(|w| {
            w.calls.push(cmd.to_vec());
            Self::answer(w, cmd)
        });
        Box::pin(async move { out })
    }
}

struct Host {
    home: tempfile::TempDir,
    etc: tempfile::TempDir,
    pulse: FakePulse,
    audio: Audio,
}

fn host() -> Host {
    let (home, etc, pulse) = (tempfile::tempdir().unwrap(), tempfile::tempdir().unwrap(), FakePulse::new());
    let audio = Audio::new(Arc::new(pulse.clone()), home.path().into(), etc.path().into());
    Host { home, etc, pulse, audio }
}

fn read(home: &Path, rel: &str) -> String {
    std::fs::read_to_string(home.join(rel)).unwrap()
}

fn has(calls: &[Vec<String>], prefix: &[&str]) -> bool {
    calls.iter().any(|c| c.len() >= prefix.len() && c.iter().zip(prefix).all(|(a, b)| a == b))
}

// -- the config layers ------------------------------------------------------

#[test]
fn config_writes_are_idempotent() {
    type Writer = fn(&Audio) -> std::io::Result<bool>;
    let writers: [(&str, Writer); 3] = [
        (".config/pulse/daemon.conf", |a| a.ensure_daemon_conf()),
        (".config/pulse/default.pa", |a| a.ensure_default_pa("virtmic")),
        (".asoundrc", |a| a.ensure_asoundrc()),
    ];
    for (rel, write) in writers {
        let h = host();
        assert!(write(&h.audio).unwrap(), "{rel}");
        let first = read(h.home.path(), rel);
        assert!(!write(&h.audio).unwrap());
        assert!(!write(&h.audio).unwrap());
        assert_eq!(read(h.home.path(), rel), first, "{rel}");
    }
}

/// Hosts that ran the first mic already carry this marker. A changed one appends a duplicate.
#[test]
fn asoundrc_marker_is_byte_identical_to_the_mic_original() {
    let h = host();
    assert_eq!(audio::ASOUNDRC_MARKER, "remote-audio bridge");
    std::fs::write(h.home.path().join(".asoundrc"), "\n# remote-audio bridge: route ALSA default through PulseAudio\npcm.!default { type pulse }\n").unwrap();
    assert!(!h.audio.ensure_asoundrc().unwrap());
}

// -- the reconcile pass -----------------------------------------------------

#[tokio::test(start_paused = true)]
async fn ensure_once_provisions_from_scratch() {
    let h = host();
    let res = h.audio.ensure_once("virtmic").await.unwrap();
    assert_eq!(res["sink_present"], true);
    assert_eq!(res["default_source"], "virtmic.monitor");
    assert_eq!(res["healed"], true);
    assert!(res["repairs"].as_array().unwrap().contains(&"config".into()));
}

#[tokio::test(start_paused = true)]
async fn ensure_once_is_idempotent_on_a_healthy_host() {
    let h = host();
    h.audio.ensure_once("virtmic").await.unwrap();
    let res = h.audio.ensure_once("virtmic").await.unwrap();
    assert_eq!(res["healed"], false);
    assert_eq!(res["repairs"], serde_json::json!([]));
    assert_eq!(res["default_source"], "virtmic.monitor");
}

#[tokio::test(start_paused = true)]
async fn config_change_restarts_the_daemon_once() {
    let h = host();
    h.audio.ensure_once("virtmic").await.unwrap();
    let restarts = h.pulse.calls().iter().filter(|c| c.starts_with(&["systemctl".into(), "--user".into(), "restart".into()])).count();
    assert_eq!(restarts, 1, "a config write restarts PulseAudio exactly once");
    h.pulse.with(|w| w.calls.clear());
    h.audio.ensure_once("virtmic").await.unwrap();
    assert!(!h.pulse.calls().iter().any(|c| c.iter().any(|a| a == "restart")), "a steady pass restarts nothing");
}

/// PulseAudio dies and takes the sink with it; the next pass brings both back.
#[tokio::test(start_paused = true)]
async fn sink_is_rebuilt_after_pulseaudio_is_killed() {
    let h = host();
    h.audio.ensure_once("virtmic").await.unwrap();
    h.pulse.with(|w| {
        w.running = false;
        w.sinks.clear();
        w.default_source = "auto_null.monitor".into();
    });
    let res = h.audio.ensure_once("virtmic").await.unwrap();
    assert_eq!(res["sink_present"], true);
    assert_eq!(res["default_source"], "virtmic.monitor");
    assert!(res["repairs"].as_array().unwrap().contains(&"daemon".into()));
}

#[tokio::test(start_paused = true)]
async fn default_source_restored_when_something_else_steals_it() {
    let h = host();
    h.audio.ensure_once("virtmic").await.unwrap();
    h.pulse.with(|w| w.default_source = "alsa_input.pci-0000_00_1f.3.analog-stereo".into());
    let res = h.audio.ensure_once("virtmic").await.unwrap();
    assert_eq!(res["default_source"], "virtmic.monitor");
    assert!(res["repairs"].as_array().unwrap().contains(&"default_source".into()));
}

/// Never report ready while the audio path is dead, and always record why.
#[tokio::test(start_paused = true)]
async fn unreachable_pulseaudio_fails_rather_than_reporting_healthy() {
    let h = host();
    h.pulse.with(|w| {
        w.running = false;
        w.starts_ok = false;
    });
    let err = h.audio.ensure_once("virtmic").await.unwrap_err();
    assert!(err.message.contains("unreachable"), "{err}");
    assert_eq!(err.class, "RuntimeError");
    assert!(h.audio.record().last_error.unwrap_or_default().contains("unreachable"));
}

#[tokio::test(start_paused = true)]
async fn heal_timestamp_only_advances_on_an_actual_repair() {
    let h = host();
    h.audio.ensure_once("virtmic").await.unwrap();
    let healed_at = h.audio.record().last_heal_ts.unwrap();
    h.audio.ensure_once("virtmic").await.unwrap();
    let r = h.audio.record();
    assert_eq!(r.last_heal_ts, Some(healed_at));
    assert!(r.last_ok_ts.unwrap() >= healed_at);
    h.pulse.with(|w| w.sinks.retain(|s| s != "virtmic"));
    h.audio.ensure_once("virtmic").await.unwrap();
    let r = h.audio.record();
    assert!(r.last_heal_ts.unwrap() >= healed_at);
    assert_eq!(r.heals, 2);
}

/// systemd parks both units in `failed` after a restart loop; mic clears that itself.
#[tokio::test(start_paused = true)]
async fn recovers_from_failed_systemd_units() {
    let h = host();
    h.audio.ensure_once("virtmic").await.unwrap();
    h.pulse.with(|w| {
        w.running = false;
        w.unit_state = "failed".into();
        w.sinks.clear();
    });
    let res = h.audio.ensure_once("virtmic").await.unwrap();
    assert!(has(&h.pulse.calls(), &["systemctl", "--user", "reset-failed"]));
    assert_eq!(res["sink_present"], true);
    assert_eq!(res["default_source"], "virtmic.monitor");
}

#[tokio::test(start_paused = true)]
async fn non_systemd_host_uses_the_bare_daemon() {
    let h = host();
    h.pulse.with(|w| {
        w.systemd = false;
        w.running = false;
    });
    h.audio.ensure_once("virtmic").await.unwrap();
    let calls = h.pulse.calls();
    assert!(calls.iter().any(|c| c[0] == "pulseaudio"));
    assert!(!calls.iter().any(|c| c[0] == "systemctl" && (c[2] == "start" || c[2] == "restart")));
}

// -- the status surface -----------------------------------------------------

#[tokio::test(start_paused = true)]
async fn status_reports_every_acceptance_field() {
    let h = host();
    h.audio.ensure_once("virtmic").await.unwrap();
    let st = h.audio.status("virtmic").await;
    let keys: Vec<&str> = st.as_object().unwrap().keys().map(String::as_str).collect();
    assert_eq!(
        keys,
        [
            "sink", "pulseaudio_reachable", "systemd_managed", "pulseaudio_unit_state", "sink_present", "default_source", "default_source_ok",
            "idle_timeout_disabled", "sink_declared_in_default_pa", "asoundrc_bridged", "last_ok_ts", "last_heal_ts", "last_error", "passes", "heals"
        ]
    );
    for k in ["pulseaudio_reachable", "sink_present", "default_source_ok", "idle_timeout_disabled", "sink_declared_in_default_pa", "asoundrc_bridged"] {
        assert_eq!(st[k], true, "{k}");
    }
    assert_eq!(st["pulseaudio_unit_state"], "active");
}

/// A status call that healed would mask the breakage it exists to expose.
#[tokio::test(start_paused = true)]
async fn status_never_repairs() {
    let h = host();
    h.pulse.with(|w| w.sinks.clear());
    let st = h.audio.status("virtmic").await;
    assert_eq!(st["sink_present"], false);
    assert_eq!(st["default_source_ok"], false);
    assert!(h.pulse.with(|w| w.sinks.is_empty()));
    assert!(!h.pulse.calls().iter().any(|c| c.iter().any(|a| a == "load-module")));
}

/// Python's bytes for each starting state (fixtures/capture.py), written twice each.
#[test]
fn every_starting_state_matches_python() {
    let cases: serde_json::Value = serde_json::from_str(include_str!("fixtures/stanzas.json")).unwrap();
    for case in cases.as_array().unwrap() {
        let h = host();
        if case["include"] == true {
            std::fs::create_dir_all(h.etc.path().join("etc/pulse")).unwrap();
            for f in ["daemon.conf", "default.pa"] {
                std::fs::write(h.etc.path().join("etc/pulse").join(f), "").unwrap();
            }
        }
        let files = case["files"].as_object().unwrap();
        if let Some(start) = case["start"].as_str() {
            for rel in files.keys() {
                let p = h.home.path().join(rel);
                std::fs::create_dir_all(p.parent().unwrap()).unwrap();
                std::fs::write(p, start).unwrap();
            }
        }
        let sink = case["sink"].as_str().unwrap();
        let changed: Vec<bool> = (0..2).map(|_| h.audio.ensure_config(sink).unwrap()).collect();
        assert_eq!(serde_json::json!(changed), case["changed"], "{case}");
        for (rel, want) in files {
            assert_eq!(read(h.home.path(), rel), want.as_str().unwrap(), "{rel} in {case}");
        }
    }
}
