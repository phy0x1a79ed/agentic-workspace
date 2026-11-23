//! The long-running roles: a node's service (its entry and its providers), `attach`, and callers' links.

use crate::client::Client;
use outsource_carrier::{dial, unix_connect, unix_listen, Link};
use outsource_market::Market;
use outsource_proto::vocab::Vocab;
use outsource_provider::{Hosted, Provider, ProviderConfig};
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

pub fn provider_socket() -> PathBuf {
    outsource_dir().join("provider.sock")
}

pub(crate) fn hostname() -> String {
    std::fs::read_to_string("/proc/sys/kernel/hostname").map(|h| h.trim().to_string()).unwrap_or_else(|_| "provider".into())
}

/// `OUTSOURCE_NODE`, else the hostname a provider takes as its default id.
pub fn node() -> String {
    std::env::var("OUTSOURCE_NODE").ok().filter(|n| !n.is_empty()).unwrap_or_else(hostname)
}

fn read_toml<T: for<'de> Deserialize<'de> + Default>(path: &Path) -> anyhow::Result<T> {
    match std::fs::read_to_string(path) {
        Ok(text) => Ok(toml::from_str(&text)?),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(T::default()),
        Err(e) => Err(e.into()),
    }
}

/// `~/.outsource/outsource.toml`: where the entry listens for callers, and for a standalone
/// `serve`, the attach commands of the other nodes it reaches.
#[derive(Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
pub struct MarketConfig {
    pub socket: Option<PathBuf>,
    /// Each a shell command whose stdio is a node's providers, e.g. `ssh altair outsource attach`.
    pub links: Vec<String>,
}

impl MarketConfig {
    pub fn load() -> anyhow::Result<Self> {
        read_toml(&outsource_dir().join("outsource.toml"))
    }
}

/// Runs this node's service: its providers on the provider socket, and its entry, which reaches
/// them in process and every other node's providers through `links`, and listens for callers.
pub async fn serve(market: Market, socket: PathBuf, links: Vec<String>) -> anyhow::Result<()> {
    let hosted = providers().await?;
    let plistener = unix_listen(provider_socket())?;
    tokio::spawn(hosted.clone().listen(plistener));
    let Link { tx, rx } = hosted.join();
    let m = market.clone();
    tokio::spawn(async move { m.join(tx, rx).await });
    for cmd in links {
        tokio::spawn(keep_dialled(market.clone(), cmd));
    }
    let listener = unix_listen(&socket)?;
    loop {
        let Link { tx, rx } = listener.accept().await?;
        let m = market.clone();
        tokio::spawn(async move { m.serve(tx, rx).await });
    }
}

/// Redials a dropped link forever, backing off to a minute. Nothing announces that a remote host
/// came back, so this is a timer; links use `ssh -o BatchMode=yes`, so a failed dial never raises
/// a 2FA prompt. A link that held for the cap counts as up, and only a change of state is logged.
async fn keep_dialled(market: Market, cmd: String) {
    const CAP: Duration = Duration::from_secs(60);
    let (mut wait, mut up) = (Duration::from_secs(1), true);
    loop {
        let started = tokio::time::Instant::now();
        let failure = match dial(&cmd) {
            Ok(Link { tx, rx }) => {
                market.join(tx, rx).await;
                "closed at once".to_string()
            }
            Err(e) => e.to_string(),
        };
        if started.elapsed() >= CAP {
            eprintln!("outsource: link {cmd} dropped after {}s; redialing", started.elapsed().as_secs());
            (wait, up) = (Duration::from_secs(1), true);
        } else if up {
            eprintln!("outsource: link {cmd} is down ({failure}); redialing at most every {}s", CAP.as_secs());
            up = false;
        }
        tokio::time::sleep(wait).await;
        wait = (wait * 2).min(CAP);
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

/// This node's providers from `~/.outsource/provider.toml`, sharing the vocabulary kept in `~/.outsource/vocab.json`.
async fn providers() -> anyhow::Result<Hosted> {
    let file: ProviderFile = read_toml(&outsource_dir().join("provider.toml"))?;
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
    let mut vocab = Vocab::seeded();
    vocab.keep(outsource_dir().join("vocab.json"))?;
    Ok(Hosted::new(providers.into_iter().map(|(p, _)| p).collect(), vocab))
}

/// Bridges this process's stdio to a socket, so `ssh <node> outsource attach` is a link to that node's providers.
pub async fn bridge(socket: PathBuf) -> anyhow::Result<()> {
    let stream = tokio::net::UnixStream::connect(&socket).await?;
    let (mut r, mut w) = stream.into_split();
    let (mut i, mut o) = (tokio::io::stdin(), tokio::io::stdout());
    tokio::select! {
        res = tokio::io::copy(&mut i, &mut w) => { res?; }
        res = tokio::io::copy(&mut r, &mut o) => { res?; }
    }
    Ok(())
}

/// A caller's link to this node's entry.
pub async fn client(name: &str) -> anyhow::Result<Client> {
    let link = unix_connect(default_socket()).await?;
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::offer::{compile, Site};
    use outsource_proto::{Msg, OfferArgs, OfferSet, Phase, PropertiesArgs, PropertyAddArgs, WatchArgs};
    use serde_json::json;
    use tokio::sync::mpsc::UnboundedReceiver;

    fn host(dir: &Path, providers: &[(&str, &[(&str, &str)])]) -> Hosted {
        let cfg = |id: &str, routes: &[(&str, &str)]| ProviderConfig {
            state_dir: dir.join(id),
            routes: routes.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect(),
            ..ProviderConfig::new(id)
        };
        Hosted::new(providers.iter().map(|(id, routes)| Provider::local(cfg(id, routes))).collect(), Vocab::seeded())
    }

    fn entry(name: &str, hosts: &[&Hosted]) -> Market {
        let market = Market::new(name);
        for h in hosts {
            let (Link { tx, rx }, m) = (h.join(), market.clone());
            tokio::spawn(async move { m.join(tx, rx).await });
        }
        market
    }

    async fn watching(market: &Market) -> (Client, UnboundedReceiver<Msg>) {
        let client = local_client(market, "caller");
        let events = client.events().unwrap();
        client.ask(Msg::Watch(WatchArgs { promises: vec![] })).await.unwrap();
        (client, events)
    }

    /// Posts `set` until a provider has registered to bid on it, and returns its final phase. Every
    /// event the client sees must be about a promise it posted.
    async fn settle(client: &Client, events: &mut UnboundedReceiver<Msg>, set: OfferSet) -> Phase {
        let mut posted = vec![];
        loop {
            let Ok(Msg::Return(r)) = client.ask(Msg::Offer(OfferArgs { set: set.clone(), promise: None, inbound: vec![] })).await else { panic!("offer refused") };
            posted.push(r["promise"].as_str().unwrap().to_string());
            let phase = loop {
                let Some(Msg::Event(s)) = events.recv().await else { panic!("market link closed") };
                assert!(posted.contains(&s.promise), "an event of another entry's promise reached this one");
                if s.promise == *posted.last().unwrap() && s.phase.is_final() {
                    break s.phase;
                }
            };
            if !matches!(&phase, Phase::Failed { message, .. } if message == "no eligible provider") {
                return phase;
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
    }

    fn job(to: &str, tag: &str) -> OfferSet {
        serde_json::from_value(json!({"steps": [{"op": "task", "props": {"contract": tag}, "slots": ["out"],
            "spec": {"script": format!("echo {tag} > out.txt"), "stage": {}, "outputs": ["out.txt"]}}], "to": to}))
        .unwrap()
    }

    fn settled_at(phase: &Phase, at: &str) -> bool {
        matches!(phase, Phase::Settled { provider, .. } if provider == at)
    }

    /// `far` has no route to `near`, which holds the input and the destination, but `near` reaches
    /// `far`: `near` pushes the input and pulls the result, both through the entry.
    #[tokio::test]
    async fn a_holder_pushes_to_a_provider_that_cannot_reach_it() {
        let d = tempfile::tempdir().unwrap();
        let market = entry("market", &[&host(d.path(), &[("near", &[("far", "local")]), ("far", &[])])]);
        let work = d.path().join("work");
        std::fs::create_dir_all(&work).unwrap();
        std::fs::write(work.join("in.txt"), "data").unwrap();
        let site = Site { node: "near".into(), cwd: Some(work.clone()) };
        let offer = json!({"requires": ["in.txt"], "produces": ["res"], "protocol": "mkdir res && cp in.txt res/copy.txt", "dest": "dest", "to": "far"});
        let set = compile(offer, &site).await.unwrap().set;
        let (client, mut events) = watching(&market).await;
        let phase = settle(&client, &mut events, set).await;
        assert!(settled_at(&phase, "far"), "{phase:?}");
        assert_eq!(std::fs::read_to_string(work.join("dest/res/copy.txt")).unwrap(), "data");
    }

    /// Entry `a` attaches to both nodes, `b` only to the shared one. Each entry's promises settle
    /// through the shared provider with only their own events, and `b` cannot reach `solo`.
    #[tokio::test]
    async fn entries_share_a_provider_and_reach_only_what_they_attach() {
        let d = tempfile::tempdir().unwrap();
        let (shared, solo) = (host(d.path(), &[("shared", &[])]), host(d.path(), &[("solo", &[])]));
        let (a, b) = (entry("a", &[&shared, &solo]), entry("b", &[&shared]));
        let ((ca, mut ea), (cb, mut eb)) = (watching(&a).await, watching(&b).await);
        let (pa, pb) = tokio::join!(settle(&ca, &mut ea, job("shared", "a")), settle(&cb, &mut eb, job("shared", "b")));
        assert!(settled_at(&pa, "shared") && settled_at(&pb, "shared"), "{pa:?} {pb:?}");
        assert!(settled_at(&settle(&ca, &mut ea, job("solo", "a2")).await, "solo"));
        let Ok(Msg::Return(r)) = cb.ask(Msg::Offer(OfferArgs { set: job("solo", "b2"), promise: None, inbound: vec![] })).await else { panic!("offer refused") };
        let id = r["promise"].as_str().unwrap();
        let phase = loop {
            let Some(Msg::Event(s)) = eb.recv().await else { panic!("market link closed") };
            if s.promise == id && s.phase.is_final() {
                break s.phase;
            }
        };
        assert!(matches!(&phase, Phase::Failed { message, .. } if message == "no eligible provider"), "{phase:?}");
    }

    #[tokio::test]
    async fn a_property_added_at_one_entry_reaches_another_through_a_shared_provider() {
        let d = tempfile::tempdir().unwrap();
        let shared = host(d.path(), &[("shared", &[])]);
        let (a, b) = (entry("a", &[&shared]), entry("b", &[&shared]));
        let (ca, cb) = (local_client(&a, "caller"), local_client(&b, "caller"));
        let add = PropertyAddArgs { name: "organism".into(), description: "source organism".into(), values: Some(vec!["Human".into()]) };
        // `a` shares only with providers that have registered there.
        loop {
            ca.ask(Msg::PropertyAdd(add.clone())).await.unwrap();
            let Ok(Msg::Return(v)) = cb.ask(Msg::Properties(PropertiesArgs {})).await else { panic!("properties") };
            if v["properties"]["organism"]["values"] == json!(["human"]) {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
    }
}
