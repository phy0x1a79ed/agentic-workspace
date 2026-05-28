use crate::executor;
use crate::frames::Frame;
use crate::signaling::{SignalingEvent, SignalingPublisher};
use crate::tui::{OutKind, Status, TuiMsg, Who};
use anyhow::Result;
use tokio::sync::mpsc;

pub struct MqttRelay {
    publisher: SignalingPublisher,
    display_tx: mpsc::Sender<TuiMsg>,
}

impl MqttRelay {
    pub fn new(publisher: SignalingPublisher, display_tx: mpsc::Sender<TuiMsg>) -> Self {
        Self {
            publisher,
            display_tx,
        }
    }

    pub async fn send_chat(&self, message: String) {
        let frame = Frame::Chat { message };
        if let Ok(json) = frame.to_json() {
            if let Err(e) = self.publisher.publish_data(json.into_bytes()).await {
                let _ = self
                    .display_tx
                    .send(TuiMsg::Error(format!("send chat failed: {e}")))
                    .await;
            }
        }
    }

    pub async fn handle_event(&self, event: SignalingEvent) -> Result<()> {
        match event {
            SignalingEvent::OperatorData(bytes) => {
                let text = match std::str::from_utf8(&bytes) {
                    Ok(t) => t,
                    Err(e) => {
                        let _ = self
                            .display_tx
                            .send(TuiMsg::System(format!("non-utf8 data frame: {e}")))
                            .await;
                        return Ok(());
                    }
                };
                let frame = match Frame::from_json(text) {
                    Ok(f) => f,
                    Err(e) => {
                        let _ = self
                            .display_tx
                            .send(TuiMsg::Error(format!("frame parse error: {e}")))
                            .await;
                        return Ok(());
                    }
                };
                self.dispatch_frame(frame).await;
            }
            SignalingEvent::OperatorSdp(_)
            | SignalingEvent::OperatorIce(_)
            | SignalingEvent::OperatorTurn(_) => {
                let _ = self
                    .display_tx
                    .send(TuiMsg::System(
                        "ignoring WebRTC signaling in mqtt-relay mode".into(),
                    ))
                    .await;
            }
            SignalingEvent::OperatorBye(b) => {
                let _ = self
                    .display_tx
                    .send(TuiMsg::System(format!("operator disconnected: {}", b.reason)))
                    .await;
                let _ = self.display_tx.send(TuiMsg::Status(Status::Ended)).await;
            }
        }
        Ok(())
    }

    async fn dispatch_frame(&self, frame: Frame) {
        match frame {
            Frame::Exec { id, cmd } => {
                let _ = self
                    .display_tx
                    .send(TuiMsg::ExecStarted {
                        id,
                        cmd: cmd.clone(),
                    })
                    .await;
                let publisher = self.publisher.clone();
                let display_tx = self.display_tx.clone();
                tokio::spawn(async move {
                    let (tx, mut rx) = mpsc::channel::<Frame>(64);
                    tokio::spawn(executor::run_exec(id, cmd, tx));
                    while let Some(f) = rx.recv().await {
                        let display_msg = match &f {
                            Frame::Stdout { data, .. } => {
                                let text = String::from_utf8_lossy(data).to_string();
                                Some(TuiMsg::ExecOutput {
                                    id,
                                    kind: OutKind::Stdout,
                                    text,
                                })
                            }
                            Frame::Stderr { data, .. } => {
                                let text = String::from_utf8_lossy(data).to_string();
                                Some(TuiMsg::ExecOutput {
                                    id,
                                    kind: OutKind::Stderr,
                                    text,
                                })
                            }
                            Frame::Exit { code, .. } => Some(TuiMsg::ExecExit { id, code: *code }),
                            Frame::Error { message, .. } => {
                                Some(TuiMsg::Error(message.clone()))
                            }
                            _ => None,
                        };
                        if let Some(m) = display_msg {
                            let _ = display_tx.send(m).await;
                        }
                        match f.to_json() {
                            Ok(json) => {
                                if let Err(e) =
                                    publisher.publish_data(json.into_bytes()).await
                                {
                                    let _ = display_tx
                                        .send(TuiMsg::Error(format!(
                                            "publish_data failed: {e}"
                                        )))
                                        .await;
                                    break;
                                }
                            }
                            Err(e) => {
                                let _ = display_tx
                                    .send(TuiMsg::Error(format!(
                                        "frame encode failed: {e}"
                                    )))
                                    .await;
                            }
                        }
                    }
                });
            }
            Frame::Stdin { .. } | Frame::StdinClose { .. } => {}
            Frame::Chat { message } => {
                let _ = self
                    .display_tx
                    .send(TuiMsg::Chat {
                        from: Who::Operator,
                        message,
                    })
                    .await;
            }
            Frame::Hello { scope, .. } => {
                let _ = self.display_tx.send(TuiMsg::Scope(scope)).await;
            }
            other => {
                let _ = self
                    .display_tx
                    .send(TuiMsg::System(format!("unexpected frame (relay): {other:?}")))
                    .await;
            }
        }
    }
}
