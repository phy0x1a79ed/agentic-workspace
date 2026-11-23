//! The long-running roles: the market server, a provider over stdio, and `connect`.

use crate::client::Client;
use outsource_carrier::{dial, stdio, unix_connect, unix_listen, Link};
use outsource_market::Market;
use outsource_provider::{Provider, ProviderConfig};
use outsource_slurm::{SlurmBackend, SlurmConfig};
use serde::Deserialize;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

pub fn outsource_dir() -> PathBuf {
    PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".outsource")
}

pub fn default_socket() -> PathBuf {
    std::env::var_os("OUTSOURCE_SOCKET").map(PathBuf::from).unwrap_or_else(|| outsource_dir().join("market.sock"))
}

pub(crate) fn hostname() -> String {
    std::fs::read_to_string("/proc/sys/kernel/hostname").map(|h| h.trim().to_string()).unwrap_or_else(|_| "provider".into())
}

fn read_toml<T: for<'de> Deserialize<'de> + Default>(path: &Path) -> anyhow::Result<T> {
    match std::fs::read_to_string(path) {
        Ok(text) => Ok(toml::from_str(&text)?),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(T::default()),
        Err(e) => Err(e.into()),
    }
}

/// `~/.outsource/outsource.toml`: where the market listens and the provider links it dials.
#[derive(Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
pub struct MarketConfig {
    pub socket: Option<PathBuf>,
    /// Each a shell command whose stdio is one provider link, e.g. `ssh capella outsource provider`.
    pub links: Vec<String>,
}

impl MarketConfig {
    pub fn load() -> anyhow::Result<Self> {
        read_toml(&outsource_dir().join("outsource.toml"))
    }
}

/// Runs the market: listens on the socket and keeps every configured link dialled.
pub async fn serve(market: Market, cfg: MarketConfig) -> anyhow::Result<()> {
    let socket = cfg.socket.clone().unwrap_or_else(default_socket);
    market.keep_vocab(outsource_dir().join("vocab.json"))?;
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
            Err(e) => eprintln!("outsource: dial {cmd}: {e}"),
        }
        if started.elapsed() > Duration::from_secs(600) {
            wait = Duration::from_secs(30);
        }
        tokio::time::sleep(wait).await;
        wait = (wait * 2).min(Duration::from_secs(1800));
    }
}

/// `~/.outsource/provider.toml`: this host's `ProviderConfig`, index YAMLs to import at start, and
/// one `[[cluster]]` per Slurm cluster this process fronts over ssh.
#[derive(Deserialize, Default)]
#[serde(default)]
struct ProviderFile {
    #[serde(flatten)]
    provider: ProviderConfig,
    imports: Vec<PathBuf>,
    cluster: Vec<Cluster>,
}

/// One provider per cluster, so the market's picker, reviews and success rates stay per cluster.
#[derive(Deserialize)]
struct Cluster {
    provider: ProviderConfig,
    slurm: SlurmConfig,
    #[serde(default)]
    imports: Vec<PathBuf>,
}

pub async fn provider(config: Option<PathBuf>) -> anyhow::Result<()> {
    let file: ProviderFile = read_toml(&config.unwrap_or_else(|| outsource_dir().join("provider.toml")))?;
    let mut cfg = file.provider;
    if cfg.id.is_empty() {
        cfg.id = hostname();
    }
    let mut providers = vec![(Provider::local(cfg), file.imports)];
    for c in file.cluster {
        let (tx, rx) = tokio::sync::mpsc::unbounded_channel();
        let saved = c.provider.resolved_state_dir().join("jobs.json");
        let backend = SlurmBackend::new(c.slurm, c.provider.host(), saved, tx);
        providers.push((Provider::new(c.provider, Arc::new(backend), rx), c.imports));
    }
    // An unreachable cluster must not take the other providers down; its last import stays in state.json.
    for (p, imports) in &providers {
        for index in imports {
            if let Err(e) = p.import(index).await {
                eprintln!("outsource: {}: import {}: {e:#}", p.id(), index.display());
            }
        }
    }
    outsource_provider::serve(providers.into_iter().map(|(p, _)| p).collect(), stdio()).await
}

/// Bridges this process's stdio to the market socket, so `ssh <host> outsource connect` is a link.
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

/// A caller's link: `OUTSOURCE_CONNECT` names a command to dial (e.g. `ssh mizar outsource connect`),
/// else the local market socket.
pub async fn client(name: &str) -> anyhow::Result<Client> {
    let link = match std::env::var("OUTSOURCE_CONNECT") {
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
