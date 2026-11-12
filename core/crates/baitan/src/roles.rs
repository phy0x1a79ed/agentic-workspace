//! The long-running roles: the market server, a provider over stdio, a Slurm front over stdio, and `connect`.

use crate::client::Client;
use baitan_carrier::{dial, stdio, unix_connect, unix_listen, Link};
use baitan_market::Market;
use baitan_provider::{Provider, ProviderConfig};
use baitan_slurm::{SlurmBackend, SlurmConfig};
use serde::Deserialize;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

pub fn baitan_dir() -> PathBuf {
    PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".baitan")
}

pub fn default_socket() -> PathBuf {
    std::env::var_os("BAITAN_SOCKET").map(PathBuf::from).unwrap_or_else(|| baitan_dir().join("market.sock"))
}

fn hostname() -> String {
    std::fs::read_to_string("/proc/sys/kernel/hostname").map(|h| h.trim().to_string()).unwrap_or_else(|_| "provider".into())
}

fn read_toml<T: for<'de> Deserialize<'de> + Default>(path: &Path) -> anyhow::Result<T> {
    match std::fs::read_to_string(path) {
        Ok(text) => Ok(toml::from_str(&text)?),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(T::default()),
        Err(e) => Err(e.into()),
    }
}

/// `~/.baitan/baitan.toml`: where the market listens and the provider links it dials.
#[derive(Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
pub struct MarketConfig {
    pub socket: Option<PathBuf>,
    /// Each a shell command whose stdio is one provider link, e.g. `ssh capella baitan provider`.
    pub links: Vec<String>,
}

impl MarketConfig {
    pub fn load() -> anyhow::Result<Self> {
        read_toml(&baitan_dir().join("baitan.toml"))
    }
}

/// Runs the market: listens on the socket and keeps every configured link dialled.
pub async fn serve(market: Market, cfg: MarketConfig) -> anyhow::Result<()> {
    let socket = cfg.socket.clone().unwrap_or_else(default_socket);
    if let Some(dir) = socket.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let _ = std::fs::remove_file(&socket);
    let listener = unix_listen(&socket)?;
    for cmd in cfg.links {
        tokio::spawn(keep_dialled(market.clone(), cmd));
    }
    loop {
        let Link { tx, rx } = listener.accept().await?;
        let m = market.clone();
        tokio::spawn(async move { m.serve(tx, rx).await });
    }
}

/// Redials a dropped link with backoff. Nothing announces that a remote host came back, so this
/// is a timer; links use `ssh -o BatchMode=yes`, so a failed dial never raises a 2FA prompt.
async fn keep_dialled(market: Market, cmd: String) {
    let mut wait = Duration::from_secs(30);
    loop {
        let started = tokio::time::Instant::now();
        match dial(&cmd) {
            Ok(Link { tx, rx }) => market.serve(tx, rx).await,
            Err(e) => eprintln!("baitan: dial {cmd}: {e}"),
        }
        if started.elapsed() > Duration::from_secs(600) {
            wait = Duration::from_secs(30);
        }
        tokio::time::sleep(wait).await;
        wait = (wait * 2).min(Duration::from_secs(1800));
    }
}

/// `~/.baitan/provider.toml`: a `ProviderConfig` plus index YAMLs to import at start.
#[derive(Deserialize, Default)]
#[serde(default)]
struct ProviderFile {
    #[serde(flatten)]
    provider: ProviderConfig,
    imports: Vec<PathBuf>,
}

pub async fn provider(config: Option<PathBuf>) -> anyhow::Result<()> {
    let file: ProviderFile = read_toml(&config.unwrap_or_else(|| baitan_dir().join("provider.toml")))?;
    let mut cfg = file.provider;
    if cfg.id.is_empty() {
        cfg.id = hostname();
    }
    let p = Provider::local(cfg);
    for index in &file.imports {
        p.import(index)?;
    }
    baitan_provider::serve(vec![p], stdio()).await
}

/// `~/.baitan/front.toml`: one `[[cluster]]` per Slurm cluster this front registers.
#[derive(Deserialize, Default)]
#[serde(default)]
struct FrontFile {
    cluster: Vec<FrontCluster>,
}

#[derive(Deserialize)]
struct FrontCluster {
    provider: ProviderConfig,
    slurm: SlurmConfig,
    #[serde(default)]
    imports: Vec<PathBuf>,
}

pub async fn front(config: Option<PathBuf>, only: Option<String>) -> anyhow::Result<()> {
    let file: FrontFile = read_toml(&config.unwrap_or_else(|| baitan_dir().join("front.toml")))?;
    let mut providers = Vec::new();
    for c in file.cluster.into_iter().filter(|c| only.as_ref().is_none_or(|o| *o == c.slurm.cluster)) {
        let (tx, rx) = tokio::sync::mpsc::unbounded_channel();
        let p = Provider::new(c.provider, Arc::new(SlurmBackend::new(c.slurm, tx)), rx);
        for index in &c.imports {
            p.import(index)?;
        }
        providers.push(p);
    }
    anyhow::ensure!(!providers.is_empty(), "front.toml names no matching [[cluster]]");
    baitan_provider::serve(providers, stdio()).await
}

/// Bridges this process's stdio to the market socket, so `ssh <host> baitan connect` is a link.
pub async fn connect(socket: PathBuf) -> anyhow::Result<()> {
    let stream = tokio::net::UnixStream::connect(&socket).await?;
    let (mut r, mut w) = stream.into_split();
    let (mut i, mut o) = (tokio::io::stdin(), tokio::io::stdout());
    tokio::select! {
        res = tokio::io::copy(&mut i, &mut w) => { res?; }
        res = tokio::io::copy(&mut r, &mut o) => { res?; }
    }
    Ok(())
}

/// A caller's link: `BAITAN_CONNECT` names a command to dial (e.g. `ssh mizar baitan connect`),
/// else the local market socket.
pub async fn client(name: &str) -> anyhow::Result<Client> {
    let link = match std::env::var("BAITAN_CONNECT") {
        Ok(cmd) if !cmd.is_empty() => dial(&cmd)?,
        _ => unix_connect(default_socket()).await?,
    };
    Ok(Client::new(name, link.tx, link.rx))
}

/// A caller attached to a market in this process.
pub fn local_client(market: &Market, name: &str) -> Client {
    let (to_market, from_client) = tokio::sync::mpsc::unbounded_channel();
    let (to_client, from_market) = tokio::sync::mpsc::unbounded_channel();
    let m = market.clone();
    tokio::spawn(async move { m.serve(to_client, from_client).await });
    Client::new(name, to_market, from_market)
}
