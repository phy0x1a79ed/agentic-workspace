//! Push the board to the vault on deneb (fleet.md R79): deneb makes no outbound call, so alcor
//! carries each card change and notice to it. A failed push waits for the next event, which
//! resumes from the vault's own mark, so nothing is lost and nothing retries on a timer.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use awm_plugin_sdk::{CallOpts, Hub};
use serde_json::{json, Value};
use tokio::sync::Notify;

use crate::relay::async_trait_free::BoxFut;
use crate::store::Store;

const PAGE: i64 = 50;

/// `(verb, args)` on the vault.
pub trait Vault: Send + Sync {
    fn push<'a>(&'a self, verb: &'a str, args: Value) -> BoxFut<'a, Result<Value, String>>;
}

pub struct HubVault {
    pub hub: Hub,
}

impl Vault for HubVault {
    fn push<'a>(&'a self, verb: &'a str, args: Value) -> BoxFut<'a, Result<Value, String>> {
        Box::pin(async move {
            let opts = CallOpts { timeout: Duration::from_secs(120), ..CallOpts::default() };
            self.hub.call_with("trilium", verb, args, &opts).await.map_err(|e| e.to_string())
        })
    }
}

pub struct Pusher {
    store: Arc<Store>,
    vault: Arc<dyn Vault>,
    wake: Notify,
    pushed_notices: Mutex<Option<Value>>,
}

fn mark_of(answer: &Value) -> Result<i64, String> {
    answer["mark"].as_i64().ok_or_else(|| format!("the vault answered no mark: {answer}"))
}

impl Pusher {
    /// A pusher that wakes on each card event and notice, and once at the start.
    pub fn attach(store: Arc<Store>, vault: Arc<dyn Vault>) -> Arc<Self> {
        let me = Arc::new(Self { store: store.clone(), vault, wake: Notify::new(), pushed_notices: Mutex::new(None) });
        let w = Arc::downgrade(&me);
        store.set_on_event(Arc::new(move |_| {
            if let Some(p) = w.upgrade() {
                p.wake.notify_one();
            }
        }));
        let w = Arc::downgrade(&me);
        store.set_on_notice(Arc::new(move || {
            if let Some(p) = w.upgrade() {
                p.wake.notify_one();
            }
        }));
        me.wake.notify_one();
        me
    }

    pub async fn run(self: Arc<Self>) {
        loop {
            self.wake.notified().await;
            if let Err(e) = self.pass().await {
                tracing::warn!("the vault push failed, and the next event retries it: {e}");
            }
        }
    }

    /// Bring the vault up to the store: every card page after its mark, then the notices if
    /// they changed.
    pub async fn pass(&self) -> Result<(), String> {
        self.cards().await?;
        self.notices().await
    }

    async fn cards(&self) -> Result<(), String> {
        let mut mark = mark_of(&self.vault.push("board_apply", json!({})).await?)?;
        loop {
            let store = self.store.clone();
            let page = tokio::task::spawn_blocking(move || store.board_changes(mark, Some(PAGE)))
                .await
                .map_err(|e| e.to_string())?
                .map_err(|e| e.to_string())?;
            let next = mark_of(&page)?;
            if next == mark {
                return Ok(());
            }
            let body = json!({"base": mark, "cards": page["cards"], "mark": next});
            let answered = mark_of(&self.vault.push("board_apply", body).await?)?;
            if answered == next && page["more"] != json!(true) {
                return Ok(());
            }
            mark = answered;
        }
    }

    async fn notices(&self) -> Result<(), String> {
        let store = self.store.clone();
        let list = tokio::task::spawn_blocking(move || store.ops_list())
            .await
            .map_err(|e| e.to_string())?
            .map_err(|e| e.to_string())?;
        let notices = list["notes"].clone();
        if self.pushed_notices.lock().expect("notices lock").as_ref() == Some(&notices) {
            return Ok(());
        }
        self.vault.push("ops_render", json!({"notices": notices})).await?;
        *self.pushed_notices.lock().expect("notices lock") = Some(notices);
        Ok(())
    }
}
