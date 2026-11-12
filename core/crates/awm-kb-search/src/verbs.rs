//! The `search` tool's verbs, as the gateway sees them.

use crate::search::Search;
use awm_plugin_sdk::{Call, Service, VerbError};
use serde_json::{json, Value};
use std::sync::Arc;
use std::time::Duration;

pub const TOOL: &str = "search";

pub const QUERY_DESCRIPTION: &str = "\
Rank hits from named sources. Open a hit with `get`.
  query: string | {\"text\"?: T, \"<source>\": F, ...}    max: hits per source per section, default 10

<source>: a kind `sources` lists, such as journals, or guide for how to use awm; @node for another node.
   A bare kind this node lacks goes to the first peer that serves it.
F: MongoDB filter on the source's fields: $and $or $nor $eq $ne $gt $gte $lt $lte $in $nin $all.
   {} matches all. * in a value globs. A scope covers its nested scopes. ts is ISO-8601.
$sort in F: one field, +/-. On content, by match score.
T: string | {\"$contains\": s} | {\"$about\": s} | {\"$and\"|\"$or\": [...]}, shared by all sources.
   $contains: case-insensitive phrase of whole words. Nests under $and $or $nor.
   $about: rank by meaning. At most one, directly under the top $and or $or.
   Under $and or alone it only ranks the matches. Under $or it also adds the nearest others, as `related`.
Short forms: query s = {\"text\": s}. No source = this node's journals. T s = {\"$or\": [{\"$about\": s}, {\"$contains\": s}]}.
Returns {source: {exact, related, node?}}; node names the peer a bare kind went to. A hit is the id and the fields `sources` marks. Open one with `get`.
";

const GET_DESCRIPTION: &str = "One document in full: every field its source declares; a paper adds its authors and journal, and a guide hit is its section of a doc or its verb's describe. Take `source` and `id` from a hit. For a hit from another node, set `node` to the node of its source. Pass the `query` the hit answered: each get is logged with the caller's scope as feedback on search.";

const SOURCES_DESCRIPTION: &str = "Every source a query reaches, on this node and on its peers (`mode: peer`), with its kind, node, mode, fields and their classes, the fields a hit shows, whether a query that names no source asks it (`default`), its document count, and its last answer and error; and search's health: whether semantic ranking is available; and `peers`, each peer's kinds or why it did not answer.";

const STATUS_DESCRIPTION: &str = "search's health: documents per source, each source's last answer and error, and whether semantic ranking is available.";

/// A param as describe lists it: the type only when it is not a string.
fn p(name: &str, ty: &str, required: bool, description: &str) -> Value {
    let mut v = json!({"name": name, "type": ty, "required": required, "description": description});
    if ty == "string" {
        v.as_object_mut().map(|m| m.remove("type"));
    }
    v
}

/// The calling session on this node, as the read records name it. A call from another node has none.
fn session(call: &Call) -> Option<String> {
    if call.caller.lane != awm_plugin_sdk::Lane::Local {
        return None;
    }
    call.caller.session.as_deref().or_else(|| call.caller_session_id()).and_then(awm_plugin_sdk::ids::SessionId::checked)
}

/// Add every verb of the `search` tool to `svc`.
pub fn register(svc: Service, s: Arc<Search>) -> Service {
    let (q, g, so, st, j, js) = (s.clone(), s.clone(), s.clone(), s.clone(), s.clone(), s);
    svc.verb(
        TOOL,
        "query",
        QUERY_DESCRIPTION,
        json!([
            {"name": "query", "type": ["string", "object"], "required": true,
             "description": "A string, or an object {\"text\"?: T, \"<source>\": F, ...}."},
            p("max", "integer", false, "Hits per source per section, 1-100. Default 10."),
        ]),
        move |call: Call| {
            let s = q.clone();
            async move { s.query(&call.args, call.as_.as_deref(), session(&call)).await.map_err(VerbError::from) }
        },
    )
    .timeout(Duration::from_secs(60))
    .verb(
        TOOL,
        "get",
        GET_DESCRIPTION,
        json!([
            p("source", "string", true, "The hit's source."),
            p("id", "string", true, "The hit's id."),
            p("node", "string", false, "The node that holds the document. Default: this node."),
            p("query", "string", false, "The query whose hit this is."),
        ]),
        move |call: Call| {
            let s = g.clone();
            async move { s.get(&call.args, call.as_.as_deref(), session(&call)).await.map_err(VerbError::from) }
        },
    )
    .verb(TOOL, "sources", SOURCES_DESCRIPTION, json!([]), move |call: Call| {
        let s = so.clone();
        async move { Ok::<Value, VerbError>(s.sources(call.as_.as_deref()).await) }
    })
    .verb(TOOL, "status", STATUS_DESCRIPTION, json!([]), move |call: Call| {
        let s = st.clone();
        async move { Ok::<Value, VerbError>(s.status(call.as_.as_deref()).await) }
    })
    .hidden()
    .verb(
        TOOL,
        "judge",
        "Record whether one hit answers one query, for the judgement set that every change to search is replayed against. A later verdict on the same query and hit replaces the earlier one, and `clear` removes it.",
        json!([
            p("query", "string", true, "The query, as asked."),
            p("source", "string", true, "The hit's source."),
            p("id", "string", true, "The hit's id."),
            p("verdict", "string", true, "relevant, irrelevant, or clear."),
            p("section", "string", false, "For a relevant hit, the section it must answer in: exact or related. Default: any."),
            p("set", "string", false, "The set the judgement belongs to, such as gold or t5. Default: none, a mark by hand."),
            p("note", "string", false, "Why."),
        ]),
        move |call: Call| {
            let s = j.clone();
            async move { s.judge(&call.args, call.as_.as_deref()).await.map_err(VerbError::from) }
        },
    )
    .hidden()
    .verb(
        TOOL,
        "judgements",
        "The recorded judgements, optionally of one set or one query.",
        json!([
            p("set", "string", false, "Only this set."),
            p("query", "string", false, "Only this query."),
        ]),
        move |call: Call| {
            let s = js.clone();
            async move { s.judgements(&call.args).await.map_err(VerbError::from) }
        },
    )
    .hidden()
}
