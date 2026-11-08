//! The HTML the edge serves itself, never proxied: the landing page at `/` after sign-in, and
//! the page the vault answers while it starts. Sign-in itself is alcor's passkey page.
//!
//! Both are inline and dependency-free: the one authenticated door into awm must not depend on
//! a frontend build or an external asset. The styles and the landing script are embedded from
//! `assets/`, which hold Python's strings byte for byte.

use std::collections::HashMap;

use awm_pycompat::repr::{float_repr, repr};
use serde_json::{Map, Value};

/// Trilium's own palette, shared by both pages.
const TOKENS: &str = include_str!("../assets/tokens.css");
/// The landing page's own rules, after [`TOKENS`].
const LANDING_CSS: &str = include_str!("../assets/landing.css");
/// Trilium's setup card, for the vault's waiting page.
const DOOR: &str = include_str!("../assets/door.css");
/// The landing page's script, after its five data lines.
const LANDING_JS: &str = include_str!("../assets/landing.js");

/// One page the gateway's plugins serve, as the landing page lists it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PageEntry {
    /// The page's own name: the key its tags and display name are kept under.
    pub name: String,
    pub href: String,
}

/// `html.escape(s)`, which escapes both quotes.
pub fn escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            c => out.push(c),
        }
    }
    out
}

/// `urllib.parse.quote(s, safe='')`: every UTF-8 byte but `A-Za-z0-9_.-~` percent-encoded.
pub fn quote(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || b"_.-~".contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// Python truthiness of a JSON value.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
    }
}

/// `str(v or "")`.
fn py_str(v: Option<&Value>) -> String {
    match v.filter(|v| truthy(v)) {
        None => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) if n.is_f64() => float_repr(n.as_f64().unwrap_or(0.0)),
        Some(other) => match other {
            Value::Bool(true) => "True".into(),
            Value::Number(n) => n.to_string(),
            other => repr(other),
        },
    }
}

/// `for x in (v or [])`: `None` where Python would raise, iterating a number or a bool.
fn iterate(v: Option<&Value>) -> Option<Vec<Value>> {
    match v.filter(|v| truthy(v)) {
        None => Some(Vec::new()),
        Some(Value::Array(a)) => Some(a.clone()),
        Some(Value::Object(m)) => Some(m.keys().map(|k| Value::String(k.clone())).collect()),
        Some(Value::String(s)) => Some(s.chars().map(|c| Value::String(c.to_string())).collect()),
        Some(_) => None,
    }
}

/// `(v or {})`: `None` where `.get` on the result would raise.
fn mapping(v: &Value) -> Option<Option<&Map<String, Value>>> {
    match v {
        Value::Object(m) => Some(Some(m)),
        v if !truthy(v) => Some(None),
        _ => None,
    }
}

/// Every page in the gateway's `/hub/plugins` snapshot, linked at `/p/<plugin>/<page>/`.
///
/// A snapshot whose shape Python's loop would raise on gives no entries, as the landing route
/// degraded to an empty index.
pub fn page_entries(snapshot: &Value) -> Vec<PageEntry> {
    entries_or_raise(snapshot).unwrap_or_default()
}

fn entries_or_raise(snapshot: &Value) -> Option<Vec<PageEntry>> {
    let plugins = snapshot.as_object().and_then(|m| m.get("plugins"));
    let mut out = Vec::new();
    for plugin in iterate(plugins)? {
        let plugin = mapping(&plugin)?;
        let pname = py_str(plugin.and_then(|p| p.get("name")));
        for page in iterate(plugin.and_then(|p| p.get("pages")))? {
            let page = mapping(&page)?;
            let name = py_str(page.and_then(|p| p.get("name")));
            if !pname.is_empty() && !name.is_empty() {
                let href = format!("/p/{}/{}/", quote(&pname), quote(&name));
                out.push(PageEntry { name, href });
            }
        }
    }
    Some(out)
}

/// What the landing page shows besides its entries, from [`crate::landing::Landing`].
#[derive(Debug, Clone, Default)]
pub struct LandingView {
    pub tags_by_page: HashMap<String, Vec<String>>,
    /// Every tag and how many pages carry it, in the store's order.
    pub tag_counts: Vec<(String, i64)>,
    pub selected_tags: Vec<String>,
    /// Cosmetic label overrides keyed by page name, in the store's order.
    pub display_names: Vec<(String, String)>,
}

fn lookup<'a>(pairs: &'a [(String, String)], key: &str) -> Option<&'a str> {
    pairs.iter().rev().find(|(k, _)| k == key).map(|(_, v)| v.as_str())
}

/// The index of `entries`, taggable and filterable by tag, titled `peer_name`.
///
/// A display name changes a card's label only, never its `href` or `data-page`.
pub fn landing_page(entries: &[PageEntry], view: &LandingView, peer_name: &str) -> String {
    let label = |e: &PageEntry| lookup(&view.display_names, &e.name).filter(|s| !s.is_empty()).unwrap_or(&e.name).to_string();
    let mut pages: Vec<&PageEntry> = entries.iter().collect();
    pages.sort_by_key(|e| label(e));

    let body = if pages.is_empty() {
        "  <p class=\"muted\">No pages are registered.</p>".to_string()
    } else {
        let cards: Vec<String> = pages
            .iter()
            .map(|s| {
                let name = &s.name;
                let page_tags = view.tags_by_page.get(name).cloned().unwrap_or_default();
                let ename = escape(name);
                let tag_chips: Vec<String> = page_tags
                    .iter()
                    .map(|t| {
                        format!(
                            "      <span class=\"tagchip\">{}<button type=\"button\" onclick=\"removeTag('{ename}','{}')\">&times;</button></span>",
                            escape(t),
                            escape(t)
                        )
                    })
                    .collect();
                format!(
                    "  <li class=\"card\" data-page=\"{ename}\" data-tags=\"{}\">\n    <a class=\"pagelink\" href=\"{}\">{}</a>\n    <div class=\"cardfoot\">\n      <span class=\"cardtags\">\n{}\n      </span>\n      <button class=\"dots\" type=\"button\" onclick=\"toggleTagMenu(this,'{ename}')\">&#8942;</button>\n      <div class=\"tagmenu\"></div>\n    </div>\n  </li>",
                    escape(&page_tags.join(",")),
                    escape(&s.href),
                    escape(&label(s)),
                    tag_chips.join("\n"),
                )
            })
            .collect();
        format!("  <ul class=\"cards\">\n{}\n  </ul>", cards.join("\n"))
    };

    let count = |t: &str| view.tag_counts.iter().rev().find(|(k, _)| k == t).map_or(0, |(_, n)| *n);
    let filter_chips: Vec<String> = view
        .selected_tags
        .iter()
        .map(|t| {
            let e = escape(t);
            format!("  <span class=\"chip\" data-tag=\"{e}\">{e}:{}<button type=\"button\" onclick=\"deselectFilter('{e}')\">&times;</button></span>", count(t))
        })
        .collect();
    let mut all_tags: Vec<&str> = view.tag_counts.iter().map(|(k, _)| k.as_str()).collect();
    all_tags.sort_unstable();
    all_tags.dedup();
    let filter_opts: Vec<String> = all_tags
        .iter()
        .filter(|t| !view.selected_tags.iter().any(|s| s == *t))
        .map(|t| {
            let e = escape(t);
            format!("    <option value=\"{e}\">{e}:{}</option>", count(t))
        })
        .collect();

    let mut counts = Map::new();
    for (k, n) in &view.tag_counts {
        counts.insert(k.clone(), Value::from(*n));
    }
    let mut names = Map::new();
    for (k, v) in &view.display_names {
        names.insert(k.clone(), Value::from(v.as_str()));
    }
    let dumps = awm_pycompat::json::dumps;
    let selected = Value::from(view.selected_tags.clone());
    let title = escape(peer_name);

    format!(
        "<!doctype html>\n<html lang=\"en\"><head>\n<meta charset=\"utf-8\" />\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n<title>{title}</title>\n<style>{TOKENS}{LANDING_CSS}</style>\n</head><body>\n<div class=\"top\"><h1>{title}</h1>\n  <a class=\"logout\" href=\"#\" onclick=\"fetch('/__auth/logout',{{method:'POST'}}).then(()=>location.reload());return false;\">sign out</a>\n</div>\n<div class=\"filterbar\" id=\"filterbar\">\n{}\n</div>\n<div class=\"filteradd\">\n  <select id=\"addFilter\" onchange=\"if(this.value){{selectFilter(this.value);}}\">\n    <option value=\"\">+ filter by tag&hellip;</option>\n{}\n  </select>\n  <button type=\"button\" id=\"showAllToggle\" class=\"togglechip\" onclick=\"toggleShowAll()\">show all</button>\n</div>\n<p class=\"status\" id=\"status\"></p>\n{body}\n<script>\nconst TAG_COUNTS = {};\nlet SELECTED = {};\nlet DISPLAY_NAMES = {};\nlet SHOW_ALL = false;\nconst TOTAL_PAGES = {};\n{LANDING_JS}</script>\n</body></html>",
        filter_chips.join("\n"),
        filter_opts.join("\n"),
        dumps(&Value::Object(counts)),
        dumps(&selected),
        dumps(&Value::Object(names)),
        pages.len(),
    )
}

/// The page the vault answers while it is not listening yet, with `reason`. Refreshes itself
/// every five seconds, because the common cause is a cold start.
pub fn vault_unavailable_page(reason: &str) -> String {
    format!(
        "<!doctype html>\n<html lang=\"en\"><head>\n<meta charset=\"utf-8\" />\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n<meta http-equiv=\"refresh\" content=\"5\" />\n<title>Trilium &mdash; starting</title>\n<style>{TOKENS}{DOOR}</style>\n</head><body>\n<div class=\"backdrop\">\n<main class=\"card\">\n  <header>\n    <h1>Trilium</h1>\n    <p class=\"muted\">The knowledge base is not answering yet.</p>\n  </header>\n  <p class=\"note\">{}</p>\n  <p class=\"note\">A vault that has just been started takes up to two minutes to\n  come up. This page retries every five seconds.</p>\n</main>\n</div>\n</body></html>",
        escape(reason)
    )
}
