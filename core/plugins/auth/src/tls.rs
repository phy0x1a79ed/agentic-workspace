//! The edge's listeners: a rustls server config built from the leaf, and accept loops that
//! hand each connection to a callback (ports/auth.md C1, C2).
//!
//! The Python front ran uvicorn, which spoke HTTP/1.1 only and read the leaf once at start.
//! This keeps both: ALPN offers `http/1.1` alone, and a new leaf takes effect on restart.

use std::future::Future;
use std::net::{Ipv4Addr, SocketAddr};
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use rustls::pki_types::{CertificateDer, PrivateKeyDer};
use rustls::ServerConfig;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::{TcpListener, TcpStream};
pub use tokio_rustls::server::TlsStream;
use tokio_rustls::TlsAcceptor;

pub const EDGE_TLS_ENV: &str = "AWM_EDGE_TLS";
pub const HTTPS_PORT_ENV: &str = "AWM_HTTPS_PORT";
/// The port when `$AWM_HTTPS_PORT` is unset, as the Python front had it.
pub const DEFAULT_HTTPS_PORT: u16 = 8443;
pub const ALPN_HTTP11: &[u8] = b"http/1.1";
/// asyncio's default TLS handshake timeout, which uvicorn ran under.
pub const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(60);

#[derive(Debug, thiserror::Error)]
pub enum TlsError {
    #[error("{0}: {1}")]
    Io(String, #[source] std::io::Error),
    #[error("{0}: no certificate in the file")]
    NoCert(String),
    #[error("{0}: no private key in the file")]
    NoKey(String),
    #[error("{0}")]
    Rustls(#[from] rustls::Error),
}

/// `$AWM_EDGE_TLS` is on unless it reads `0`, `false` or `no`. Off means plain HTTP on
/// loopback behind a TLS-terminating proxy, with no certs minted.
pub fn tls_enabled() -> bool {
    tls_enabled_from(std::env::var(EDGE_TLS_ENV).ok().as_deref())
}

pub fn tls_enabled_from(raw: Option<&str>) -> bool {
    !matches!(raw.unwrap_or("1").trim().to_lowercase().as_str(), "0" | "false" | "no")
}

/// `$AWM_HTTPS_PORT`, else [`DEFAULT_HTTPS_PORT`]; an error names a value that is not a port.
pub fn https_port() -> Result<u16, String> {
    match std::env::var(HTTPS_PORT_ENV) {
        Ok(v) => v.trim().parse().map_err(|_| format!("{HTTPS_PORT_ENV}={v:?} is not a port")),
        Err(_) => Ok(DEFAULT_HTTPS_PORT),
    }
}

/// Every certificate in a PEM blob, leaf first.
pub fn parse_certs(pem: &[u8], what: &str) -> Result<Vec<CertificateDer<'static>>, TlsError> {
    let certs = rustls_pemfile::certs(&mut &pem[..]).collect::<Result<Vec<_>, _>>().map_err(|e| TlsError::Io(what.to_owned(), e))?;
    if certs.is_empty() {
        return Err(TlsError::NoCert(what.to_owned()));
    }
    Ok(certs)
}

/// The first private key in a PEM blob: PKCS#8, PKCS#1 RSA or SEC1 EC.
pub fn parse_key(pem: &[u8], what: &str) -> Result<PrivateKeyDer<'static>, TlsError> {
    rustls_pemfile::private_key(&mut &pem[..])
        .map_err(|e| TlsError::Io(what.to_owned(), e))?
        .ok_or_else(|| TlsError::NoKey(what.to_owned()))
}

/// A server config for the leaf: ring, TLS 1.2 and 1.3, ALPN `http/1.1`, no client auth.
/// RSA, ECDSA and Ed25519 keys all load; a key that is not the leaf's is refused.
pub fn server_config_from_pem(cert_pem: &[u8], key_pem: &[u8]) -> Result<ServerConfig, TlsError> {
    build(parse_certs(cert_pem, "cert")?, parse_key(key_pem, "key")?)
}

fn build(certs: Vec<CertificateDer<'static>>, key: PrivateKeyDer<'static>) -> Result<ServerConfig, TlsError> {
    let mut cfg = ServerConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
        .with_protocol_versions(&[&rustls::version::TLS13, &rustls::version::TLS12])?
        .with_no_client_auth()
        .with_single_cert(certs, key)?;
    cfg.alpn_protocols = vec![ALPN_HTTP11.to_vec()];
    Ok(cfg)
}

/// [`server_config_from_pem`] over the leaf files, read once.
pub fn server_config(cert_path: &Path, key_path: &Path) -> Result<Arc<ServerConfig>, TlsError> {
    let read = |p: &Path| std::fs::read(p).map_err(|e| TlsError::Io(p.display().to_string(), e));
    let certs = parse_certs(&read(cert_path)?, &cert_path.display().to_string())?;
    let key = parse_key(&read(key_path)?, &key_path.display().to_string())?;
    build(certs, key).map(Arc::new)
}

/// Bind the TLS listener on every IPv4 interface, as the Python front did.
pub async fn bind_tls(port: u16) -> std::io::Result<TcpListener> {
    TcpListener::bind((Ipv4Addr::UNSPECIFIED, port)).await
}

/// Bind the plain-HTTP listener, loopback only.
pub async fn bind_plain(port: u16) -> std::io::Result<TcpListener> {
    TcpListener::bind((Ipv4Addr::LOCALHOST, port)).await
}

async fn accept_next(listener: &TcpListener) -> (TcpStream, SocketAddr) {
    loop {
        match listener.accept().await {
            Ok((stream, peer)) => {
                let _ = stream.set_nodelay(true);
                return (stream, peer);
            }
            Err(e) => {
                // EMFILE and friends: back off a moment rather than spin.
                tracing::warn!("accept failed: {e}");
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        }
    }
}

/// Accept forever, complete each TLS handshake on its own task, and hand the stream and
/// peer address to `handler`. A failed or slow handshake drops that connection only.
pub async fn serve_tls<F, Fut>(listener: TcpListener, config: Arc<ServerConfig>, handler: F)
where
    F: Fn(TlsStream<TcpStream>, SocketAddr) -> Fut + Clone + Send + Sync + 'static,
    Fut: Future<Output = ()> + Send + 'static,
{
    let acceptor = TlsAcceptor::from(config);
    loop {
        let (stream, peer) = accept_next(&listener).await;
        let acceptor = acceptor.clone();
        let handler = handler.clone();
        tokio::spawn(async move {
            match tokio::time::timeout(HANDSHAKE_TIMEOUT, acceptor.accept(stream)).await {
                Ok(Ok(tls)) => handler(tls, peer).await,
                Ok(Err(e)) => tracing::debug!("TLS handshake from {peer} failed: {e}"),
                Err(_) => tracing::debug!("TLS handshake from {peer} timed out"),
            }
        });
    }
}

/// Accept forever and hand each plain TCP stream to `handler`, for `AWM_EDGE_TLS=0`.
pub async fn serve_plain<F, Fut>(listener: TcpListener, handler: F)
where
    F: Fn(TcpStream, SocketAddr) -> Fut + Clone + Send + Sync + 'static,
    Fut: Future<Output = ()> + Send + 'static,
{
    loop {
        let (stream, peer) = accept_next(&listener).await;
        let handler = handler.clone();
        tokio::spawn(async move { handler(stream, peer).await });
    }
}

/// A stream either loop hands out, for a handler that serves both.
pub trait EdgeStream: AsyncRead + AsyncWrite + Unpin + Send + 'static {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send + 'static> EdgeStream for T {}
