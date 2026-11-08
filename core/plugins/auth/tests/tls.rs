//! The edge's TLS listener: handshakes over loopback against a leaf from the private CA.

use std::path::Path;
use std::sync::Arc;

use awm_plugin_auth::{certs, tls};
use rustls::pki_types::ServerName;
use rustls::{ClientConfig, RootCertStore};
use serde_json::Value;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

const RSA_VECTORS: &str = include_str!("vectors/rsa_leaf.json");

fn sans(v: &[&str]) -> Vec<String> {
    v.iter().map(|x| x.to_string()).collect()
}

fn provider() -> Arc<rustls::crypto::CryptoProvider> {
    Arc::new(rustls::crypto::ring::default_provider())
}

fn client(ca_pem: &[u8], versions: &[&'static rustls::SupportedProtocolVersion], alpn: &[&[u8]]) -> tokio_rustls::TlsConnector {
    let mut roots = RootCertStore::empty();
    for c in tls::parse_certs(ca_pem, "ca").unwrap() {
        roots.add(c).unwrap();
    }
    let mut cfg = ClientConfig::builder_with_provider(provider())
        .with_protocol_versions(versions)
        .unwrap()
        .with_root_certificates(roots)
        .with_no_client_auth();
    cfg.alpn_protocols = alpn.iter().map(|p| p.to_vec()).collect();
    tokio_rustls::TlsConnector::from(Arc::new(cfg))
}

/// Serve `config` on an ephemeral port with a handler that answers `pong` to one line.
async fn serve(config: Arc<rustls::ServerConfig>) -> u16 {
    let listener = tls::bind_tls(0).await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(tls::serve_tls(listener, config, |mut stream, _peer| async move {
        let mut buf = [0u8; 5];
        if stream.read_exact(&mut buf).await.is_ok() && &buf == b"ping\n" {
            let _ = stream.write_all(b"pong\n").await;
            let _ = stream.shutdown().await;
        }
    }));
    port
}

async fn ping(
    port: u16,
    connector: tokio_rustls::TlsConnector,
    name: ServerName<'static>,
) -> std::io::Result<(Vec<u8>, Option<Vec<u8>>, Option<rustls::ProtocolVersion>)> {
    let tcp = tokio::net::TcpStream::connect(("127.0.0.1", port)).await?;
    let mut s = connector.connect(name, tcp).await?;
    let alpn = s.get_ref().1.alpn_protocol().map(|p| p.to_vec());
    let version = s.get_ref().1.protocol_version();
    s.write_all(b"ping\n").await?;
    let mut out = Vec::new();
    s.read_to_end(&mut out).await?;
    Ok((out, alpn, version))
}

fn ip_name(ip: &str) -> ServerName<'static> {
    ServerName::IpAddress(ip.parse::<std::net::IpAddr>().unwrap().into())
}

fn minted(dir: &Path) -> (certs::CertInfo, Vec<u8>) {
    let ca = dir.join("ca");
    let info = certs::ensure_certs(&dir.join("certs"), Some(&ca), Some(&sans(&["IP:127.0.0.1", "DNS:localhost"]))).unwrap();
    let root = std::fs::read(ca.join("ca.pem")).unwrap();
    (info, root)
}

#[tokio::test]
async fn a_minted_leaf_completes_a_handshake_with_a_client_trusting_the_ca() {
    let tmp = tempfile::tempdir().unwrap();
    let (info, root) = minted(tmp.path());
    let port = serve(tls::server_config(&info.cert, &info.key).unwrap()).await;

    for versions in [&[&rustls::version::TLS13][..], &[&rustls::version::TLS12][..]] {
        let (out, alpn, version) = ping(port, client(&root, versions, &[b"h2", b"http/1.1"]), ip_name("127.0.0.1")).await.unwrap();
        assert_eq!(out, b"pong\n");
        // uvicorn spoke HTTP/1.1 only.
        assert_eq!(alpn.as_deref(), Some(&b"http/1.1"[..]));
        assert_eq!(version, Some(versions[0].version));
    }
    let named = ServerName::try_from("localhost").unwrap();
    let (out, _, _) = ping(port, client(&root, &[&rustls::version::TLS13], &[]), named).await.unwrap();
    assert_eq!(out, b"pong\n");
}

#[tokio::test]
async fn a_client_trusting_another_root_is_refused() {
    let tmp = tempfile::tempdir().unwrap();
    let (info, _) = minted(tmp.path());
    let other = tempfile::tempdir().unwrap();
    let (_, other_root) = minted(other.path());
    let port = serve(tls::server_config(&info.cert, &info.key).unwrap()).await;
    assert!(ping(port, client(&other_root, &[&rustls::version::TLS13], &[]), ip_name("127.0.0.1")).await.is_err());
}

#[tokio::test]
async fn a_name_outside_the_san_set_is_refused() {
    let tmp = tempfile::tempdir().unwrap();
    let (info, root) = minted(tmp.path());
    let port = serve(tls::server_config(&info.cert, &info.key).unwrap()).await;
    assert!(ping(port, client(&root, &[&rustls::version::TLS13], &[]), ip_name("10.74.81.213")).await.is_err());
    // A failed handshake leaves the listener serving.
    assert!(ping(port, client(&root, &[&rustls::version::TLS13], &[]), ip_name("127.0.0.1")).await.is_ok());
}

#[tokio::test]
async fn the_fleets_rsa_leaf_loads_and_serves_in_either_key_format() {
    let v: Value = serde_json::from_str(RSA_VECTORS).unwrap();
    let root = v["ca_pem"].as_str().unwrap().as_bytes();
    for key in ["leaf_key_pkcs8", "leaf_key_pkcs1"] {
        let cfg = tls::server_config_from_pem(v["leaf_pem"].as_str().unwrap().as_bytes(), v[key].as_str().unwrap().as_bytes()).unwrap();
        let port = serve(Arc::new(cfg)).await;
        for versions in [&[&rustls::version::TLS13][..], &[&rustls::version::TLS12][..]] {
            let (out, _, _) = ping(port, client(root, versions, &[]), ip_name("10.74.81.213")).await.unwrap_or_else(|e| panic!("{key}: {e}"));
            assert_eq!(out, b"pong\n");
        }
    }
}

#[test]
fn an_ed25519_leaf_loads() {
    let kp = rcgen::KeyPair::generate_for(&rcgen::PKCS_ED25519).unwrap();
    let cert = rcgen::CertificateParams::new(vec!["localhost".to_owned()]).unwrap().self_signed(&kp).unwrap();
    tls::server_config_from_pem(cert.pem().as_bytes(), kp.serialize_pem().as_bytes()).unwrap();
}

#[test]
fn a_key_that_is_not_the_leafs_is_refused() {
    let tmp = tempfile::tempdir().unwrap();
    let (info, _) = minted(tmp.path());
    let other = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P256_SHA256).unwrap();
    let cert = std::fs::read(&info.cert).unwrap();
    assert!(tls::server_config_from_pem(&cert, other.serialize_pem().as_bytes()).is_err());
}

#[test]
fn missing_or_empty_leaf_files_are_errors() {
    let tmp = tempfile::tempdir().unwrap();
    let empty = tmp.path().join("empty.pem");
    std::fs::write(&empty, b"").unwrap();
    assert!(matches!(tls::server_config(&tmp.path().join("nope.pem"), &empty), Err(tls::TlsError::Io(..))));
    assert!(matches!(tls::server_config(&empty, &empty), Err(tls::TlsError::NoCert(_))));
    let (info, _) = minted(tmp.path());
    assert!(matches!(tls::server_config(&info.cert, &empty), Err(tls::TlsError::NoKey(_))));
}

#[tokio::test]
async fn the_plain_listener_binds_loopback_only_and_hands_out_streams() {
    let listener = tls::bind_plain(0).await.unwrap();
    let addr = listener.local_addr().unwrap();
    assert!(addr.ip().is_loopback());
    tokio::spawn(tls::serve_plain(listener, |mut stream, peer| async move {
        assert!(peer.ip().is_loopback());
        let mut buf = [0u8; 5];
        if stream.read_exact(&mut buf).await.is_ok() {
            let _ = stream.write_all(b"pong\n").await;
        }
    }));
    let mut s = tokio::net::TcpStream::connect(addr).await.unwrap();
    s.write_all(b"ping\n").await.unwrap();
    let mut out = Vec::new();
    s.read_to_end(&mut out).await.unwrap();
    assert_eq!(out, b"pong\n");
}

#[test]
fn edge_tls_is_on_unless_it_says_off() {
    for off in ["0", "false", "no", " NO ", "False"] {
        assert!(!tls::tls_enabled_from(Some(off)), "{off}");
    }
    for on in [None, Some("1"), Some("true"), Some(""), Some("off"), Some("yes")] {
        assert!(tls::tls_enabled_from(on), "{on:?}");
    }
}

#[test]
fn the_tls_listener_binds_every_ipv4_interface() {
    let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
    let addr = rt.block_on(async { tls::bind_tls(0).await.unwrap().local_addr().unwrap() });
    assert!(addr.ip().is_unspecified() && addr.is_ipv4());
}
