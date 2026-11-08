//! The edge's credential gate (httpsfront test_device_token.py and test_scoped_device_token.py,
//! their gate halves), the slide rule against the Python's vectors, and the in-process source.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use awm_plugin_auth::gate::{self, AuthGate, Decision, DeviceEntry, Material, MaterialSource, Shape, Used};
use awm_plugin_auth::store::AuthStore;
use awm_plugin_auth::tokens::{self, AVATAR_STAGE, PREFIX};
use awm_plugin_auth::verbs::{self, Caller};
use serde_json::{json, Value};

const SECRET: &str = "edge-secret";
const NOW: f64 = 1_700_000_000.0;
const SESSION: &str = "/hub/session/avatar/stage";

/// A source that answers with fixed material, or fails, and keeps each report it was sent.
struct Fake {
    material: Mutex<Option<Material>>,
    sent: Mutex<Vec<Used>>,
}

impl Fake {
    fn new(material: Option<Material>) -> Arc<Fake> {
        Arc::new(Fake { material: Mutex::new(material), sent: Mutex::default() })
    }

    fn sent(&self) -> Vec<Used> {
        self.sent.lock().unwrap().clone()
    }

    fn fail(&self) {
        *self.material.lock().unwrap() = None;
    }
}

impl MaterialSource for Fake {
    fn fetch(&self, used: &Used) -> anyhow::Result<Material> {
        self.sent.lock().unwrap().push(used.clone());
        self.material.lock().unwrap().clone().ok_or_else(|| anyhow::anyhow!("auth down"))
    }
}

fn material(devices: Vec<DeviceEntry>) -> Material {
    Material { secret: SECRET.into(), device_tokens: devices, session_ttl_seconds: 3600.0, max_session_seconds: 0.0 }
}

fn gate_with(devices: Vec<DeviceEntry>) -> (Arc<Fake>, AuthGate) {
    let fake = Fake::new(Some(material(devices)));
    let g = AuthGate::new(fake.clone());
    (fake, g)
}

fn device(sub: &str, scope: Option<&str>) -> (String, DeviceEntry) {
    let t = tokens::mint();
    (t.token, DeviceEntry { id: t.id, sub: sub.into(), sha256: tokens::digest(&t.secret), scope: scope.map(str::to_string) })
}

fn cookie(sub: &str) -> String {
    tokens::session_mint(SECRET, sub, 3600.0, None, NOW)
}

fn auth(g: &AuthGate, bearer: Option<&str>, cookie: Option<&str>, shape: Option<&Shape<'_>>) -> (bool, Option<String>, Option<String>) {
    let d = g.authenticate(cookie, bearer, shape, NOW);
    let (ok, r, s) = d.as_tuple();
    (ok, r.map(str::to_string), s.map(str::to_string))
}

const REFUSED: (bool, Option<String>, Option<String>) = (false, None, None);

fn me(sub: &str) -> (bool, Option<String>, Option<String>) {
    (true, None, Some(sub.into()))
}

const KINDS: [&str; 5] = ["revoked", "unknown", "wrong secret", "malformed", "bare prefix"];

fn bad(kind: &str, entry: DeviceEntry, token: &str) -> (String, Vec<DeviceEntry>) {
    let (id, _) = tokens::parse(token).unwrap();
    match kind {
        "revoked" => (token.into(), vec![]),
        "unknown" => (tokens::mint().token, vec![entry]),
        "wrong secret" => (format!("{PREFIX}{id}.{}", tokens::mint().secret), vec![entry]),
        "malformed" => (format!("{PREFIX}{id}.short"), vec![entry]),
        _ => (PREFIX.into(), vec![entry]),
    }
}

// -- the gate (test_device_token.py) -----------------------------------------------------------

#[test]
fn test_a_live_token_is_its_user() {
    let (token, entry) = device("tony", None);
    let (_f, g) = gate_with(vec![entry]);
    assert_eq!(auth(&g, Some(&token), None, None), me("tony"));
}

#[test]
fn test_a_bad_token_is_refused() {
    for kind in KINDS {
        let (token, entry) = device("tony", None);
        let (bearer, devices) = bad(kind, entry, &token);
        let (_f, g) = gate_with(devices);
        assert_eq!(auth(&g, Some(&bearer), None, None), REFUSED, "{kind}");
    }
}

#[test]
fn test_a_bad_token_never_falls_through_to_a_cookie() {
    for kind in KINDS {
        let (token, entry) = device("tony", None);
        let (bearer, devices) = bad(kind, entry, &token);
        let (_f, g) = gate_with(devices);
        let c = cookie("tony");
        assert_eq!(auth(&g, Some(&bearer), None, None), REFUSED, "{kind}");
        assert_eq!(auth(&g, Some(&bearer), Some(&c), None), REFUSED, "{kind}");
    }
}

#[test]
fn test_a_token_naming_a_machine_is_refused() {
    for sub in ["operator", "peer", "peer:mira", ""] {
        let (token, entry) = device(sub, None);
        let (_f, g) = gate_with(vec![entry]);
        assert_eq!(auth(&g, Some(&token), None, None), REFUSED, "{sub:?}");
    }
}

#[test]
fn test_material_from_an_older_auth_without_the_key_still_works() {
    let mat: Material = serde_json::from_value(json!({"secret": SECRET, "session_ttl_seconds": 3600.0, "max_session_seconds": 0})).unwrap();
    let g = AuthGate::new(Fake::new(Some(mat)));
    let (token, _) = device("tony", None);
    assert_eq!(auth(&g, Some(&token), None, None), REFUSED);
    assert_eq!(auth(&g, None, Some(&cookie("tony")), None).2.as_deref(), Some("tony"));
}

#[test]
fn a_bearer_that_is_not_a_device_token_leaves_the_cookie_to_decide() {
    let (_f, g) = gate_with(vec![]);
    let c = cookie("tony");
    for bearer in [None, Some(""), Some("not-a-device-token")] {
        let (ok, refreshed, sub) = auth(&g, bearer, Some(&c), None);
        assert!(ok && refreshed.is_some() && sub.as_deref() == Some("tony"), "{bearer:?}");
    }
    assert_eq!(auth(&g, Some("x"), None, None), REFUSED);
    assert_eq!(auth(&g, None, Some(""), None), REFUSED);
    assert_eq!(auth(&g, None, Some("garbage"), None), REFUSED);
}

#[test]
fn with_no_material_the_gate_admits_nothing() {
    let fake = Fake::new(None);
    let g = AuthGate::new(fake.clone());
    let (token, _) = device("tony", None);
    assert_eq!(auth(&g, Some(&token), None, None), REFUSED);
    assert_eq!(auth(&g, None, Some(&cookie("tony")), None), REFUSED);
    assert_eq!(g.mint_session("tony", NOW), None);
    assert_eq!(g.session_ttl_seconds(), gate::DEFAULT_SESSION_TTL);
    assert!(fake.sent().len() >= 3, "each request retries the fetch");
}

#[test]
fn material_with_no_secret_counts_as_no_material() {
    let mut mat = material(vec![]);
    mat.secret.clear();
    let g = AuthGate::new(Fake::new(Some(mat)));
    assert_eq!(auth(&g, None, Some(&cookie("tony")), None), REFUSED);
}

#[test]
fn material_stays_fresh_for_the_window() {
    let (fake, g) = gate_with(vec![]);
    for _ in 0..5 {
        assert!(g.material().is_some());
    }
    assert_eq!(fake.sent().len(), 1);
    let g = AuthGate::new(fake.clone()).with_interval(Duration::ZERO);
    g.material();
    g.material();
    assert_eq!(fake.sent().len(), 3);
}

// -- usage reporting ---------------------------------------------------------------------------

#[test]
fn test_the_edge_reports_use_on_its_next_fetch_and_then_forgets_it() {
    let (token, entry) = device("tony", None);
    let token_id = entry.id.clone();
    let (fake, g) = gate_with(vec![entry]);
    assert!(auth(&g, Some(&token), None, None).0);
    assert!(g.tokens_seen().keys().eq([&token_id]));
    assert_eq!(g.tokens_seen()[&token_id], NOW);
    assert!(g.refresh().is_some());
    let sent = fake.sent();
    assert_eq!(sent.len(), 2);
    assert!(sent[0].is_empty());
    assert_eq!(sent[1].keys().collect::<Vec<_>>(), [&token_id]);
    assert!(g.tokens_seen().is_empty());
    g.refresh();
    assert!(fake.sent()[2].is_empty());
}

#[test]
fn test_a_failed_fetch_keeps_the_report_for_the_next_one() {
    let (token, entry) = device("tony", None);
    let token_id = entry.id.clone();
    let (fake, g) = gate_with(vec![entry]);
    auth(&g, Some(&token), None, None);
    let before = g.material().unwrap();
    fake.fail();
    let after = g.refresh().expect("the stale material stays");
    assert!(Arc::ptr_eq(&before, &after));
    assert!(g.tokens_seen().keys().eq([&token_id]));
    assert_eq!(auth(&g, Some(&token), None, None), me("tony"), "the stale material still answers");
}

// -- scoped tokens (test_scoped_device_token.py) -----------------------------------------------

fn sh<'a>(method: &'a str, path: &'a str, raw: Option<&'a [u8]>, query: &'a [u8], ws: bool) -> Shape<'a> {
    Shape { method, path, raw: raw.unwrap_or(path.as_bytes()), query, websocket: ws }
}

fn refused_shapes() -> Vec<Shape<'static>> {
    vec![
        sh("GET", SESSION, None, b"", false),
        sh("POST", SESSION, None, b"", false),
        sh("POST", SESSION, None, b"", true),
        sh("GET", "/hub/session/avatar/other", None, b"", true),
        sh("GET", "/hub/emit/avatar/live", None, b"", true),
        sh("GET", "/hub/session/journal/stage", None, b"", true),
        sh("POST", "/invoke", None, b"", false),
        sh("GET", "/hub/session/avatar/stage/", None, b"", true),
        sh("GET", SESSION, None, b"peer=deneb", true),
        sh("GET", SESSION, Some(b"/hub/session/avatar/%73tage"), b"", true),
        sh("GET", "/s/avatar/stage", None, b"", true),
        sh("GET", "/svc/avatar/session/stage", None, b"", true),
    ]
}

#[test]
fn test_a_scoped_token_is_its_user_on_its_shapes() {
    let (token, entry) = device("tony", Some(AVATAR_STAGE));
    let (_f, g) = gate_with(vec![entry]);
    assert_eq!(auth(&g, Some(&token), None, Some(&sh("GET", SESSION, None, b"", true))), me("tony"));
}

#[test]
fn test_a_scoped_token_is_no_one_anywhere_else() {
    let (token, entry) = device("tony", Some(AVATAR_STAGE));
    let (_f, g) = gate_with(vec![entry]);
    let c = cookie("tony");
    let shapes = refused_shapes();
    for shape in shapes.iter().map(Some).chain([None]) {
        assert_eq!(auth(&g, Some(&token), None, shape), REFUSED, "{shape:?}");
        assert_eq!(auth(&g, Some(&token), Some(&c), shape), REFUSED, "{shape:?}");
    }
    assert!(g.tokens_seen().is_empty(), "a refused token is not reported as used");
}

#[test]
fn test_a_token_with_an_unknown_scope_is_refused_on_every_shape() {
    let (token, entry) = device("tony", Some("everything"));
    let (_f, g) = gate_with(vec![entry]);
    let ok = sh("GET", SESSION, None, b"", true);
    for shape in [Some(&ok), None] {
        assert_eq!(auth(&g, Some(&token), None, shape), REFUSED);
    }
}

#[test]
fn test_an_unscoped_token_still_needs_no_shape() {
    let (token, entry) = device("tony", None);
    let (_f, g) = gate_with(vec![entry]);
    assert_eq!(auth(&g, Some(&token), None, None), me("tony"));
    assert_eq!(auth(&g, Some(&token), None, Some(&sh("POST", "/invoke", None, b"", false))), me("tony"));
}

#[test]
fn an_empty_scope_reads_as_unscoped() {
    let (token, entry) = device("tony", Some(""));
    let (_f, g) = gate_with(vec![entry]);
    assert_eq!(auth(&g, Some(&token), None, None), me("tony"));
}

// -- the slide rule and the session mint, against the Python -----------------------------------

fn vectors() -> Value {
    serde_json::from_str(include_str!("vectors/tokens.json")).expect("tokens.json")
}

fn mat_of(v: &Value) -> Material {
    serde_json::from_value(v.clone()).unwrap()
}

#[test]
fn the_slide_rule_is_pythons_at_its_boundaries() {
    let v = vectors();
    let cases = v["gate"]["slide"].as_array().unwrap();
    assert!(cases.iter().any(|c| c["ok"] == true && c["refreshed"].is_null()), "the ceiling is covered");
    assert!(cases.iter().any(|c| c["ok"] == false), "the exp is covered");
    for c in cases {
        let d = gate::decide(&mat_of(&c["material"]), c["cookie"].as_str(), None, None, c["now"].as_f64().unwrap());
        let (ok, refreshed, sub) = d.as_tuple();
        assert_eq!(json!([ok, refreshed, sub]), json!([c["ok"], c["refreshed"], c["sub"]]), "{c}");
    }
}

#[test]
fn a_portal_session_is_minted_as_python_mints_it() {
    let v = vectors();
    for c in v["gate"]["mint_session"].as_array().unwrap() {
        let g = AuthGate::new(Fake::new(Some(mat_of(&c["material"]))));
        let (token, ttl) = g.mint_session(c["sub"].as_str().unwrap(), c["now"].as_f64().unwrap()).unwrap();
        assert_eq!(token, c["token"].as_str().unwrap(), "{c}");
        assert_eq!(ttl, c["ttl"].as_i64().unwrap());
        assert_eq!(g.session_ttl_seconds(), c["session_ttl_seconds"].as_f64().unwrap());
    }
}

#[test]
fn a_minted_session_authenticates_and_slides() {
    let (_f, g) = gate_with(vec![]);
    let (token, ttl) = g.mint_session("tony", NOW).unwrap();
    assert_eq!(ttl, 3600);
    let d = g.authenticate(Some(&token), None, None, NOW + 1800.0);
    let Decision::Session { sub, refreshed: Some(next) } = d else { panic!("{d:?}") };
    assert_eq!(sub, "tony");
    let claims = tokens::session_verify(SECRET, &next, NOW + 1800.0).unwrap();
    assert_eq!(claims["sat"], NOW as i64);
    assert_eq!(claims["exp"], (NOW + 1800.0 + 3600.0) as i64);
    assert_eq!(g.authenticate(Some(&token), None, None, NOW + 3600.0), Decision::Refused);
    assert!(g.tokens_seen().is_empty(), "a cookie is not a device");
}

#[test]
fn bearer_of_and_is_machine_sub_are_pythons() {
    let v = vectors();
    for c in v["headers"]["bearer_of"].as_array().unwrap() {
        assert_eq!(gate::bearer_of(c["header"].as_str()), c["bearer"].as_str(), "{c}");
    }
    for c in v["headers"]["is_machine_sub"].as_array().unwrap() {
        assert_eq!(gate::is_machine_sub(c["sub"].as_str()), c["machine"].as_bool().unwrap(), "{c}");
    }
    assert_eq!((gate::COOKIE_NAME, gate::AS_COOKIE_NAME), ("awm_session", "awm_as"));
}

// -- the in-process source ---------------------------------------------------------------------

fn store() -> (tempfile::TempDir, AuthStore) {
    let dir = tempfile::tempdir().unwrap();
    let s = AuthStore::open(AuthStore::path_in(dir.path()), true).unwrap();
    (dir, s)
}

#[test]
fn the_store_feeds_the_gate_and_takes_its_report() {
    let (_d, store) = store();
    let out = verbs::device_mint(&store, &json!({"label": "kiosk", "scope": AVATAR_STAGE}), Caller::OPERATOR, NOW).unwrap();
    let token = out["token"].as_str().unwrap().to_string();
    let token_id = out["id"].as_str().unwrap().to_string();
    let g = AuthGate::new(Arc::new(store.clone()));
    let ws = sh("GET", SESSION, None, b"", true);
    assert_eq!(auth(&g, Some(&token), None, Some(&ws)), me(&format!("device:{token_id}")));
    assert_eq!(auth(&g, Some(&token), None, None), REFUSED, "a scoped token needs its shape");
    g.refresh();
    assert_eq!(store.device_get(&token_id).unwrap().unwrap().last_used_at, Some(NOW));
    assert!(g.tokens_seen().is_empty());

    let session = g.mint_session("tony", NOW).unwrap();
    assert_eq!(session.1, 86400, "the default lifetime is a day");
    assert_eq!(tokens::session_verify(&store.secret().unwrap(), &session.0, NOW).unwrap()["sub"], "tony");

    verbs::device_revoke(&store, &json!({"id": token_id}), Caller::OPERATOR, NOW).unwrap();
    assert!(auth(&g, Some(&token), None, Some(&ws)).0, "the cached material still holds it");
    g.refresh();
    assert_eq!(auth(&g, Some(&token), None, Some(&ws)), REFUSED, "gone after the next refresh");
}

#[test]
fn the_store_feeds_the_session_settings() {
    let (_d, store) = store();
    verbs::config_set(&store, &json!({"values": {"session_ttl_hours": 12.0, "max_session_days": 1.0}})).unwrap();
    let g = AuthGate::new(Arc::new(store.clone()));
    let m = g.material().unwrap();
    assert_eq!((m.session_ttl_seconds, m.max_session_seconds), (43200.0, 86400.0));
    assert_eq!(g.session_ttl_seconds(), 43200.0);
    let old = tokens::session_mint(&m.secret, "tony", 86400.0 * 3.0, Some(NOW as i64), NOW);
    let d = g.authenticate(Some(&old), None, None, NOW + 86400.0);
    assert_eq!(d, Decision::Session { sub: "tony".into(), refreshed: None }, "past the ceiling it stops sliding");
}

#[test]
fn a_store_without_its_secret_feeds_no_material() {
    let (_d, store) = store();
    store.db().connection().unwrap().execute_batch("DELETE FROM awm_secret").unwrap();
    let g = AuthGate::new(Arc::new(store.clone()));
    assert!(g.material().is_none());
    assert!(g.mint_session("tony", NOW).is_none());
}
