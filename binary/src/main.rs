mod executor;
mod frames;
mod mqtt_relay;
mod signaling;
mod transport;

use anyhow::{Context, Result};
use clap::Parser;
use signaling::{ByeMessage, SignalingConfig};
use std::io::{self, BufRead, Write};
use std::process::ExitCode;
use std::time::Duration;

const VERSION: &str = env!("CARGO_PKG_VERSION");

// Baked-in signaling broker config. URL + USER are operational config that
// rarely change; rotating them requires a rebuild + release. Password stays
// as a runtime flag/env var so it can be rotated without rebuilding.
const BUILTIN_MQTT_URL: &str = "wss://s12a68ff.ala.us-east-1.emqxsl.com:8084/mqtt";
const BUILTIN_MQTT_USER: &str = "awm_probe";

#[derive(Parser, Debug)]
#[command(name = "probe", version = VERSION, about = "ephemeral pair-debug shell")]
struct Args {
    /// Name advertised on the signaling broker (operator uses this to find this probe).
    #[arg(long, short = 'n')]
    name: String,

    /// EMQX broker URL (override compiled-in default)
    #[arg(long, default_value = BUILTIN_MQTT_URL)]
    mqtt_url: String,

    /// MQTT username (override compiled-in default)
    #[arg(long, default_value = BUILTIN_MQTT_USER)]
    mqtt_user: String,

    /// MQTT password (or set EMQX_PASS)
    #[arg(long, env = "EMQX_PASS")]
    mqtt_pass: String,

    /// Operator display name for the consent prompt
    #[arg(long, default_value = "an awm operator")]
    operator: String,

    /// Skip the interactive consent prompt (TESTS ONLY)
    #[arg(long, default_value_t = false)]
    no_consent: bool,

    /// Route data frames through the MQTT broker instead of WebRTC.
    /// Use on UDP-blocked networks (HPC nodes, restrictive corp egress)
    /// where WebRTC cannot establish a data channel. The broker sees all
    /// command stdout/stderr — no P2P privacy. Operator must also pass
    /// --mqtt-relay.
    #[arg(long, default_value_t = false)]
    mqtt_relay: bool,
}

#[tokio::main]
async fn main() -> ExitCode {
    let args = Args::parse();

    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .with_writer(std::io::stderr)
        .init();

    if !args.no_consent {
        match prompt_consent(&args.operator) {
            ConsentResult::Granted => {}
            ConsentResult::Denied => {
                eprintln!("aborted by user.");
                return ExitCode::from(1);
            }
            ConsentResult::ReadError => {
                eprintln!("could not read consent — aborting.");
                return ExitCode::from(2);
            }
        }
    }

    eprintln!("probe v{VERSION} starting (name={})", args.name);
    eprintln!("code={}", args.name);

    match run(args).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("probe: fatal: {e:#}");
            ExitCode::from(2)
        }
    }
}

enum ConsentResult {
    Granted,
    Denied,
    ReadError,
}

fn prompt_consent(operator: &str) -> ConsentResult {
    eprintln!("probe v{VERSION} — vagrant-shell pair-debug binary");
    eprintln!();
    eprintln!("probe is about to expose this shell to {operator}.");
    eprint!("Continue? [y/N] ");
    let _ = io::stderr().flush();
    let mut line = String::new();
    let stdin = io::stdin();
    if stdin.lock().read_line(&mut line).is_err() {
        return ConsentResult::ReadError;
    }
    let answer = line.trim().to_ascii_lowercase();
    if answer == "y" || answer == "yes" {
        ConsentResult::Granted
    } else {
        ConsentResult::Denied
    }
}

async fn run(args: Args) -> Result<()> {
    let client_id = format!("probe-friend-{}-{}", args.name, rand_suffix());
    let cfg = SignalingConfig {
        url: args.mqtt_url,
        user: args.mqtt_user,
        pass: args.mqtt_pass,
        name: args.name.clone(),
        client_id,
    };

    let mut handles = signaling::connect(cfg).await.context("signaling connect failed")?;
    tracing::info!("connected to broker; subscribed");

    enum Handler {
        Webrtc(transport::Transport),
        MqttRelay(mqtt_relay::MqttRelay),
    }

    let handler = if args.mqtt_relay {
        tracing::info!("mqtt-relay mode: data frames will go through the broker");
        Handler::MqttRelay(mqtt_relay::MqttRelay::new(handles.publisher.clone()))
    } else {
        Handler::Webrtc(transport::Transport::new(handles.publisher.clone()))
    };

    // Main event loop: forward signaling events to the active handler.
    // Also listen for Ctrl-C for graceful shutdown.
    let shutdown = tokio::signal::ctrl_c();
    tokio::pin!(shutdown);

    loop {
        tokio::select! {
            evt = handles.events.recv() => {
                match evt {
                    Some(event) => {
                        let res = match &handler {
                            Handler::Webrtc(t) => t.handle_event(event).await,
                            Handler::MqttRelay(r) => r.handle_event(event).await,
                        };
                        if let Err(e) = res {
                            tracing::warn!("handle_event failed: {e:#}");
                        }
                    }
                    None => {
                        tracing::info!("signaling event channel closed");
                        break;
                    }
                }
            }
            _ = &mut shutdown => {
                tracing::info!("ctrl-c received; sending bye");
                let _ = handles.publisher.publish_bye(&ByeMessage {
                    reason: "user requested".into(),
                }).await;
                tokio::time::sleep(Duration::from_millis(200)).await;
                break;
            }
        }
    }

    if let Handler::Webrtc(t) = &handler {
        let _ = t.close().await;
    }
    handles.eventloop.abort();
    Ok(())
}

fn rand_suffix() -> String {
    // Cheap unique suffix without pulling in `rand`: nanos since epoch.
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.subsec_nanos())
        .unwrap_or(0);
    format!("{nanos:08x}")
}
