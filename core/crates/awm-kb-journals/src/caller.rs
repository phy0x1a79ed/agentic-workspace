//! Who calls, and what they may read, write and see.
//!
//! The bound follows the calling session's trust level. A `trusted` caller is unbounded. A
//! cooperative or untrusted caller needs its own scope: cooperative reads this node and writes
//! only its own scope, untrusted reads and writes only its own scope. A call over an edge is
//! bound to the stamped peer node, so no cooperative or untrusted peer reads here.
//!
//! The private marks hide a channel, and a post tagged with a marked project's name, from a
//! peer caller only. The gateway filters a peer's args and emits but never a result, so the
//! journal applies the marks itself.

use std::collections::BTreeSet;

use awm_plugin_sdk::{privacy, Call, Lane};
use awm_pycompat::pystr::norm;
use awm_trust::JournalBound;
use serde_json::Value;

use crate::posts::{Names, Post};
use crate::{Fail, Journals, Result};

/// The caller of one verb, as the SDK stamps it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Who {
    /// The `X-Awm-As` principal: `peer:<node>` over an edge.
    pub as_: Option<String>,
    /// The trust level as the gateway wrote it; none is untrusted.
    pub trust: Option<String>,
    /// The calling session's own `<project>/<scope>`.
    pub scope: Option<String>,
    /// The calling session's R9 id, for a local caller only.
    pub session: Option<String>,
    pub lane: Lane,
}

impl Who {
    /// A trusted local caller with no session, as a plain shell is.
    pub fn trusted() -> Who {
        Who { trust: Some("trusted".into()), ..Who::default() }
    }

    pub fn from_call(call: &Call) -> Who {
        let arg = |k: &str| call.args.get(k).and_then(Value::as_str).map(str::to_string);
        let session = match call.caller.lane {
            Lane::Local => call.caller.session.as_deref().and_then(|s| awm_plugin_sdk::ids::SessionId::parse(s).ok()).map(|i| i.canonical()),
            _ => None,
        };
        Who {
            as_: call.as_.clone(),
            trust: call.caller.trust.clone().or_else(|| arg(awm_plugin_sdk::service::TRUST_KEY)),
            scope: call.caller.scope.clone().or_else(|| arg(awm_plugin_sdk::service::SCOPE_KEY)),
            session,
            lane: call.caller.lane,
        }
    }

    pub fn as_(&self) -> Option<&str> {
        self.as_.as_deref()
    }

    pub fn peer(&self) -> bool {
        privacy::from_peer(self.as_())
    }

    pub fn with_as(mut self, as_: Option<&str>) -> Who {
        self.as_ = as_.map(str::to_string);
        self
    }
}

/// Whether a mark covers the channel `(owner_project, owner_scope)`.
pub fn owner_hidden(project: &str, scope: &str) -> bool {
    if project.is_empty() {
        if let Some(p) = scope.strip_prefix("project:") {
            return privacy::hidden(p, None);
        }
    }
    privacy::hidden(project, Some(scope).filter(|s| !s.is_empty()))
}

/// Whether the caller `as_` may not see the channel.
pub fn hides(as_: Option<&str>, project: &str, scope: &str) -> bool {
    privacy::from_peer(as_) && owner_hidden(project, scope)
}

/// What a peer may not see, read once per call: by channel, or by a tag named for a marked
/// project. Unreadable marks hide everything.
pub enum Exclusion {
    Nothing,
    Everything,
    Marks(BTreeSet<String>),
}

impl Exclusion {
    pub fn of(as_: Option<&str>) -> Exclusion {
        if !privacy::from_peer(as_) {
            return Exclusion::Nothing;
        }
        match privacy::marks() {
            Err(_) => Exclusion::Everything,
            Ok(m) => Exclusion::Marks(m.iter().map(|m| norm(&m.project)).collect()),
        }
    }

    pub fn hides(&self, p: &Post, names: &Names) -> bool {
        match self {
            Exclusion::Nothing => false,
            Exclusion::Everything => true,
            Exclusion::Marks(projects) => {
                owner_hidden(&p.project, &p.scope)
                    || p.tag_ids.iter().filter_map(|t| names.get(t)).any(|n| projects.contains(&norm(n)))
            }
        }
    }
}

/// `private.hides_post`.
pub fn hides_post(as_: Option<&str>, p: &Post, names: &Names) -> bool {
    if !privacy::from_peer(as_) {
        return false;
    }
    if owner_hidden(&p.project, &p.scope) {
        return true;
    }
    match privacy::marks() {
        Err(_) => !p.tag_ids.is_empty(),
        Ok(m) => {
            let projects: BTreeSet<String> = m.iter().map(|m| norm(&m.project)).collect();
            p.tag_ids.iter().filter_map(|t| names.get(t)).any(|n| projects.contains(&norm(n)))
        }
    }
}

fn unbounded() -> JournalBound {
    JournalBound { read_nodes: None, read_scope: None, write_scope: None }
}

impl Journals {
    /// `bounds.of`: the caller's bound.
    pub fn bound(&self, who: &Who) -> Result<JournalBound> {
        let level = who.trust.clone().filter(|t| !t.is_empty()).unwrap_or_else(|| "untrusted".into());
        if level == "trusted" {
            return Ok(unbounded());
        }
        let stamp = who.as_().unwrap_or("").trim();
        let node = if stamp.to_lowercase().starts_with("peer:") {
            awm_plugin_sdk::config::label(&stamp[5..])
        } else {
            self.here()
        };
        let own = who.scope.clone().unwrap_or_default();
        let (project, scope) = own.split_once('/').unwrap_or((own.as_str(), ""));
        if project.is_empty() || scope.is_empty() {
            return Err(Fail::refused(format!(
                "a {level} session reaches the journal only from its own scope, and none came with this call"
            )));
        }
        let lvl: awm_trust::Level = level.parse().unwrap_or(awm_trust::Level::Untrusted);
        Ok(awm_trust::journal_bound(&self.edges(), lvl, &node, project, scope))
    }

    pub fn check_read(&self, bound: &JournalBound, project: &str, scope: &str) -> Result<()> {
        let here = self.here();
        if !bound.may_read(&here, Some(project), Some(scope)) {
            return Err(Fail::refused(format!("this session may not read {project}/{scope} on {here}")));
        }
        Ok(())
    }

    pub fn check_write(&self, bound: &JournalBound, project: &str, scope: &str) -> Result<()> {
        self.check_read(bound, project, scope)?;
        if !bound.may_write(Some(project), Some(scope)) {
            return Err(Fail::refused(format!(
                "this session writes only under {}",
                bound.write_scope.clone().unwrap_or_else(|| "None".into())
            )));
        }
        Ok(())
    }

    /// `bounds.narrow`: the project and scope a fetch may cover.
    pub fn narrow(&self, bound: &JournalBound, project: Option<String>, scope: Option<String>) -> Result<(Option<String>, Option<String>)> {
        let here = self.here();
        if bound.read_nodes.as_ref().is_some_and(|n| !n.contains(&here)) {
            return Err(Fail::refused(format!("this session may not read the journals on {here}")));
        }
        let Some(read) = &bound.read_scope else { return Ok((project, scope)) };
        let (own_p, own_s) = read.split_once('/').unwrap_or((read.as_str(), ""));
        let p = project.clone().filter(|p| !p.is_empty()).unwrap_or_else(|| own_p.to_string());
        let s = scope.clone().filter(|s| !s.is_empty()).unwrap_or_else(|| own_s.to_string());
        if p != own_p || s != own_s {
            return Err(Fail::refused(format!("this session reads only {read}")));
        }
        Ok((Some(own_p.to_string()), Some(own_s.to_string())))
    }

    /// Whether this caller may see a post: its bound reaches the channel, and no mark hides it.
    pub fn visible(&self, bound: &JournalBound, who: &Who, p: &Post, names: &Names) -> bool {
        bound.may_read(&self.here(), Some(&p.project), Some(&p.scope)) && !hides_post(who.as_(), p, names)
    }

    /// Refuse a peer's read while a tag clash stands between it and this node.
    pub fn refuse_clashed(&self, who: &Who) -> Result<()> {
        let peer = if who.peer() { awm_plugin_sdk::config::caller_peer(who.as_()) } else { None };
        if self.blocked(peer)? {
            return Err(Fail::permission(format!(
                "a tag clash stands between {} and this node; reads between them wait until an agent renames the newer tag",
                peer.unwrap_or("")
            )));
        }
        Ok(())
    }
}
