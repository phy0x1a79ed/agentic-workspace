//! The public door's verdicts against Python's (`vectors/policy.json`), plus the profile's security claims.

use awm_plugin_auth::policy::{self, classify, slices, tether, vault, Verdict};
use base64::Engine;
use serde_json::{json, Value};

fn vectors() -> Value {
    serde_json::from_str(include_str!("../vectors/policy.json")).unwrap()
}

fn opt_str(v: &Value) -> Option<&str> {
    v.as_str()
}

fn unhex(v: &Value) -> Option<Vec<u8>> {
    v.as_str().map(|s| hex::decode(s).unwrap())
}

#[test]
fn every_path_gets_pythons_verdicts() {
    let v = vectors();
    let subs: Vec<Option<&str>> = v["subs"].as_array().unwrap().iter().map(opt_str).collect();
    let rows = v["paths"].as_array().unwrap();
    assert!(rows.len() >= 200);
    for row in rows {
        let path = row["path"].as_str().unwrap();
        assert_eq!(classify(path).as_str(), row["verdict"], "{path:?}");
        for (sub, want) in subs.iter().zip(row["allows"].as_array().unwrap()) {
            assert_eq!(policy::allows(path, *sub), want.as_bool().unwrap(), "{path:?} as {sub:?}");
        }
        assert_eq!(vault::owns(path), row["vault_owns"], "{path:?}");
        assert_eq!(vault::upstream_path(path), row["vault_upstream"].as_str().unwrap(), "{path:?}");
        assert_eq!(slices::token_of(path), row["slices_token"].as_str(), "{path:?}");
        assert_eq!(slices::owns(path), row["slices_owns"], "{path:?}");
        assert_eq!(slices::refused(path), row["slices_refused"], "{path:?}");
        assert_eq!(slices::upstream_path(path), row["slices_upstream"].as_str().unwrap(), "{path:?}");
        assert_eq!(tether::owns(path), row["tether_owns"], "{path:?}");
        assert_eq!(tether::refused(path), row["tether_refused"], "{path:?}");
        assert_eq!(tether::upstream_path(path), row["tether_upstream"].as_str().unwrap(), "{path:?}");
    }
}

#[test]
fn every_raw_target_is_stripped_as_python_strips_it() {
    for row in vectors()["raw"].as_array().unwrap() {
        let raw = unhex(&row["raw"]).unwrap();
        assert_eq!(vault::upstream_raw_path(&raw), unhex(&row["vault"]), "vault {raw:?}");
        assert_eq!(slices::upstream_raw_path(&raw), unhex(&row["slices"]), "slices {raw:?}");
        assert_eq!(tether::upstream_raw_path(&raw), unhex(&row["tether"]), "tether {raw:?}");
    }
}

#[test]
fn every_invoke_body_gets_pythons_answer() {
    for row in vectors()["invoke"].as_array().unwrap() {
        let body = base64::engine::general_purpose::STANDARD.decode(row["body"].as_str().unwrap()).unwrap();
        assert_eq!(policy::invoke_allows(&body), row["allows"], "{:?}", String::from_utf8_lossy(&body));
    }
}

#[test]
fn the_constants_are_pythons() {
    let v = vectors();
    let c = &v["constants"];
    let mut exact: Vec<&str> = policy::OPEN_EXACT.to_vec();
    exact.sort_unstable();
    assert_eq!(json!(exact), c["open_exact"]);
    assert_eq!(json!(policy::OPEN_PREFIXES), c["open_prefixes"]);
    let mut fns = policy::TRILIUM_OPEN_FNS.to_vec();
    fns.sort_unstable();
    assert_eq!(json!(fns), c["trilium_open_fns"]);
    assert_eq!(policy::INVOKE_BODY_CAP as u64, c["invoke_body_cap"].as_u64().unwrap());
    let table = |t: &[(&str, &str)]| Value::Object(t.iter().map(|(k, v)| (k.to_string(), json!(v))).collect());
    assert_eq!(table(vault::NOT_FORWARDED), c["vault_not_forwarded"]);
    assert_eq!(table(slices::NOT_FORWARDED), c["slices_not_forwarded"]);
    assert_eq!(table(tether::NOT_FORWARDED), c["tether_not_forwarded"]);
    let all: Vec<&str> = slices::all_not_forwarded().iter().map(|(k, _)| *k).collect();
    assert_eq!(json!(all), c["slices_all_not_forwarded"]);
    assert_eq!(vault::manifest(), c["vault_manifest"]);
}

const DENIED: &[&str] = &[
    "/hub/services", "/hub/plugins", "/tools", "/svc/scopes/fn/scope_search",
    "/svc/auth/fn/password", "/svc/auth/fn/verify",
    "/__auth/link", "/__auth/login", "/ca.crt", "/ca.pem",
    "/ui/agents/", "/p/dashboard/dashboard/", "/p/httpsfront/login/",
    "/files/", "/s/fileviewer/", "/s/fileviewer/projects/awm/dev/x",
    "/s/claude-science/mcp",
    "/__landing/tags",
    "/ui/notes/", "/svc/notes/fn/list", "/svc/notes/emit/note:tony:1",
    "/ui/drawio/", "/drawio-app/index.html", "/svc/drawio/fn/save",
    "/svc/drawio/emit/drawio:tony:x.drawio",
    "/etapi/app-info", "/custom/x", "/mcp", "/share/abc",
    "/login", "/setup", "/set-password", "/robots.txt",
    "/ui/trilium", "/ui/trilium/", "/svc/trilium/fn/status",
    "/svc/trilium/fn/restore",
    "/p/trilium/triliumXYZ", "/p/trilium/trilium-admin/x", "/p/trilium/",
];
const ALLOWED: &[&str] = &["/p/trilium/trilium", "/p/trilium/trilium/", "/p/trilium/trilium/assets/index.js"];

fn call(verb: &str) -> Value {
    json!({"name": "trilium", "args": {"verb": verb, "args": {}}})
}

const TOKEN: &str = "Ab3-_9xYzQw012345678";

fn sp(inner: &str) -> String {
    format!("{}{TOKEN}{inner}", slices::PREFIX)
}

#[test]
fn the_public_profile_admits_only_what_it_names() {
    for path in DENIED {
        assert_eq!(classify(path), Verdict::Deny, "{path}");
        for sub in [Some("tony"), None] {
            assert!(!policy::allows(path, sub), "{path} {sub:?}");
        }
    }
    for path in ALLOWED {
        assert!(policy::allows(path, Some("tony")), "{path}");
    }
    // The vault needs a person; a peer bearer or an anonymous caller is none.
    assert!(policy::allows(vault::SHELL, Some("tony")));
    for sub in [Some("peer"), Some("peer:mira"), Some("operator"), None] {
        assert!(!policy::allows(vault::SHELL, sub), "{sub:?}");
    }
    // User-scoped paths admit only their user.
    let tony = Some("tony");
    assert!(policy::allows("/s/fileviewer/projects/userdata/tony/notes/a.md", tony));
    assert!(!policy::allows("/s/fileviewer/projects/userdata/steven/notes/a.md", tony));
    // A slice needs no session, yet keeps every refusal the vault records.
    assert!(policy::allows(&sp("/api/tree"), None));
    assert!(!policy::allows(&sp("/etapi/app-info"), None));
    assert!(!slices::owns(&sp("/manifest.webmanifest")));
    for (entry, _) in vault::NOT_FORWARDED {
        let inner = if entry.ends_with('/') { format!("{entry}x") } else { entry.to_string() };
        assert!(!slices::owns(&sp(&inner)), "{inner}");
    }
}

#[test]
fn public_invoke_admits_only_the_trilium_read_verbs() {
    assert!(!policy::invoke_allows(b""));
    assert!(!policy::invoke_allows(b"{not json"));
    for verb in policy::TRILIUM_OPEN_FNS {
        assert!(policy::invoke_allows(call(verb).to_string().as_bytes()), "{verb}");
    }
    let mut with_peer = call("status");
    with_peer["args"]["peer"] = json!("mira");
    let bodies = [
        call("restore"), call("stop"), call("export"), call("slice_resolve"), with_peer,
        json!({"name": "journal", "args": {"verb": "status", "args": {}}}),
        json!({"name": "trilium", "args": {"verb": "status", "args": []}}),
        json!({"name": "trilium", "args": {"verb": "status"}, "peer": "mira"}),
        json!([]), json!("status"),
    ];
    for body in bodies {
        assert!(!policy::invoke_allows(body.to_string().as_bytes()), "{body}");
    }
}

#[test]
fn the_vault_mount_is_owned_and_its_unforwarded_routes_are_not() {
    for inner in ["", "bootstrap", "api/tree", "assets/index-abc.js", "node_modules/@excalidraw/excalidraw/dist/prod/x.js"] {
        let path = format!("{}{inner}", vault::SHELL);
        assert!(vault::owns(&path), "{path}");
        assert_eq!(classify(&path), Verdict::Vault);
    }
    for (inner, _) in vault::NOT_FORWARDED {
        let path = format!("{}{}", vault::SHELL, inner.trim_start_matches('/'));
        assert!(!vault::owns(&path), "{path}");
        assert_eq!(classify(&path), Verdict::Deny);
    }
    assert!(!vault::owns("/api/tree"));
    assert!(!vault::owns(policy::TRILIUM_PAGE));
    assert_eq!(vault::upstream_raw_path(b"/%74rilium/api/tree"), None);
}

const TICKET: &str = "0123456789abcdef0123456789abcdef";

#[test]
fn the_tether_mount_claims_its_surface_and_refuses_near_misses() {
    let t31 = &TICKET[..31];
    let claimed = [
        "/tether".to_string(), "/tether/win".into(), "/tether/bin/tether".into(), "/tether/claim/1".into(),
        format!("/tether/join/7/{TICKET}"), format!("/tether/seat/{TICKET}/recv"), "/room/acre-anchor-kettle/send".into(),
        "/room/acre-anchor-kettle-abbey-otter-lamp/recv".into(),
    ];
    for path in &claimed {
        assert!(tether::owns(path), "{path}");
        assert_eq!(classify(path), Verdict::Tether, "{path}");
        assert!(policy::allows(path, None), "{path}");
    }
    let misses = [
        "/tether/health".to_string(), "/tether/".into(), "/tether/claim/0".into(), "/tether/claim/07".into(),
        "/tether/claim/7/extra".into(), format!("/tether/join/7/{t31}"), format!("/tether/join/7/{}", TICKET.to_uppercase()),
        format!("/tether/seat/{TICKET}/join"), "/tether/bin/../../etc/passwd".into(), "/tether/invoke".into(),
        "/tether/issue".into(), "/tether/status".into(), "/room".into(), "/room/acre-anchor".into(),
        "/room/Acre-anchor-kettle".into(), "/room/acre-anchor-kettle/".into(), "/room/acre-anchor-kettle/close".into(),
    ];
    for path in &misses {
        assert!(!tether::owns(path), "{path}");
        assert_eq!(classify(path), Verdict::Deny, "{path}");
        assert!(!policy::allows(path, None) && !policy::allows(path, Some("tony")), "{path}");
    }
    assert_eq!(tether::upstream_raw_path(b"/%74ether/claim/7"), None);
    for (path, reason) in tether::NOT_FORWARDED {
        assert!(!reason.is_empty(), "{path}");
        assert!(tether::refused(&format!("{}{path}", tether::PREFIX)), "{path}");
    }
}

