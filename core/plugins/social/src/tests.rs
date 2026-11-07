//! The plugin's surface against the Python one.

use std::collections::BTreeSet;

use serde_json::{json, Value};

fn norm(v: &Value) -> Value {
    match v {
        Value::Number(n) => json!(n.as_f64()),
        Value::Array(a) => Value::Array(a.iter().map(norm).collect()),
        Value::Object(o) => Value::Object(o.iter().map(|(k, v)| (k.clone(), norm(v))).collect()),
        other => other.clone(),
    }
}

/// The surface changes the port plan lists; every other verb is the Python's byte for byte.
const REMOVED: [&str; 10] = ["keepers", "keep_reset", "keep_adopt", "bucket_sites", "bucket_versions", "bucket_restore", "bucket_share", "bucket_shares", "bucket_unshare", "bucket_resolve"];
const ADDED: [&str; 5] = ["mark_seat_done", "forward", "seat_reset", "seat_adopt", "seat_admit"];
const REWORDED: [&str; 10] = ["send", "react", "seats", "mount", "search", "buckets", "bucket_ls", "bucket_get", "bucket_mv", "bucket_search"];

#[test]
fn the_surface_is_the_python_one_but_the_listed_changes() {
    let golden: Value = serde_json::from_str(include_str!("../tests/golden/social.api.json")).unwrap();
    let ours: Value = serde_json::from_str(crate::MANIFEST).unwrap();
    let names = |v: &Value| v["functions"].as_array().unwrap().iter().map(|f| f["name"].as_str().unwrap().to_string()).collect::<BTreeSet<_>>();
    let (g, o) = (names(&golden), names(&ours));
    assert_eq!(g.difference(&o).cloned().collect::<BTreeSet<_>>(), REMOVED.iter().map(|s| s.to_string()).collect());
    assert_eq!(o.difference(&g).cloned().collect::<BTreeSet<_>>(), ADDED.iter().map(|s| s.to_string()).collect());
    assert_eq!((ours["emitters"].clone(), ours["sessions"].clone()), (golden["emitters"].clone(), golden["sessions"].clone()));
    for f in golden["functions"].as_array().unwrap() {
        let name = f["name"].as_str().unwrap();
        let Some(mine) = ours["functions"].as_array().unwrap().iter().find(|x| x["name"] == name) else { continue };
        if !REWORDED.contains(&name) {
            assert_eq!(mine, f, "{name} is unchanged");
            continue;
        }
        let strip = |v: &Value| {
            let mut v = v.clone();
            v.as_object_mut().unwrap().remove("description");
            for p in v["params"].as_array_mut().into_iter().flatten() {
                p.as_object_mut().unwrap().remove("required");
            }
            if let Some(ps) = v["params"].as_array_mut() {
                ps.retain(|p| !(name == "bucket_get" && p["name"] == "version"));
            }
            v
        };
        assert_eq!(strip(mine), strip(f), "{name} changes only its text and which ids it requires");
    }
}

#[tokio::test]
async fn the_service_serves_manifest_json() {
    let ours: Value = serde_json::from_str(crate::MANIFEST).unwrap();
    let svc = crate::service(awm_plugin_sdk::Service::new(test_env()), test_app().await);
    let got = svc.manifest();
    assert_eq!(norm(&got["functions"]), norm(&ours["functions"]));
}

fn test_env() -> awm_plugin_sdk::Env {
    awm_plugin_sdk::Env { hub_url: "http://127.0.0.1:1".into(), service: "social".into(), plugin: "social".into(), node: "testnode".into(), awm_dir: std::env::temp_dir(), token_file: None }
}

async fn test_app() -> std::sync::Arc<crate::app::Social> {
    let tmp = tempfile::tempdir().unwrap().keep();
    let db = crate::db::Db::open(&tmp.join("social.db")).unwrap();
    let hub = awm_plugin_sdk::Hub::new("http://127.0.0.1:1", None);
    let conns: crate::app::Conns = Default::default();
    let world = std::sync::Arc::new(crate::seats::world::Live { hub: hub.clone(), here: "testnode".into(), conns: conns.clone() });
    let seats = crate::seats::Seats::new(db.clone(), Default::default(), vec![], world);
    crate::app::Social::new("testnode".into(), hub, db, vec![], conns, vec![], seats)
}
