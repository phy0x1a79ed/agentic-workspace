//! The events installed routines listen for: one supervised subscription per distinct
//! `<service>.<topic>`, on this node. An event missed while the service is down is not replayed.

use std::collections::{BTreeSet, HashMap};
use std::sync::{Arc, Mutex};

use awm_plugin_sdk::{Hub, SubscriptionOpts, SupervisedSubscription};
use serde_json::Value;

use crate::runner::{row_events, Runner};

pub struct Events {
    hub: Hub,
    runner: Arc<Runner>,
    subs: Mutex<HashMap<String, SupervisedSubscription>>,
}

impl Events {
    pub fn new(hub: Hub, runner: Arc<Runner>) -> Arc<Self> {
        Arc::new(Self { hub, runner, subs: Mutex::default() })
    }

    /// Make the open subscriptions match the installed routines' events.
    pub fn reconcile(&self) {
        let wanted: BTreeSet<String> = match self.runner.store.list_routines(None) {
            Ok(rows) => rows.iter().flat_map(row_events).map(str::to_string).collect(),
            Err(e) => return tracing::error!("routine events: cannot list routines: {e}"),
        };
        let mut subs = self.subs.lock().unwrap_or_else(|p| p.into_inner());
        subs.retain(|topic, sub| {
            let keep = wanted.contains(topic);
            if !keep {
                sub.abort();
            }
            keep
        });
        for topic in wanted {
            if subs.contains_key(&topic) {
                continue;
            }
            let Some((service, name)) = topic.split_once('.') else { continue };
            let (service, name) = (service.to_string(), name.to_string());
            let hub = self.hub.clone();
            let runner = self.runner.clone();
            let t = topic.clone();
            let sub = SupervisedSubscription::spawn(
                &format!("routine/{topic}"),
                SubscriptionOpts::default(),
                move || {
                    let (hub, service, name) = (hub.clone(), service.clone(), name.clone());
                    async move { hub.subscribe(&service, &name, None, None).await }
                },
                move |event: Value| {
                    runner.on_event(&t, &event);
                    async { Ok::<(), anyhow::Error>(()) }
                },
            );
            subs.insert(topic, sub);
        }
    }
}
