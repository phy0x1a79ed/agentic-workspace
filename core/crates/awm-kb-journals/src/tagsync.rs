//! The fleet's one tag vocabulary: each node's copy, kept in step.
//!
//! An add or an edit goes to every peer by itself. Each start pulls every peer's vocabulary and
//! sends this node's, so a push that failed heals when either node next starts. A tag keeps its id everywhere; the higher version of an id wins. Two
//! nodes that add one name before they sync hold two concepts: on every node the newer tag takes
//! a provisional name, and the node that added it dispatches an agent to choose the real one.
//! While the clash stands the two nodes refuse each other's journal reads.
//!
//! A peer never learns a tag the private marks hide: one named for a marked project, or one
//! used only on posts in marked channels.

use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

use awm_plugin_sdk::privacy;
use awm_pycompat::pystr::norm;
use awm_pycompat::repr::str_repr;
use awm_pycompat::time::now_ms;
use awm_store::rusqlite::params;
use regex::Regex;
use serde_json::{json, Map, Value};

use crate::schema::BUILTINS;
use crate::tags::{normalize_name, Tag};
use crate::{caller, py_str, Fail, Journals, Result};

static PROVISIONAL_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\.clash-[0-9a-f]{6}$").unwrap());

pub fn provisional(name: &str) -> bool {
    PROVISIONAL_RE.is_match(name)
}

/// The name the newer tag of a clash holds until an agent renames it. Every node derives the same one.
pub fn provisional_name(name: &str, tag_id: &str) -> String {
    let hex: String = tag_id.to_lowercase().chars().filter(|c| c.is_ascii_digit() || ('a'..='f').contains(c)).collect();
    let mut digits: String = format!("{hex:0<6}");
    digits = digits.chars().take(6).collect();
    let suffix = format!(".clash-{digits}");
    let keep = 64usize.saturating_sub(suffix.chars().count());
    format!("{}{suffix}", awm_pycompat::pystr::head(name, keep))
}

fn created(v: &Value) -> i64 {
    match v.get("created_ms") {
        Some(Value::Number(n)) => n.as_i64().unwrap_or(0),
        Some(Value::String(s)) => s.trim().parse().unwrap_or(0),
        _ => 0,
    }
}

fn text(v: &Value, key: &str) -> String {
    v.get(key).filter(|x| crate::truthy(x)).map(py_str).unwrap_or_default()
}

/// Whether tag `a` was added after tag `b`; the id breaks a tie.
fn newer(a: &Value, b: &Value) -> bool {
    (created(a), text(a, "id")) > (created(b), text(b, "id"))
}

impl Journals {
    /// The tags a peer may not learn, by id. Unreadable marks hide every tag.
    pub async fn hidden_tags(&self) -> Result<Option<HashSet<String>>> {
        let marks = match privacy::marks() {
            Ok(m) => m,
            Err(_) => return Ok(None),
        };
        let names: HashSet<String> = marks.iter().map(|m| norm(&m.project)).collect();
        let mut hidden = HashSet::new();
        for t in self.vocabulary()? {
            if names.contains(&norm(&t.name)) {
                hidden.insert(t.id);
            }
        }
        if !marks.is_empty() {
            let mut used: HashMap<String, (bool, bool)> = HashMap::new();
            for p in self.scan("true").await? {
                let covered = caller::owner_hidden(&p.project, &p.scope);
                for t in &p.tag_ids {
                    let e = used.entry(t.clone()).or_insert((false, false));
                    e.0 = true;
                    e.1 |= !covered;
                }
            }
            hidden.extend(used.into_iter().filter(|(_, (any, open))| *any && !*open).map(|(t, _)| t));
        }
        Ok(Some(hidden))
    }

    /// Send one tag to every peer, unless the marks hide it from them.
    pub async fn push_tag(&self, tag: &Tag) {
        match self.hidden_tags().await {
            Ok(Some(h)) if !h.contains(&tag.id) => {}
            Ok(_) => return,
            Err(e) => {
                tracing::warn!("tag push skipped, the hidden tags are unknown: {e}");
                return;
            }
        }
        let out = self.outside();
        for peer in out.peers() {
            out.tag_receive(&peer, vec![tag.to_dict()]);
        }
    }

    /// Fold a peer's tags into this node's vocabulary; returns what changed.
    pub async fn merge_tags(&self, remote: &[Value], from_node: &str) -> Result<Value> {
        let (mut added, mut updated, mut clashes) = (0, 0, 0);
        for raw in remote {
            let Some(obj) = raw.as_object() else { continue };
            let id = obj.get("id").filter(|v| crate::truthy(v)).map(py_str);
            let raw_name = obj.get("name").filter(|v| crate::truthy(v));
            let (Some(id), Some(raw_name)) = (id, raw_name) else { continue };
            let mut name = match normalize_name(raw_name) {
                Ok(n) => n,
                Err(_) => {
                    tracing::warn!("tag {id} from {from_node} has a name this node refuses: {}", crate::py_repr(raw_name));
                    continue;
                }
            };
            let mut incoming: Map<String, Value> = obj.clone();
            incoming.insert("name".into(), json!(name));
            let description = obj.get("description").filter(|v| crate::truthy(v)).map(py_str).unwrap_or_else(|| name.clone());
            incoming.insert("description".into(), json!(description));
            let origin = obj.get("origin").filter(|v| crate::truthy(v)).map(py_str).unwrap_or_else(|| from_node.to_string());
            incoming.insert("origin".into(), json!(origin));
            let version = match obj.get("version").filter(|v| crate::truthy(v)) {
                Some(v) => crate::int_arg(&json!({"v": v}), "v", 1)?,
                None => 1,
            };
            incoming.insert("version".into(), json!(version));
            let incoming = Value::Object(incoming);
            let local = self.tag(&id)?;
            if local.as_ref().is_some_and(|l| version <= l.version) {
                continue;
            }
            let base = PROVISIONAL_RE.replace(&name, "").to_string();
            let holder = self.tag_by_name(&base)?;
            if let Some(h) = holder.filter(|h| h.id != id && !provisional(&name)) {
                clashes += 1;
                match self.clash(&h.to_dict(), &incoming).await? {
                    Some(n) => name = n,
                    None => continue,
                }
            } else if let Some(other) = self.tag_by_name(&name)?.filter(|o| o.id != id) {
                tracing::warn!("tag {id} from {from_node} names {}, which {} holds here", str_repr(&name), other.id);
                continue;
            }
            self.store_tag(&incoming, &name, local.is_some())?;
            if local.is_some() {
                updated += 1;
            } else {
                added += 1;
            }
            if local.is_some_and(|l| l.name != name) {
                self.resolved_by_edit(&id)?;
            }
        }
        Ok(json!({"added": added, "updated": updated, "clashes": clashes}))
    }

    fn store_tag(&self, tag: &Value, name: &str, existed: bool) -> Result<()> {
        let now = now_ms();
        let (tag, name) = (tag.clone(), name.to_string());
        self.db.blocking_write(move |tx| {
            let id = py_str(&tag["id"]);
            let description = py_str(&tag["description"]);
            let origin = py_str(&tag["origin"]);
            let version = tag["version"].as_i64().unwrap_or(1);
            if existed {
                tx.execute(
                    "UPDATE tags SET name=?, description=?, version=?, updated_at=? WHERE id=?",
                    params![name, description, version, now, id],
                )?;
            } else {
                let made = match created(&tag) {
                    0 => now,
                    c => c,
                };
                tx.execute(
                    "INSERT INTO tags (id, name, description, origin, version, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    params![id, name, description, origin, version, made, now],
                )?;
            }
            tx.execute(
                "INSERT OR IGNORE INTO tag_versions (tag_id, version, name, description, edited_at, edited_by) VALUES (?,?,?,?,?,?)",
                params![id, version, py_str(&tag["name"]), description, now, format!("sync:{origin}")],
            )?;
            Ok::<_, Fail>(())
        })
    }

    /// Record a clash between the tag here and one from a peer under one name. Returns the
    /// name to store the incoming tag under, or None when it should wait.
    async fn clash(&self, holder: &Value, incoming: &Value) -> Result<Option<String>> {
        let builtin = |v: &Value| BUILTINS.iter().any(|(id, _, _)| v["id"].as_str() == Some(id));
        if builtin(holder) && builtin(incoming) {
            return Ok(None);
        }
        let holder_newer = !newer(incoming, holder);
        let (newer_tag, older_tag) = if holder_newer { (holder, incoming) } else { (incoming, holder) };
        let base = py_str(&holder["name"]);
        let now = now_ms();
        let (b, o, n, hid) = (base.clone(), older_tag.clone(), newer_tag.clone(), py_str(&holder["id"]));
        self.db.blocking_write(move |tx| {
            tx.execute(
                "INSERT OR IGNORE INTO tag_clashes (name, older_id, newer_id, older_node, newer_node, detected_at) VALUES (?,?,?,?,?,?)",
                params![b, py_str(&o["id"]), py_str(&n["id"]), text(&o, "origin"), text(&n, "origin"), now],
            )?;
            if holder_newer {
                tx.execute("UPDATE tags SET name=?, updated_at=? WHERE id=?", params![provisional_name(&b, &hid), now, hid])?;
            }
            Ok::<_, Fail>(())
        })?;
        tracing::warn!(
            "tag clash on {}: {} ({}) is newer than {} ({})",
            str_repr(&base),
            py_str(&newer_tag["id"]),
            text(newer_tag, "origin"),
            py_str(&older_tag["id"]),
            text(older_tag, "origin")
        );
        if text(newer_tag, "origin") == self.node {
            self.dispatch_open().await?;
        }
        Ok(Some(if holder_newer { base } else { provisional_name(&base, &py_str(&incoming["id"])) }))
    }

    /// A rename of the newer tag of a clash settles it.
    pub fn resolved_by_edit(&self, tag_id: &str) -> Result<()> {
        let (id, now) = (tag_id.to_string(), now_ms());
        self.db.blocking_write(move |tx| {
            tx.execute("UPDATE tag_clashes SET resolved_at=? WHERE newer_id=? AND resolved_at IS NULL", params![now, id])?;
            Ok::<_, Fail>(())
        })
    }

    pub fn clashes(&self, open_only: bool) -> Result<Vec<Value>> {
        let sql = format!(
            "SELECT * FROM tag_clashes{} ORDER BY detected_at",
            if open_only { " WHERE resolved_at IS NULL" } else { "" }
        );
        self.db.blocking_read(move |c| {
            let mut stmt = c.prepare(&sql)?;
            Ok::<_, Fail>(awm_store::rows_json(&mut stmt, [])?)
        })
    }

    /// Whether an open clash stands between this node and `peer`.
    pub fn blocked(&self, peer: Option<&str>) -> Result<bool> {
        let Some(peer) = peer.filter(|p| !p.is_empty()) else { return Ok(false) };
        let pair: HashSet<&str> = [self.node.as_str(), peer].into();
        Ok(self.clashes(true)?.iter().any(|c| {
            let got: HashSet<&str> = [c["older_node"].as_str().unwrap_or(""), c["newer_node"].as_str().unwrap_or("")].into();
            got == pair
        }))
    }

    /// The prompt of the agent that renames the newer tag of a clash.
    pub fn clash_prompt(&self, c: &Value) -> Result<String> {
        let (newer_id, older_id) = (py_str(&c["newer_id"]), py_str(&c["older_id"]));
        let (newer_t, older_t) = (self.tag(&newer_id)?, self.tag(&older_id)?);
        let older_node = c["older_node"].as_str().filter(|s| !s.is_empty()).unwrap_or("a peer");
        let older_desc = older_t.as_ref().map(|t| t.description.clone()).unwrap_or_else(|| "(not here)".into());
        let newer_name = newer_t.as_ref().map(|t| t.name.clone()).unwrap_or_else(|| "?".into());
        let newer_desc = newer_t.as_ref().map(|t| t.description.clone()).unwrap_or_default();
        let newer_tagname = newer_t.as_ref().map(|t| t.name.clone()).unwrap_or_default();
        Ok(format!(
            "Two nodes added the journal tag name {} before they synced, so it names two concepts (fleet.md R107 item 6). \
The older tag {older_id} from {older_node} keeps the name: {}. The newer tag {newer_id} from this node is {} for now: {}. \
Read posts that carry it with journal(verb=\"fetch\", args={{tags:{}}}). Choose a name that says what the newer tag means \
and is not in tag_list, then run journal(verb=\"tag_edit\", args={{tag:\"{newer_id}\", name:<the name>}}). That rename \
reaches every node and ends the clash. Change nothing else, then stop.",
            str_repr(&py_str(&c["name"])),
            str_repr(&older_desc),
            str_repr(&newer_name),
            str_repr(&newer_desc),
            str_repr(&newer_tagname),
        ))
    }

    /// Dispatch an agent for each open clash whose newer tag this node added, once each.
    pub async fn dispatch_open(&self) -> Result<Vec<Value>> {
        let mut out = Vec::new();
        for c in self.clashes(true)? {
            if crate::truthy(&c["dispatched_at"]) || c["newer_node"].as_str() != Some(self.node.as_str()) {
                continue;
            }
            let newer_id = py_str(&c["newer_id"]);
            let args = json!({
                "name": format!("tag-clash-{}", awm_pycompat::pystr::head(&newer_id, 8)),
                "prompt": self.clash_prompt(&c)?,
                "model": "haiku",
                "effort": "low",
            });
            let handle = match self.outside().start_agent(args).await {
                Ok(res) => {
                    let pick = |k: &str| res.get(k).filter(|v| crate::truthy(v)).cloned();
                    py_str(&pick("job").or_else(|| pick("job_id")).unwrap_or(res.clone()))
                }
                Err(e) => {
                    tracing::warn!("cannot dispatch the agent for the {} clash: {e}", str_repr(&py_str(&c["name"])));
                    continue;
                }
            };
            let (now, h, o, n) = (now_ms(), handle.clone(), py_str(&c["older_id"]), newer_id.clone());
            self.db.blocking_write(move |tx| {
                tx.execute(
                    "UPDATE tag_clashes SET dispatched_at=?, dispatch=? WHERE older_id=? AND newer_id=?",
                    params![now, h, o, n],
                )?;
                Ok::<_, Fail>(())
            })?;
            let mut row = c.clone();
            row["dispatch"] = json!(handle);
            out.push(row);
        }
        Ok(out)
    }

    /// Merge every peer's vocabulary into this node's; returns the counts per peer.
    pub async fn pull(&self) -> Result<Value> {
        let outside = self.outside();
        let mut out = Map::new();
        for peer in outside.peers() {
            let got = match outside.tag_list(&peer).await {
                Ok(res) => {
                    let tags = res.get("tags").and_then(Value::as_array).cloned().unwrap_or_default();
                    self.merge_tags(&tags, &peer).await.map_err(|e| e.message)
                }
                Err(e) => Err(e),
            };
            let entry = match got {
                Ok(v) => v,
                Err(e) => {
                    let text = format!("RuntimeError({})", str_repr(&e));
                    json!({"error": awm_pycompat::pystr::head(&text, 200)})
                }
            };
            out.insert(peer, entry);
        }
        self.dispatch_open().await?;
        Ok(Value::Object(out))
    }

    /// Take each peer's vocabulary, then send this node's, once at start.
    pub async fn sync_at_start(&self) {
        match self.pull().await {
            Ok(counts) => tracing::info!("tag pull: {counts}"),
            Err(e) => tracing::warn!("tag pull failed: {e}"),
        }
        let hidden = match self.hidden_tags().await {
            Ok(Some(h)) => h,
            Ok(None) => return,
            Err(e) => {
                tracing::warn!("tag send skipped, the hidden tags are unknown: {e}");
                return;
            }
        };
        let vocabulary = match self.vocabulary() {
            Ok(tags) => tags,
            Err(e) => {
                tracing::warn!("tag send skipped: {e}");
                return;
            }
        };
        let tags: Vec<Value> = vocabulary.iter().filter(|t| !hidden.contains(&t.id)).map(Tag::to_dict).collect();
        let out = self.outside();
        for peer in out.peers() {
            out.tag_receive(&peer, tags.clone());
        }
    }

    /// A peer's pushed tags. Only an edge-stamped peer may send them.
    pub async fn receive_tags(&self, tags: &Value, as_: Option<&str>) -> Result<Value> {
        let Some(node) = awm_plugin_sdk::config::caller_peer(as_) else {
            return Err(Fail::permission("tag_receive takes tags from a peer node only"));
        };
        let list = tags.as_array().cloned().unwrap_or_default();
        self.merge_tags(&list, node).await
    }
}
