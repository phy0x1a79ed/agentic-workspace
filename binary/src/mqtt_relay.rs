//! MQTT-relay friend transport.
//!
//! When the friend's network blocks UDP (HPC nodes, restrictive corp egress)
//! WebRTC cannot establish a data channel — webrtc-rs 0.11 also lacks TURN-
//! over-TCP relay gathering. This module bypasses WebRTC entirely: the
//! protocol Frame stream rides the same MQTT broker used for signaling, on
//! the topic `probe/<name>/from-{friend,operator}/data`.
//!
//! Topology:
//!     operator --[publish data]--> EMQX --[deliver]--> friend (this)
//!     friend   --[publish data]--> EMQX --[deliver]--> operator
//!
//! The broker sees command stdout/stderr. Use only when WebRTC's privacy
//! benefit is unavailable.

use crate::executor;
use crate::frames::Frame;
use crate::signaling::{SignalingEvent, SignalingPublisher};
use anyhow::Result;
use tokio::sync::mpsc;

pub struct MqttRelay {
    publisher: SignalingPublisher,
}

impl MqttRelay {
    pub fn new(publisher: SignalingPublisher) -> Self {
        Self { publisher }
    }

    pub async fn handle_event(&self, event: SignalingEvent) -> Result<()> {
        match event {
            SignalingEvent::OperatorData(bytes) => {
                let text = match std::str::from_utf8(&bytes) {
                    Ok(t) => t,
                    Err(e) => {
                        tracing::warn!("non-utf8 data frame ignored: {e}");
                        return Ok(());
                    }
                };
                let frame = match Frame::from_json(text) {
                    Ok(f) => f,
                    Err(e) => {
                        tracing::warn!("data frame parse error: {e}; raw={text}");
                        return Ok(());
                    }
                };
                self.dispatch_frame(frame).await;
            }
            // Ignore WebRTC signaling traffic in relay mode — the operator
            // is expected to also be in --mqtt-relay mode and not send
            // SDP/ICE/TURN. Log if anything sneaks in.
            SignalingEvent::OperatorSdp(_)
            | SignalingEvent::OperatorIce(_)
            | SignalingEvent::OperatorTurn(_) => {
                tracing::warn!(
                    "ignoring WebRTC signaling event in mqtt-relay mode \
                     (operator should also be in relay mode)"
                );
            }
            SignalingEvent::OperatorBye(b) => {
                tracing::info!("operator sent bye: {}", b.reason);
            }
        }
        Ok(())
    }

    async fn dispatch_frame(&self, frame: Frame) {
        match frame {
            Frame::Exec { id, cmd } => {
                tracing::info!("[relay] exec id={id} cmd={cmd:?}");
                let publisher = self.publisher.clone();
                tokio::spawn(async move {
                    let (tx, mut rx) = mpsc::channel::<Frame>(64);
                    tokio::spawn(executor::run_exec(id, cmd, tx));
                    while let Some(f) = rx.recv().await {
                        match f.to_json() {
                            Ok(json) => {
                                if let Err(e) =
                                    publisher.publish_data(json.into_bytes()).await
                                {
                                    tracing::warn!("publish_data failed: {e}");
                                    break;
                                }
                            }
                            Err(e) => tracing::warn!("frame encode failed: {e}"),
                        }
                    }
                });
            }
            Frame::Stdin { .. } | Frame::StdinClose { .. } => {
                // reserved; v1 ignores (matches webrtc transport behavior)
            }
            other => {
                tracing::warn!("unexpected inbound frame at friend (relay): {other:?}");
            }
        }
    }
}
