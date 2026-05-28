mod executor;
mod frames;
mod mqtt_relay;
mod signaling;
mod transport;
mod tui;

use anyhow::{Context, Result};
use clap::Parser;
use signaling::{ByeMessage, SignalingConfig};
use std::io::{self, BufRead, Write};
use std::process::ExitCode;
use std::time::Duration;
use tokio::sync::mpsc;
use tui::{HeaderInit, Status, TuiMsg, UiEvent, Who};

const VERSION: &str = env!("CARGO_PKG_VERSION");

const BUILTIN_MQTT_URL: &str = "wss://s12a68ff.ala.us-east-1.emqxsl.com:8084/mqtt";
const BUILTIN_MQTT_USER: &str = "awm_probe";

#[derive(Parser, Debug)]
#[command(name = "probe", version = VERSION, about = "ephemeral pair-debug shell")]
struct Args {
    #[arg(long, short = 'n')]
    name: String,

    #[arg(long, default_value = BUILTIN_MQTT_URL)]
    mqtt_url: String,

    #[arg(long, default_value = BUILTIN_MQTT_USER)]
    mqtt_user: String,

    #[arg(long, env = "EMQX_PASS")]
    mqtt_pass: String,

    #[arg(long, default_value = "an awm operator")]
    operator: String,

    #[arg(long, default_value_t = false)]
    no_consent: bool,

    #[arg(long, default_value_t = false)]
    mqtt_relay: bool,
}

#[tokio::main]
async fn main() -> ExitCode {
    let args = Args::parse();

    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("error")),
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
    let (msg_tx, msg_rx) = mpsc::channel::<TuiMsg>(1024);
    let (ui_tx, mut ui_rx) = mpsc::channel::<UiEvent>(64);

    let header_init = HeaderInit {
        name: args.name.clone(),
        transport: if args.mqtt_relay {
            "MQTT-relay"
        } else {
            "WebRTC"
        },
    };

    let tui_handle = tokio::spawn(tui::run_tui(header_init, msg_rx, ui_tx));

    let _ = msg_tx
        .send(TuiMsg::System("connecting to broker...".into()))
        .await;

    let client_id = format!("probe-friend-{}-{}", args.name, rand_suffix());
    let cfg = SignalingConfig {
        url: args.mqtt_url,
        user: args.mqtt_user,
        pass: args.mqtt_pass,
        name: args.name.clone(),
        client_id,
    };

    let mut handles = signaling::connect(cfg).await.context("signaling connect failed")?;
    let _ = msg_tx
        .send(TuiMsg::System("subscribed; waiting for operator...".into()))
        .await;

    enum Handler {
        Webrtc(transport::Transport),
        MqttRelay(mqtt_relay::MqttRelay),
    }

    impl Handler {
        async fn send_chat(&self, message: String) {
            match self {
                Handler::Webrtc(t) => t.send_chat(message).await,
                Handler::MqttRelay(r) => r.send_chat(message).await,
            }
        }
    }

    let handler = if args.mqtt_relay {
        // Relay mode has no separate "channel open" — being subscribed is
        // as open as it gets. Flip the status to live now so the friend
        // sees the right state.
        let _ = msg_tx.send(TuiMsg::Status(Status::Live)).await;
        Handler::MqttRelay(mqtt_relay::MqttRelay::new(
            handles.publisher.clone(),
            msg_tx.clone(),
        ))
    } else {
        Handler::Webrtc(transport::Transport::new(
            handles.publisher.clone(),
            msg_tx.clone(),
        ))
    };

    let shutdown = tokio::signal::ctrl_c();
    tokio::pin!(shutdown);

    let mut tui_alive = true;

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
                            let _ = msg_tx
                                .send(TuiMsg::Error(format!("handle_event failed: {e:#}")))
                                .await;
                        }
                    }
                    None => {
                        let _ = msg_tx
                            .send(TuiMsg::System("signaling stream ended".into()))
                            .await;
                        let _ = msg_tx.send(TuiMsg::Status(Status::Ended)).await;
                        break;
                    }
                }
            }
            // UI events (Enter to send chat, Esc/Ctrl-D to disconnect). Must
            // be in the select! rather than a try_recv after the signaling
            // arm — otherwise the loop blocks indefinitely on
            // `handles.events.recv()` between MQTT events, and chat keystrokes
            // sit in the queue until the next signaling message wakes us up.
            ui_evt = ui_rx.recv(), if tui_alive => {
                match ui_evt {
                    Some(UiEvent::SendChat(msg)) => {
                        let _ = msg_tx
                            .send(TuiMsg::Chat {
                                from: Who::Friend,
                                message: msg.clone(),
                            })
                            .await;
                        handler.send_chat(msg).await;
                    }
                    Some(UiEvent::Disconnect) => {
                        let _ = msg_tx.send(TuiMsg::Status(Status::Ended)).await;
                        break;
                    }
                    None => {
                        // TUI task has dropped its UiEvent sender — stop
                        // polling this arm so select! doesn't busy-spin.
                        tui_alive = false;
                    }
                }
            }
            _ = &mut shutdown => {
                let _ = msg_tx
                    .send(TuiMsg::System("ctrl-c received; disconnecting...".into()))
                    .await;
                let _ = msg_tx.send(TuiMsg::Status(Status::Ended)).await;
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
    drop(msg_tx);
    tui_handle.abort();
    Ok(())
}

fn rand_suffix() -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.subsec_nanos())
        .unwrap_or(0);
    format!("{nanos:08x}")
}
