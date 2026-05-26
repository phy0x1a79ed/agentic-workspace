use crate::executor;
use crate::frames::Frame;
use crate::signaling::{
    IceMessage, SdpMessage, SdpType, SignalingEvent, SignalingPublisher,
};
use anyhow::Result;
use std::sync::Arc;
use tokio::sync::{mpsc, Mutex};
use webrtc::api::interceptor_registry::register_default_interceptors;
use webrtc::api::media_engine::MediaEngine;
use webrtc::api::APIBuilder;
use webrtc::data_channel::data_channel_message::DataChannelMessage;
use webrtc::data_channel::RTCDataChannel;
use webrtc::ice_transport::ice_candidate::RTCIceCandidateInit;
use webrtc::ice_transport::ice_server::RTCIceServer;
use webrtc::interceptor::registry::Registry;
use webrtc::peer_connection::configuration::RTCConfiguration;
use webrtc::peer_connection::sdp::session_description::RTCSessionDescription;
use webrtc::peer_connection::RTCPeerConnection;

/// Friend-side transport: holds at most one live RTCPeerConnection. Each new
/// operator session (signaled by a fresh SDP Offer) replaces the previous pc.
pub struct Transport {
    publisher: SignalingPublisher,
    current: Mutex<Option<Arc<RTCPeerConnection>>>,
}

impl Transport {
    pub fn new(publisher: SignalingPublisher) -> Self {
        Self {
            publisher,
            current: Mutex::new(None),
        }
    }

    pub async fn handle_event(&self, event: SignalingEvent) -> Result<()> {
        match event {
            SignalingEvent::OperatorSdp(sdp) => {
                if matches!(sdp.kind, SdpType::Answer) {
                    tracing::warn!("ignoring SDP answer from operator (we're the answerer)");
                    return Ok(());
                }
                let prev = self.current.lock().await.take();
                if let Some(p) = prev {
                    let _ = p.close().await;
                }
                let pc = build_pc(self.publisher.clone()).await?;
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
                        tracing::warn!("add_ice_candidate failed: {e}");
                    }
                } else {
                    tracing::warn!("ICE candidate arrived before SDP offer; dropping");
                }
            }
            SignalingEvent::OperatorBye(b) => {
                tracing::info!("operator sent bye: {}", b.reason);
                let prev = self.current.lock().await.take();
                if let Some(p) = prev {
                    let _ = p.close().await;
                }
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
}

async fn build_pc(publisher: SignalingPublisher) -> Result<Arc<RTCPeerConnection>> {
    let mut me = MediaEngine::default();
    me.register_default_codecs()?;
    let mut registry = Registry::new();
    registry = register_default_interceptors(registry, &mut me)?;
    let api = APIBuilder::new()
        .with_media_engine(me)
        .with_interceptor_registry(registry)
        .build();
    let config = RTCConfiguration {
        ice_servers: vec![RTCIceServer {
            urls: vec!["stun:stun.l.google.com:19302".to_owned()],
            ..Default::default()
        }],
        ..Default::default()
    };
    let pc = Arc::new(api.new_peer_connection(config).await?);

    pc.on_data_channel(Box::new(move |dc: Arc<RTCDataChannel>| {
        Box::pin(async move {
            attach_data_channel(dc).await;
        })
    }));

    let pub_for_ice = publisher.clone();
    pc.on_ice_candidate(Box::new(move |cand| {
        let publisher = pub_for_ice.clone();
        Box::pin(async move {
            let Some(cand) = cand else { return };
            let init = match cand.to_json() {
                Ok(i) => i,
                Err(e) => {
                    tracing::warn!("local ICE candidate to_json failed: {e}");
                    return;
                }
            };
            let msg = IceMessage {
                candidate: init.candidate,
                sdp_mid: init.sdp_mid,
                sdp_mline_index: init.sdp_mline_index,
            };
            if let Err(e) = publisher.publish_ice(&msg).await {
                tracing::warn!("publish local ICE candidate failed: {e}");
            }
        })
    }));

    pc.on_peer_connection_state_change(Box::new(move |state| {
        Box::pin(async move {
            tracing::info!("peer connection state: {state:?}");
        })
    }));

    pc.on_ice_connection_state_change(Box::new(move |state| {
        Box::pin(async move {
            tracing::info!("ICE connection state: {state:?}");
        })
    }));

    Ok(pc)
}

async fn attach_data_channel(dc: Arc<RTCDataChannel>) {
    let label = dc.label().to_string();
    tracing::info!("data channel '{label}' incoming");

    dc.on_open(Box::new(|| {
        Box::pin(async move {
            tracing::info!("data channel open");
        })
    }));

    let dc_for_msg = dc.clone();
    dc.on_message(Box::new(move |msg: DataChannelMessage| {
        let dc = dc_for_msg.clone();
        Box::pin(async move {
            let Ok(text) = std::str::from_utf8(&msg.data) else {
                tracing::warn!("non-utf8 data channel message ignored");
                return;
            };
            let frame = match Frame::from_json(text) {
                Ok(f) => f,
                Err(e) => {
                    tracing::warn!("frame parse error: {e}; raw={text}");
                    return;
                }
            };
            handle_inbound_frame(frame, dc).await;
        })
    }));

    dc.on_close(Box::new(|| {
        Box::pin(async move {
            tracing::info!("data channel closed");
        })
    }));
}

async fn handle_inbound_frame(frame: Frame, dc: Arc<RTCDataChannel>) {
    match frame {
        Frame::Exec { id, cmd } => {
            tracing::info!("exec id={id} cmd={cmd:?}");
            let dc_for_pump = dc.clone();
            tokio::spawn(async move {
                let (tx, mut rx) = mpsc::channel::<Frame>(64);
                tokio::spawn(executor::run_exec(id, cmd, tx));
                while let Some(f) = rx.recv().await {
                    match f.to_json() {
                        Ok(json) => {
                            if let Err(e) = dc_for_pump.send_text(json).await {
                                tracing::warn!("send_text failed: {e}");
                                break;
                            }
                        }
                        Err(e) => tracing::warn!("frame encode failed: {e}"),
                    }
                }
            });
        }
        Frame::Stdin { .. } | Frame::StdinClose { .. } => {
            // reserved; v1 ignores
        }
        other => {
            tracing::warn!("unexpected inbound frame at friend: {other:?}");
        }
    }
}
