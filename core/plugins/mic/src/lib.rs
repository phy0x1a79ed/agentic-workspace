//! mic, the remote microphone: a phone's browser audio into the PulseAudio `virtmic` sink,
//! which mic keeps alive (`install.md` R134 folded the virtmic service in).

pub mod audio;
pub mod bridge;

use std::sync::Arc;
use std::time::Duration;

use awm_plugin_sdk::{Call, Context, Service, Session, VerbError};
use serde_json::{json, Value};

use crate::audio::Failure;
use crate::bridge::Mic;

const TOOL: &str = "mic";

impl From<Failure> for VerbError {
    fn from(f: Failure) -> Self {
        VerbError::new(f.class, f.message)
    }
}

/// mic's verbs and its `stream` session on `svc`.
pub fn service(svc: Service, mic: Arc<Mic>) -> Service {
    let (m1, m2, m3) = (mic.clone(), mic.clone(), mic);
    svc.api("emitters", json!([]))
        .verb(
            TOOL,
            "status",
            "Report the active stream count, last PCM timestamp, and the audio path's health: PulseAudio reachability, whether the virtmic sink is loaded, the default capture source, which durability layers are installed, and the last heal time.",
            json!([]),
            move |_call: Call| {
                let mic = m1.clone();
                async move { Ok::<Value, VerbError>(status(&mic).await) }
            },
        )
        .verb(
            TOOL,
            "ensure_sink",
            "Run one reconcile pass now: ensure the PulseAudio config, daemon, virtmic null-sink and default capture source are all in place. Idempotent.",
            json!([]),
            move |_call: Call| {
                let mic = m2.clone();
                async move { Ok::<Value, VerbError>(mic.audio.ensure_once(&mic.sink).await?) }
            },
        )
        .session("stream", move |s: Session| {
            let mic = m3.clone();
            async move {
                let bridge = s.open_bridge().await?;
                mic.run_session(&s.init, bridge).await;
                Ok(())
            }
        })
}

/// `mic_status`: the stream state, then the sink's health.
pub async fn status(mic: &Mic) -> Value {
    let mut st = mic.status();
    let sink = mic.audio.status(&mic.sink).await;
    st["sink_present"] = sink["sink_present"].clone();
    st["default_source"] = sink["default_source"].clone();
    st["audio_path_ok"] = json!(sink["sink_present"] == json!(true) && sink["default_source_ok"] == json!(true));
    let mut out = serde_json::Map::new();
    for (k, v) in st.as_object().into_iter().flatten() {
        out.insert(k.clone(), v.clone());
        if k == "stream_channels" {
            out.insert("audio".into(), sink.clone());
        }
    }
    Value::Object(out)
}

/// What the health loop last saw, so it logs each change once.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Seen {
    Healthy,
    Failing(String),
}

/// Provision once, then reconcile every `interval`. A host with no sound server keeps mic
/// loaded: `mic_status` says why the audio path is down, and the log says it once.
pub async fn health(ctx: Context, mic: Arc<Mic>, interval: Duration) {
    let mut seen = match mic.audio.ensure_once(&mic.sink).await {
        Ok(r) => {
            let repairs: Vec<&str> = r["repairs"].as_array().into_iter().flatten().filter_map(Value::as_str).collect();
            let repairs = if repairs.is_empty() { "none".to_string() } else { repairs.join(", ") };
            tracing::info!("mic ready: sink={} default_source={} repairs={repairs}", mic.sink, r["default_source"].as_str().unwrap_or("none"));
            Seen::Healthy
        }
        Err(e) => {
            tracing::warn!("mic: initial sink provisioning failed: {e}; the loop will retry");
            Seen::Failing(e.message)
        }
    };
    loop {
        tokio::time::sleep(interval).await;
        let now = match mic.audio.ensure_once(&mic.sink).await {
            Ok(r) => {
                if r["healed"] == json!(true) {
                    ctx.usage("sink-repair", true, 0);
                }
                Seen::Healthy
            }
            Err(e) => Seen::Failing(e.message),
        };
        if now != seen {
            match &now {
                Seen::Healthy => tracing::info!("mic: the audio path is healthy again"),
                Seen::Failing(why) => tracing::warn!("mic: sink health pass failed: {why}"),
            }
            seen = now;
        }
    }
}
