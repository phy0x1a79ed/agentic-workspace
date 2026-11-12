//! Hosts one or more providers on one link: registers each, then routes envelopes by `to`.

use crate::provider::Provider;
use baitan_carrier::Link;

/// Returns when the link drops. Running jobs and their promises survive; call `serve` again with
/// the same providers on a new link to register them again.
pub async fn serve(providers: Vec<Provider>, mut link: Link) -> anyhow::Result<()> {
    for p in &providers {
        p.attach(link.tx.clone());
        p.register();
    }
    while let Some(env) = link.rx.recv().await {
        match providers.iter().find(|p| p.id() == env.to) {
            Some(p) => p.handle(env).await,
            None => tracing::warn!("no provider {} on this link (selector {})", env.to, env.selector),
        }
    }
    for p in &providers {
        p.detach();
    }
    Ok(())
}
