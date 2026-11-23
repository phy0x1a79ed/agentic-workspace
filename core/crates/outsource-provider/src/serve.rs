//! Hosts a node's providers for every entry that attaches, and keeps the vocabulary they share.

use crate::provider::Provider;
use outsource_carrier::{Link, Listener};
use outsource_proto::vocab::Vocab;
use outsource_proto::{Envelope, Msg, VocabArgs};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use tokio::sync::mpsc::{unbounded_channel, UnboundedSender};

const FROM: &str = "providers";

#[derive(Clone)]
pub struct Hosted {
    providers: Arc<Vec<Provider>>,
    entries: Arc<Mutex<HashMap<String, UnboundedSender<Envelope>>>>,
    vocab: Arc<Mutex<Vocab>>,
}

impl Hosted {
    pub fn new(providers: Vec<Provider>, vocab: Vocab) -> Hosted {
        Hosted { providers: Arc::new(providers), entries: Arc::default(), vocab: Arc::new(Mutex::new(vocab)) }
    }

    pub async fn listen(self, listener: Listener) -> std::io::Result<()> {
        loop {
            let link = listener.accept().await?;
            tokio::spawn(self.clone().serve(link));
        }
    }

    /// An entry's end of a link to these providers in this process.
    pub fn join(&self) -> Link {
        let ((to_host, host_rx), (host_tx, from_host)) = (unbounded_channel(), unbounded_channel());
        tokio::spawn(self.clone().serve(Link { tx: host_tx, rx: host_rx }));
        Link { tx: to_host, rx: from_host }
    }

    /// Serves one entry's link until it drops. Running jobs and their promises survive; the entry
    /// that attaches again under the same name gets their settles.
    pub async fn serve(self, mut link: Link) {
        let mut entry: Option<String> = None;
        while let Some(env) = link.rx.recv().await {
            match env.msg() {
                Ok(Msg::Vocab(a)) => {
                    let first = entry.is_none();
                    if first {
                        entry = Some(env.from.clone());
                        self.entries.lock().unwrap().insert(env.from.clone(), link.tx.clone());
                        for p in self.providers.iter() {
                            p.attach(&env.from, link.tx.clone());
                        }
                    }
                    self.merge(&env.from, &a, first);
                    if first {
                        for p in self.providers.iter() {
                            p.register(&env.from);
                        }
                    }
                }
                _ => match self.providers.iter().find(|p| p.id() == env.to) {
                    Some(p) => p.handle(env).await,
                    None => tracing::warn!("no provider {} here (selector {})", env.to, env.selector),
                },
            }
        }
        if let Some(name) = entry {
            for p in self.providers.iter() {
                p.detach(&name, &link.tx);
            }
            let mut entries = self.entries.lock().unwrap();
            if entries.get(&name).is_some_and(|t| t.same_channel(&link.tx)) {
                entries.remove(&name);
            }
        }
    }

    /// Joins an entry's vocabulary in. A change goes to every entry; a new entry gets the result.
    fn merge(&self, from: &str, a: &VocabArgs, first: bool) {
        let (changed, properties) = {
            let mut v = self.vocab.lock().unwrap();
            let changed = v.merge(&a.properties).unwrap_or_else(|e| {
                tracing::warn!("{e}");
                true
            });
            (changed, v.keys().clone())
        };
        let msg = Msg::Vocab(VocabArgs { properties });
        for (name, tx) in self.entries.lock().unwrap().iter().filter(|(name, _)| changed || (first && name.as_str() == from)) {
            let _ = tx.send(Envelope::new(FROM, name, &msg));
        }
    }
}
