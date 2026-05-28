use crate::executor;
use crate::frames::Frame;
use crate::signaling::{
    IceMessage, IceServerEntry, SdpMessage, SdpType, SignalingEvent, SignalingPublisher,
};
use crate::tui::{OutKind, Status, TuiMsg, Who};
use anyhow::Result;
use std::sync::Arc;
use tokio::sync::mpsc;
use tokio::sync::Mutex as TokioMutex;
use webrtc::api::interceptor_registry::register_default_interceptors;
use webrtc::api::media_engine::MediaEngine;
use webrtc::api::APIBuilder;
use webrtc::data_channel::data_channel_message::DataChannelMessage;
use webrtc::data_channel::RTCDataChannel;
use webrtc::ice_transport::ice_candidate::RTCIceCandidateInit;
use webrtc::ice_transport::ice_credential_type::RTCIceCredentialType;
use webrtc::ice_transport::ice_server::RTCIceServer;
use webrtc::interceptor::registry::Registry;
use webrtc::peer_connection::configuration::RTCConfiguration;
use webrtc::peer_connection::sdp::session_description::RTCSessionDescription;
use webrtc::peer_connection::RTCPeerConnection;

fn default_ice_servers() -> Vec<RTCIceServer> {
    vec![RTCIceServer {
        urls: vec!["stun:stun.l.google.com:19302".to_owned()],
        ..Default::default()
    }]
}

pub(crate) fn map_ice_server(entry: IceServerEntry) -> RTCIceServer {
    let username = entry.username.unwrap_or_default();
    let credential = entry.credential.unwrap_or_default();
    let credential_type = if username.is_empty() && credential.is_empty() {
        RTCIceCredentialType::Unspecified
    } else {
        RTCIceCredentialType::Password
    };
    RTCIceServer {
        urls: entry.urls,
        username,
        credential,
        credential_type,
    }
}

pub struct Transport {
    publisher: SignalingPublisher,
    current: TokioMutex<Option<Arc<RTCPeerConnection>>>,
    ice_servers: TokioMutex<Vec<RTCIceServer>>,
    data_channel: Arc<TokioMutex<Option<Arc<RTCDataChannel>>>>,
    display_tx: mpsc::Sender<TuiMsg>,
}

impl Transport {
    pub fn new(publisher: SignalingPublisher, display_tx: mpsc::Sender<TuiMsg>) -> Self {
        Self {
            publisher,
            current: TokioMutex::new(None),
            ice_servers: TokioMutex::new(default_ice_servers()),
            data_channel: Arc::new(TokioMutex::new(None)),
            display_tx,
        }
    }

    pub async fn send_chat(&self, message: String) {
        let dc = self.data_channel.lock().await.clone();
        if let Some(dc) = dc {
            let frame = Frame::Chat { message };
            if let Ok(json) = frame.to_json() {
                if let Err(e) = dc.send_text(json).await {
                    let _ = self
                        .display_tx
                        .send(TuiMsg::Error(format!("send chat failed: {e}")))
                        .await;
                }
            }
        }
    }

    pub async fn handle_event(&self, event: SignalingEvent) -> Result<()> {
        match event {
            SignalingEvent::OperatorSdp(sdp) => {
                if matches!(sdp.kind, SdpType::Answer) {
                    return Ok(());
                }
                let prev = self.current.lock().await.take();
                if let Some(p) = prev {
                    let _ = p.close().await;
                }
                let ice_servers = self.ice_servers.lock().await.clone();
                let pc = self.build_pc(ice_servers).await?;
                let desc = RTCSessionDescription::offer(sdp.sdp)?;
                pc.set_remote_description(desc).await?;
                let answer = pc.create_answer(None).await?;
                let answer_sdp = answer.sdp.clone();
                pc.set_local_description(answer).await?;
                self.publisher
                    .publish_sdp(&SdpMessage {
                        kind: SdpType::Answer,
                        sdp: answer_sdp,
                    })
                    .await?;
                *self.current.lock().await = Some(pc);
                let _ = self
                    .display_tx
                    .send(TuiMsg::System("WebRTC connection established".into()))
                    .await;
            }
            SignalingEvent::OperatorIce(ice) => {
                let pc_opt = self.current.lock().await.clone();
                if let Some(pc) = pc_opt {
                    let init = RTCIceCandidateInit {
                        candidate: ice.candidate,
                        sdp_mid: ice.sdp_mid,
                        sdp_mline_index: ice.sdp_mline_index,
                        username_fragment: None,
                    };
                    if let Err(e) = pc.add_ice_candidate(init).await {
                        let _ = self
                            .display_tx
                            .send(TuiMsg::Error(format!("add ICE candidate failed: {e}")))
                            .await;
                    }
                }
            }
            SignalingEvent::OperatorBye(b) => {
                let _ = self
                    .display_tx
                    .send(TuiMsg::System(format!("operator disconnected: {}", b.reason)))
                    .await;
                let _ = self.display_tx.send(TuiMsg::Status(Status::Ended)).await;
                let prev = self.current.lock().await.take();
                if let Some(p) = prev {
                    let _ = p.close().await;
                }
            }
            SignalingEvent::OperatorData(_) => {
                let _ = self
                    .display_tx
                    .send(TuiMsg::System(
                        "ignoring 'data' frame in WebRTC mode".into(),
                    ))
                    .await;
            }
            SignalingEvent::OperatorTurn(turn) => {
                let mapped: Vec<RTCIceServer> =
                    turn.ice_servers.into_iter().map(map_ice_server).collect();
                let _ = self
                    .display_tx
                    .send(TuiMsg::System(format!(
                        "received TURN config ({} server(s))",
                        mapped.len()
                    )))
                    .await;
                *self.ice_servers.lock().await = mapped;
            }
        }
        Ok(())
    }

    pub async fn close(&self) -> Result<()> {
        let prev = self.current.lock().await.take();
        if let Some(p) = prev {
            let _ = p.close().await;
        }
        Ok(())
    }

    async fn build_pc(&self, ice_servers: Vec<RTCIceServer>) -> Result<Arc<RTCPeerConnection>> {
        let mut me = MediaEngine::default();
        me.register_default_codecs()?;
        let mut registry = Registry::new();
        registry = register_default_interceptors(registry, &mut me)?;
        let api = APIBuilder::new()
            .with_media_engine(me)
            .with_interceptor_registry(registry)
            .build();
        let config = RTCConfiguration {
            ice_servers,
            ..Default::default()
        };
        let pc = Arc::new(api.new_peer_connection(config).await?);

        let display_tx = self.display_tx.clone();
        let dc_store = self.data_channel.clone();
        pc.on_data_channel(Box::new(move |dc: Arc<RTCDataChannel>| {
            let display_tx = display_tx.clone();
            let dc_store = dc_store.clone();
            Box::pin(async move {
                attach_data_channel(dc, display_tx, dc_store).await;
            })
        }));

        let pub_for_ice = self.publisher.clone();
        let display_tx_ice = self.display_tx.clone();
        pc.on_ice_candidate(Box::new(move |cand| {
            let publisher = pub_for_ice.clone();
            let display_tx = display_tx_ice.clone();
            Box::pin(async move {
                let Some(cand) = cand else { return };
                let init = match cand.to_json() {
                    Ok(i) => i,
                    Err(e) => {
                        let _ = display_tx
                            .send(TuiMsg::Error(format!("ICE candidate to_json failed: {e}")))
                            .await;
                        return;
                    }
                };
                let msg = IceMessage {
                    candidate: init.candidate,
                    sdp_mid: init.sdp_mid,
                    sdp_mline_index: init.sdp_mline_index,
                };
                if let Err(e) = publisher.publish_ice(&msg).await {
                    let _ = display_tx
                        .send(TuiMsg::Error(format!("publish ICE candidate failed: {e}")))
                        .await;
                }
            })
        }));

        Ok(pc)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_full_ice_server() {
        let entry = IceServerEntry {
            urls: vec!["turn:turn.cloudflare.com:3478?transport=udp".into()],
            username: Some("u".into()),
            credential: Some("c".into()),
        };
        let mapped = map_ice_server(entry);
        assert_eq!(mapped.urls.len(), 1);
        assert_eq!(mapped.username, "u");
        assert_eq!(mapped.credential, "c");
        assert!(matches!(
            mapped.credential_type,
            RTCIceCredentialType::Password
        ));
    }

    #[test]
    fn maps_stun_without_credentials() {
        let entry = IceServerEntry {
            urls: vec!["stun:stun.example:3478".into()],
            username: None,
            credential: None,
        };
        let mapped = map_ice_server(entry);
        assert_eq!(mapped.username, "");
        assert_eq!(mapped.credential, "");
        assert!(matches!(
            mapped.credential_type,
            RTCIceCredentialType::Unspecified
        ));
    }

    #[test]
    fn default_ice_servers_is_stun_only() {
        let servers = default_ice_servers();
        assert_eq!(servers.len(), 1);
        assert!(servers[0].urls[0].starts_with("stun:"));
        assert!(servers[0].username.is_empty());
    }
}

async fn attach_data_channel(
    dc: Arc<RTCDataChannel>,
    display_tx: mpsc::Sender<TuiMsg>,
    dc_store: Arc<TokioMutex<Option<Arc<RTCDataChannel>>>>,
) {
    let label = dc.label().to_string();
    let _ = display_tx
        .send(TuiMsg::System(format!("data channel '{label}' incoming")))
        .await;

    let dc_for_open = dc.clone();
    let dc_store_for_open = dc_store.clone();
    let display_tx_open = display_tx.clone();
    dc.on_open(Box::new(move || {
        let dc = dc_for_open.clone();
        let store = dc_store_for_open.clone();
        let display_tx = display_tx_open.clone();
        Box::pin(async move {
            *store.lock().await = Some(dc);
            let _ = display_tx.send(TuiMsg::Status(Status::Live)).await;
        })
    }));

    let dc_for_msg = dc.clone();
    let display_tx_msg = display_tx.clone();
    dc.on_message(Box::new(move |msg: DataChannelMessage| {
        let dc = dc_for_msg.clone();
        let display_tx = display_tx_msg.clone();
        Box::pin(async move {
            let Ok(text) = std::str::from_utf8(&msg.data) else {
                let _ = display_tx
                    .send(TuiMsg::System("non-utf8 data channel message".into()))
                    .await;
                return;
            };
            let frame = match Frame::from_json(text) {
                Ok(f) => f,
                Err(e) => {
                    let _ = display_tx
                        .send(TuiMsg::Error(format!("frame parse error: {e}")))
                        .await;
                    return;
                }
            };
            handle_inbound_frame(frame, dc, display_tx).await;
        })
    }));

    let display_tx_close = display_tx.clone();
    dc.on_close(Box::new(move || {
        let display_tx = display_tx_close.clone();
        Box::pin(async move {
            let _ = display_tx
                .send(TuiMsg::System("data channel closed".into()))
                .await;
            let _ = display_tx.send(TuiMsg::Status(Status::Ended)).await;
        })
    }));
}

async fn handle_inbound_frame(
    frame: Frame,
    dc: Arc<RTCDataChannel>,
    display_tx: mpsc::Sender<TuiMsg>,
) {
    match frame {
        Frame::Exec { id, cmd } => {
            let _ = display_tx
                .send(TuiMsg::ExecStarted {
                    id,
                    cmd: cmd.clone(),
                })
                .await;
            let dc_for_pump = dc.clone();
            let display_tx_pump = display_tx.clone();
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
                        let _ = display_tx_pump.send(m).await;
                    }
                    match f.to_json() {
                        Ok(json) => {
                            if let Err(e) = dc_for_pump.send_text(json).await {
                                let _ = display_tx_pump
                                    .send(TuiMsg::Error(format!("send_text failed: {e}")))
                                    .await;
                                break;
                            }
                        }
                        Err(e) => {
                            let _ = display_tx_pump
                                .send(TuiMsg::Error(format!("frame encode failed: {e}")))
                                .await;
                        }
                    }
                }
            });
        }
        Frame::Stdin { .. } | Frame::StdinClose { .. } => {}
        Frame::Chat { message } => {
            let _ = display_tx
                .send(TuiMsg::Chat {
                    from: Who::Operator,
                    message,
                })
                .await;
        }
        Frame::Hello { scope, .. } => {
            let _ = display_tx.send(TuiMsg::Scope(scope)).await;
        }
        other => {
            let _ = display_tx
                .send(TuiMsg::System(format!("unexpected frame: {other:?}")))
                .await;
        }
    }
}
