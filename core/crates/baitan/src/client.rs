//! A caller's end of a link to the market: asks matched to replies, events on a channel.

use baitan_proto::{Envelope, Msg};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use tokio::sync::{mpsc, oneshot};

type Pending = Arc<Mutex<HashMap<String, oneshot::Sender<Msg>>>>;

pub struct Client {
    name: String,
    tx: mpsc::UnboundedSender<Envelope>,
    pending: Pending,
    events: Mutex<Option<mpsc::UnboundedReceiver<Msg>>>,
}

impl Client {
    pub fn new(name: &str, tx: mpsc::UnboundedSender<Envelope>, mut rx: mpsc::UnboundedReceiver<Envelope>) -> Self {
        let pending: Pending = Arc::default();
        let (ev_tx, ev_rx) = mpsc::unbounded_channel();
        let p = pending.clone();
        tokio::spawn(async move {
            while let Some(env) = rx.recv().await {
                let Ok(msg) = env.msg() else { continue };
                match env.reply_to.as_ref().and_then(|id| p.lock().unwrap().remove(id)) {
                    Some(waiter) => {
                        let _ = waiter.send(msg);
                    }
                    None => {
                        let _ = ev_tx.send(msg);
                    }
                }
            }
            p.lock().unwrap().clear();
        });
        Client { name: name.into(), tx, pending, events: Mutex::new(Some(ev_rx)) }
    }

    pub async fn ask(&self, msg: Msg) -> Result<Msg, String> {
        let env = Envelope::new(&self.name, "market", &msg);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().unwrap().insert(env.id.clone(), tx);
        self.tx.send(env).map_err(|_| "market link closed".to_string())?;
        rx.await.map_err(|_| "market link closed".to_string())
    }

    /// The sends that are not replies (`event`s after a `watch`). Taken once.
    pub fn events(&self) -> Option<mpsc::UnboundedReceiver<Msg>> {
        self.events.lock().unwrap().take()
    }
}
