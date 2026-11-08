//! The edge's own pages, byte for byte against Python's (`vectors/pages.json`).

use std::collections::HashMap;

use awm_plugin_auth::landing::Landing;
use awm_plugin_auth::pages::{self, LandingView, PageEntry};
use serde_json::{json, Value};

fn vectors() -> Value {
    serde_json::from_str(include_str!("vectors/pages.json")).unwrap()
}

fn strings(v: &Value) -> Vec<String> {
    v.as_array().unwrap().iter().map(|s| s.as_str().unwrap().to_string()).collect()
}

fn entries(v: &Value) -> Vec<PageEntry> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|e| PageEntry { name: e["name"].as_str().unwrap().into(), href: e["href"].as_str().unwrap().into() })
        .collect()
}

#[test]
fn page_entries_reads_every_snapshot_as_python_did() {
    for row in vectors()["page_entries"].as_array().unwrap() {
        assert_eq!(pages::page_entries(&row["snapshot"]), entries(&row["entries"]), "{}", row["snapshot"]);
    }
}

#[test]
fn the_landing_page_is_pythons_byte_for_byte() {
    for row in vectors()["landing"].as_array().unwrap() {
        let view = LandingView {
            tags_by_page: row["tags_by_page"].as_object().unwrap().iter().map(|(k, v)| (k.clone(), strings(v))).collect(),
            tag_counts: row["tag_counts"].as_array().unwrap().iter().map(|p| (p[0].as_str().unwrap().into(), p[1].as_i64().unwrap())).collect(),
            selected_tags: strings(&row["selected_tags"]),
            display_names: row["display_names"].as_array().unwrap().iter().map(|p| (p[0].as_str().unwrap().into(), p[1].as_str().unwrap().into())).collect(),
        };
        let html = pages::landing_page(&entries(&row["entries"]), &view, row["peer_name"].as_str().unwrap());
        let want = row["html"].as_str().unwrap();
        if html != want {
            let at = html.bytes().zip(want.bytes()).position(|(a, b)| a != b).unwrap_or(html.len().min(want.len()));
            panic!("differs at byte {at}: got {:?}, want {:?}", &html[at.saturating_sub(40)..(at + 80).min(html.len())], &want[at.saturating_sub(40)..(at + 80).min(want.len())]);
        }
    }
}

#[test]
fn the_vault_waiting_page_is_pythons_byte_for_byte() {
    for row in vectors()["vault_unavailable"].as_array().unwrap() {
        assert_eq!(pages::vault_unavailable_page(row["reason"].as_str().unwrap()), row["html"].as_str().unwrap());
    }
}

#[test]
fn escape_and_quote_are_pythons() {
    assert_eq!(pages::escape("<a href=\"x\">'&'</a>"), "&lt;a href=&quot;x&quot;&gt;&#x27;&amp;&#x27;&lt;/a&gt;");
    assert_eq!(pages::quote("https://h.example/p?q=1&r=~ é"), "https%3A%2F%2Fh.example%2Fp%3Fq%3D1%26r%3D~%20%C3%A9");
}

/// httpsfront's `test_root_renders_display_name_label_but_keeps_technical_href_and_key`, on
/// the page and the store without the route.
#[test]
fn test_root_renders_display_name_label_but_keeps_technical_href_and_key() {
    let dir = tempfile::tempdir().unwrap();
    let store = Landing::open(&dir.path().join("landing.db")).unwrap();
    store.set_display_name("science", "Science Lab").unwrap();
    let snapshot = json!({"plugins": [
        {"name": "claude-science", "pages": [{"name": "science", "dir": "/x/pages/science/dist", "entry": "index.html"}]},
        {"name": "journal", "pages": []},
    ]});
    let entries = pages::page_entries(&snapshot);
    let names: Vec<String> = entries.iter().map(|e| e.name.clone()).collect();
    let view = LandingView {
        tags_by_page: store.tags_by_page(&names).unwrap(),
        tag_counts: store.all_tag_counts().unwrap(),
        selected_tags: store.selected_tags().unwrap(),
        display_names: store.display_names(&names).unwrap(),
    };
    let body = pages::landing_page(&entries, &view, "awm");
    assert!(body.contains("data-page=\"science\""));
    assert!(body.contains("href=\"/p/claude-science/science/\""));
    assert!(body.contains(">Science Lab<"));
}

#[test]
fn no_pages_says_so() {
    let body = pages::landing_page(&[], &LandingView { tags_by_page: HashMap::new(), ..Default::default() }, "awm");
    assert!(body.contains("No pages are registered."));
    assert!(body.contains("const TOTAL_PAGES = 0;"));
}
