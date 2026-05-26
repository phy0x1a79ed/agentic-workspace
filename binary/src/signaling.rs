use anyhow::{Context, Result};
use rumqttc::{AsyncClient, Event, MqttOptions, Packet, QoS, TlsConfiguration, Transport};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;
use url::Url;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum SdpType {
    Offer,
    Answer,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SdpMessage {
    #[serde(rename = "type")]
    pub kind: SdpType,
    pub sdp: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IceMessage {
    pub candidate: String,
    #[serde(rename = "sdpMid", skip_serializing_if = "Option::is_none")]
    pub sdp_mid: Option<String>,
    #[serde(rename = "sdpMLineIndex", skip_serializing_if = "Option::is_none")]
    pub sdp_mline_index: Option<u16>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ByeMessage {
    pub reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IceServerEntry {
    pub urls: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub username: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub credential: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TurnMessage {
    #[serde(rename = "iceServers")]
    pub ice_servers: Vec<IceServerEntry>,
}

#[derive(Debug)]
pub enum SignalingEvent {
    OperatorSdp(SdpMessage),
    OperatorIce(IceMessage),
    OperatorBye(ByeMessage),
    OperatorTurn(TurnMessage),
}

pub struct SignalingConfig {
    pub url: String,
    pub user: String,
    pub pass: String,
    pub name: String,
    pub client_id: String,
}

#[derive(Clone)]
pub struct SignalingPublisher {
    client: AsyncClient,
    name: String,
}

impl SignalingPublisher {
    pub async fn publish_sdp(&self, msg: &SdpMessage) -> Result<()> {
        self.publish_one("sdp", serde_json::to_vec(msg)?).await
    }

    pub async fn publish_ice(&self, msg: &IceMessage) -> Result<()> {
        self.publish_one("ice", serde_json::to_vec(msg)?).await
    }

    pub async fn publish_bye(&self, msg: &ByeMessage) -> Result<()> {
        self.publish_one("bye", serde_json::to_vec(msg)?).await
    }

    async fn publish_one(&self, leaf: &str, payload: Vec<u8>) -> Result<()> {
        let topic = format!("probe/{}/from-friend/{}", self.name, leaf);
        self.client
            .publish(topic, QoS::AtLeastOnce, false, payload)
            .await?;
        Ok(())
    }
}

pub struct SignalingHandles {
    pub publisher: SignalingPublisher,
    pub events: mpsc::Receiver<SignalingEvent>,
    pub eventloop: tokio::task::JoinHandle<()>,
}

pub async fn connect(cfg: SignalingConfig) -> Result<SignalingHandles> {
    let url = Url::parse(&cfg.url).context("invalid EMQX URL")?;
    let host = url
        .host_str()
        .context("missing host in EMQX URL")?
        .to_string();
    let port = url.port().unwrap_or(match url.scheme() {
        "wss" | "https" => 8084,
        "mqtts" => 8883,
        _ => 1883,
    });
    let scheme = url.scheme();
    let path = if url.path().is_empty() {
        "/mqtt".to_string()
    } else {
        url.path().to_string()
    };

    // rumqttc WS expects the full URL in the host slot; for plain MQTT it's just the hostname.
    let broker_id = match scheme {
        "wss" => format!("wss://{}:{}{}", host, port, path),
        "ws" => format!("ws://{}:{}{}", host, port, path),
        _ => host.clone(),
    };

    let mut opts = MqttOptions::new(&cfg.client_id, &broker_id, port);
    opts.set_credentials(&cfg.user, &cfg.pass);
    opts.set_keep_alive(Duration::from_secs(30));
    opts.set_clean_session(true);

    match scheme {
        "wss" => {
            let tls = build_tls();
            opts.set_transport(Transport::Wss(TlsConfiguration::Rustls(Arc::new(tls))));
        }
        "mqtts" => {
            let tls = build_tls();
            opts.set_transport(Transport::Tls(TlsConfiguration::Rustls(Arc::new(tls))));
        }
        "ws" => {
            opts.set_transport(Transport::Ws);
        }
        _ => {}
    }

    let (client, mut eventloop) = AsyncClient::new(opts, 64);
    let sub_topic = format!("probe/{}/from-operator/+", cfg.name);
    client
        .subscribe(&sub_topic, QoS::AtLeastOnce)
        .await
        .context("MQTT subscribe failed")?;

    let publisher = SignalingPublisher {
        client: client.clone(),
        name: cfg.name.clone(),
    };
    let (tx, rx) = mpsc::channel(64);
    let name_for_loop = cfg.name.clone();

    let handle = tokio::spawn(async move {
        loop {
            match eventloop.poll().await {
                Ok(Event::Incoming(Packet::Publish(p))) => {
                    if let Some(event) = parse_publish(&name_for_loop, &p.topic, &p.payload) {
                        if tx.send(event).await.is_err() {
                            return;
                        }
                    }
                }
                Ok(_) => {}
                Err(e) => {
                    tracing::warn!("mqtt eventloop error: {e}; sleeping 2s");
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
            }
        }
    });

    Ok(SignalingHandles {
        publisher,
        events: rx,
        eventloop: handle,
    })
}

fn build_tls() -> rumqttc::tokio_rustls::rustls::ClientConfig {
    use rumqttc::tokio_rustls::rustls::{ClientConfig, RootCertStore};
    let mut roots = RootCertStore::empty();
    roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
    ClientConfig::builder()
        .with_root_certificates(roots)
        .with_no_client_auth()
}

fn parse_publish(name: &str, topic: &str, payload: &[u8]) -> Option<SignalingEvent> {
    let prefix = format!("probe/{name}/from-operator/");
    let leaf = topic.strip_prefix(&prefix)?;
    match leaf {
        "sdp" => serde_json::from_slice::<SdpMessage>(payload)
            .ok()
            .map(SignalingEvent::OperatorSdp),
        "ice" => serde_json::from_slice::<IceMessage>(payload)
            .ok()
            .map(SignalingEvent::OperatorIce),
        "bye" => serde_json::from_slice::<ByeMessage>(payload)
            .ok()
            .map(SignalingEvent::OperatorBye),
        "turn" => serde_json::from_slice::<TurnMessage>(payload)
            .ok()
            .map(SignalingEvent::OperatorTurn),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_sdp_offer() {
        let payload = br#"{"type":"offer","sdp":"v=0\r\n..."}"#;
        let evt = parse_publish("foo", "probe/foo/from-operator/sdp", payload).unwrap();
        match evt {
            SignalingEvent::OperatorSdp(s) => {
                assert_eq!(s.kind, SdpType::Offer);
                assert!(s.sdp.starts_with("v=0"));
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn parses_ice() {
        let payload = br#"{"candidate":"candidate:1 1 UDP 2122 1.2.3.4 1234 typ host","sdpMid":"0","sdpMLineIndex":0}"#;
        let evt = parse_publish("foo", "probe/foo/from-operator/ice", payload).unwrap();
        assert!(matches!(evt, SignalingEvent::OperatorIce(_)));
    }

    #[test]
    fn parses_bye() {
        let payload = br#"{"reason":"user requested"}"#;
        let evt = parse_publish("foo", "probe/foo/from-operator/bye", payload).unwrap();
        assert!(matches!(evt, SignalingEvent::OperatorBye(_)));
    }

    #[test]
    fn ignores_other_probe_names() {
        let payload = br#"{"type":"offer","sdp":""}"#;
        assert!(parse_publish("foo", "probe/bar/from-operator/sdp", payload).is_none());
    }

    #[test]
    fn ignores_own_friend_topics() {
        let payload = br#"{"type":"answer","sdp":""}"#;
        assert!(parse_publish("foo", "probe/foo/from-friend/sdp", payload).is_none());
    }

    #[test]
    fn ignores_unknown_leaf() {
        let payload = br#"{}"#;
        assert!(parse_publish("foo", "probe/foo/from-operator/unknown", payload).is_none());
    }

    #[test]
    fn parses_turn() {
        let payload = br#"{"iceServers":[{"urls":["turn:turn.cloudflare.com:3478?transport=udp","turns:turn.cloudflare.com:5349?transport=tcp"],"username":"g071e","credential":"396c3"}]}"#;
        let evt = parse_publish("foo", "probe/foo/from-operator/turn", payload).unwrap();
        match evt {
            SignalingEvent::OperatorTurn(t) => {
                assert_eq!(t.ice_servers.len(), 1);
                let s = &t.ice_servers[0];
                assert_eq!(s.urls.len(), 2);
                assert!(s.urls[0].starts_with("turn:"));
                assert_eq!(s.username.as_deref(), Some("g071e"));
                assert_eq!(s.credential.as_deref(), Some("396c3"));
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn parses_turn_without_credentials() {
        let payload = br#"{"iceServers":[{"urls":["stun:stun.example:3478"]}]}"#;
        let evt = parse_publish("foo", "probe/foo/from-operator/turn", payload).unwrap();
        match evt {
            SignalingEvent::OperatorTurn(t) => {
                assert!(t.ice_servers[0].username.is_none());
                assert!(t.ice_servers[0].credential.is_none());
            }
            _ => panic!("wrong variant"),
        }
    }
}
