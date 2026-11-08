//! The approver against the soft authenticator: enrolment, the device sign-in, approval requests
//! on the requester's own task, grants, orders, the 2fa burst and the portal sign-in.
//! Every id, host and key here is synthetic.

mod vault_common;

use awm_plugin_operations::approver::{approval_challenge, request_kinds, Approver, ApproverError, CHALLENGE_TTL_S, REQUEST_TTL_S, SESSION_TTL_S};
use awm_plugin_operations::passkeys::soft::SoftAuthenticator;
use awm_plugin_operations::store::Open;
use awm_trust::grant::{b64u, canonical, unb64u, verify_grant, verify_order, Expect, GrantError};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use vault_common::{env, env_with, s, AGENT_NODE, ORIGIN, PORTAL, PORTAL_NODE, RP_ID};

const A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn code<T: std::fmt::Debug>(r: Result<T, ApproverError>) -> &'static str {
    r.expect_err("expected a refusal").code
}

fn ssh_pubkey() -> String {
    let k = ssh_key::PrivateKey::random(&mut rand::rngs::OsRng, ssh_key::Algorithm::Ed25519).unwrap();
    format!("{} user@laptop", k.public_key().to_openssh().unwrap())
}

fn trust_payload() -> Value {
    json!({"session_id": "cc-1", "from": "low", "to": "high"})
}

fn grant_of(got: &Value) -> &Value {
    &got["grant"]
}

// -- enrolment (R64 item 1) ----------------------------------------------------------------

#[tokio::test]
async fn open_refuses_a_missing_db_unless_create() {
    let e = env();
    let keys = awm_plugin_operations::keys::Keys::generate(&e.dir.path().join("keys2")).unwrap();
    let missing = e.dir.path().join("none").join("approver.db");
    let r = Approver::open(&missing, keys, vault_common::config(5), e.store.clone(), e.tools.clone(), false, e.clock.as_fn());
    assert!(r.is_err());
    assert!(!missing.exists());
}

#[tokio::test]
async fn enrol_link_is_single_use() {
    let e = env();
    let link = e.ap.enrol_link(15, None).unwrap();
    assert!(s(&link["url"]).starts_with(&format!("{ORIGIN}/enrol#")));
    assert_eq!(s(&link["url"]).split_once('#').unwrap().1, s(&link["token"]));
    let (device, _) = e.enrol_with(s(&link["token"]), "phone");
    assert!(device.starts_with('d'));
    assert_eq!(code(e.ap.enrol_begin(s(&link["token"]), "again")), "bad_link");
}

#[tokio::test]
async fn enrol_finish_answers_only_the_device() {
    let e = env();
    let token = e.ap.enrol_link(15, None).unwrap()["token"].as_str().unwrap().to_string();
    let mut pk = SoftAuthenticator::new(ORIGIN, RP_ID);
    let begin = e.ap.enrol_begin(&token, "phone").unwrap();
    assert_eq!(begin["options"]["rp"]["id"], RP_ID);
    let out = e.ap.enrol_finish(&token, s(&begin["reg_id"]), &pk.register(&begin["options"]), "phone", Some("203.0.113.5")).unwrap();
    let keys: Vec<&String> = out.as_object().unwrap().keys().collect();
    assert_eq!(keys, ["device"]);
    let d = e.ap.devices().unwrap();
    assert_eq!(d[0]["device_id"], out["device"]);
    assert_eq!(d[0]["name"], "phone");
    assert_eq!(d[0]["enrolled_by"], "link");
    assert!(e.ap.check_credentials().is_empty());
}

#[tokio::test]
async fn enrol_link_expires_and_caps_at_15_minutes() {
    let e = env();
    let link = e.ap.enrol_link(60, None).unwrap();
    assert_eq!(link["expires"].as_f64().unwrap(), e.clock.now() + 15.0 * 60.0);
    e.clock.advance(16.0 * 60.0);
    assert_eq!(code(e.ap.enrol_begin(s(&link["token"]), "late")), "bad_link");
}

#[tokio::test]
async fn enrol_finish_refuses_a_bad_registration_and_a_wrong_token() {
    let e = env();
    let token = e.ap.enrol_link(15, None).unwrap()["token"].as_str().unwrap().to_string();
    let begin = e.ap.enrol_begin(&token, "phone").unwrap();
    let mut evil = SoftAuthenticator::new("https://evil.example.test", RP_ID);
    let cred = evil.register(&begin["options"]);
    assert_eq!(code(e.ap.enrol_finish(&token, s(&begin["reg_id"]), &cred, "phone", None)), "bad_passkey");
    let other = e.ap.enrol_link(15, None).unwrap()["token"].as_str().unwrap().to_string();
    let mut pk = SoftAuthenticator::new(ORIGIN, RP_ID);
    let cred = pk.register(&begin["options"]);
    assert_eq!(code(e.ap.enrol_finish(&other, s(&begin["reg_id"]), &cred, "phone", None)), "bad_link");
    // The link stays good after a failed finish.
    e.ap.enrol_finish(&token, s(&begin["reg_id"]), &cred, "phone", None).unwrap();
    assert_eq!(e.ap.devices().unwrap().as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn second_device_enrols_by_approval() {
    let e = env();
    let (_, mut pk) = e.enrol("phone");
    let ask = e.ap.enrol_ask("laptop").await.unwrap();
    let (rid, secret) = (s(&ask["request_id"]).to_string(), s(&ask["poll_secret"]).to_string());
    let st = e.ap.enrol_ask_status(&rid, &secret).unwrap();
    assert_eq!((st["status"].as_str(), &st["token"]), (Some("pending"), &Value::Null));
    let view = e.ap.approval_get(&rid).unwrap();
    assert_eq!((view["kind"].as_str(), &view["payload"]), (Some("device_enrol"), &json!({"name": "laptop"})));
    let a = e.approval_assertion(&mut pk, &rid);
    e.ap.approve(&rid, &a).await.unwrap();
    let st = e.ap.enrol_ask_status(&rid, &secret).unwrap();
    assert_eq!(st["status"], "approved");
    let token = s(&st["token"]).to_string();
    let again = e.ap.enrol_ask_status(&rid, &secret).unwrap();
    assert_eq!(again["token"], Value::Null, "the enrol token is handed once");
    assert_eq!(code(e.ap.enrol_ask_status(&rid, "wrong")), "unknown_request");
    e.enrol_with(&token, "laptop");
    let by: Vec<Value> = e.ap.devices().unwrap().as_array().unwrap().iter().map(|d| d["enrolled_by"].clone()).collect();
    assert_eq!(by, [json!("link"), json!(rid)]);
}

#[tokio::test]
async fn enrol_ask_expires() {
    let e = env();
    let ask = e.ap.enrol_ask("laptop").await.unwrap();
    e.clock.advance(REQUEST_TTL_S + 1.0);
    let st = e.ap.enrol_ask_status(s(&ask["request_id"]), s(&ask["poll_secret"])).unwrap();
    assert_eq!(st["status"], "expired");
}

// -- the device sign-in (R84 items 1-2) ------------------------------------------------------

#[tokio::test]
async fn sessions_last_12_hours_and_end() {
    let e = env();
    let (d, _) = e.enrol("phone");
    let c = e.ap.session_new(&d, Some("203.0.113.5")).unwrap();
    assert_eq!(e.ap.session_device(&c).unwrap().as_deref(), Some(d.as_str()));
    assert_eq!(e.ap.session_device("not-a-cookie").unwrap(), None);
    assert_eq!(e.ap.session_device(&b64u(&[7u8; 32])).unwrap(), None);
    e.clock.advance(SESSION_TTL_S - 1.0);
    assert!(e.ap.session_device(&c).unwrap().is_some());
    e.clock.advance(2.0);
    assert_eq!(e.ap.session_device(&c).unwrap(), None);
    let c2 = e.ap.session_new(&d, None).unwrap();
    e.ap.session_end(&c2).unwrap();
    assert_eq!(e.ap.session_device(&c2).unwrap(), None);
}

#[tokio::test]
async fn only_the_cookies_hash_is_stored() {
    let e = env();
    let (d, _) = e.enrol("phone");
    let c = e.ap.session_new(&d, None).unwrap();
    let raw = unb64u(&c).unwrap();
    assert_eq!(raw.len(), 32);
    let db = rusqlite::Connection::open(e.dir.path().join("approver.db")).unwrap();
    let (sha, device, created, expires): (String, String, f64, f64) =
        db.query_row("SELECT id_sha, device, created, expires FROM device_sessions", [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))).unwrap();
    assert_eq!(sha, hex::encode(Sha256::digest(&raw)));
    assert_eq!(device, d);
    assert_eq!(expires - created, SESSION_TTL_S);
    drop(db);
    for f in ["approver.db", "approver.db-wal"] {
        if let Ok(bytes) = std::fs::read(e.dir.path().join(f)) {
            assert!(!bytes.windows(c.len()).any(|w| w == c.as_bytes()), "{f} holds the cookie");
            assert!(!bytes.windows(raw.len()).any(|w| w == raw.as_slice()), "{f} holds the raw cookie");
        }
    }
}

#[tokio::test]
async fn signin_takes_one_tap_and_a_nonce_once() {
    let e = env();
    let (d, mut pk) = e.enrol("phone");
    let b = e.ap.signin_begin().unwrap();
    assert_eq!(b["options"]["userVerification"], "required");
    let a = pk.assert(&b["options"]);
    let (device, cookie) = e.ap.signin_finish(s(&b["nonce"]), &a, Some("203.0.113.5")).unwrap();
    assert_eq!(device, d);
    assert_eq!(e.ap.session_device(&cookie).unwrap().as_deref(), Some(d.as_str()));
    let again = pk.assert(&b["options"]);
    assert_eq!(code(e.ap.signin_finish(s(&b["nonce"]), &again, None)), "bad_login");
}

#[tokio::test]
async fn a_signin_nonce_dies_after_5_minutes() {
    let e = env();
    let (_, mut pk) = e.enrol("phone");
    let b = e.ap.signin_begin().unwrap();
    let a = pk.assert(&b["options"]);
    e.clock.advance(CHALLENGE_TTL_S + 1.0);
    assert_eq!(code(e.ap.signin_finish(s(&b["nonce"]), &a, None)), "bad_login");
    assert_eq!(code(e.ap.spend("never-issued")), "bad_login");
}

#[tokio::test]
async fn signin_refuses_an_assertion_over_another_nonce() {
    let e = env();
    let (_, mut pk) = e.enrol("phone");
    let b1 = e.ap.signin_begin().unwrap();
    let b2 = e.ap.signin_begin().unwrap();
    let a = pk.assert(&b1["options"]);
    assert_eq!(code(e.ap.signin_finish(s(&b2["nonce"]), &a, None)), "bad_passkey");
}

#[tokio::test]
async fn the_rate_signal_counts_a_minute() {
    let e = env_with(3);
    let (d, _) = e.enrol("phone");
    for _ in 0..3 {
        e.ap.note_action(&d, "agent").unwrap();
    }
    assert_eq!(code(e.ap.note_action(&d, "agent")), "rate");
    let (other, _) = e.enrol("laptop");
    e.ap.note_action(&other, "agent").unwrap();
    e.clock.advance(61.0);
    e.ap.note_action(&d, "agent").unwrap();
}

// -- portal sign-in (R84) ------------------------------------------------------------------

#[tokio::test]
async fn portal_grant_verifies_against_the_grant_key() {
    let e = env();
    let (d, _) = e.enrol("phone");
    let url = e.ap.portal_grant(&format!("https://{PORTAL}/trilium/?x=1"), &d).unwrap();
    let prefix = format!("https://{PORTAL}/__auth/portal?");
    assert!(url.starts_with(&prefix), "{url}");
    let q: std::collections::HashMap<String, String> = url::form_urlencoded::parse(&url.as_bytes()[prefix.len()..]).into_owned().collect();
    // As Python: the return is the path alone.
    assert_eq!(q["next"], "/trilium/");
    assert!(url.ends_with("&next=%2Ftrilium%2F"), "{url}");
    let grant: Value = serde_json::from_slice(&unb64u(&q["grant"]).unwrap()).unwrap();
    let payload = json!({"site": PORTAL, "return": "/trilium/"});
    let body = verify_grant(&grant, &e.grant_pub(), &Expect { kind: Some("portal_login"), node: Some(PORTAL_NODE), payload: Some(&payload), ..Expect::default() }, e.now()).unwrap();
    assert_eq!(body["approved_by_device"], d.as_str());
    assert_eq!(body["not_after"].as_i64().unwrap() - body["not_before"].as_i64().unwrap(), 60);
    assert!(verify_grant(&grant, &e.grant_pub(), &Expect::default(), e.now() + 61).is_err());
    assert_eq!(e.ap.portal(&format!("https://{PORTAL}")).unwrap(), (PORTAL.to_string(), PORTAL_NODE.to_string(), "/".to_string()));
}

#[tokio::test]
async fn portal_takes_only_a_known_https_portal() {
    let e = env();
    for next in [
        "https://evil.example.test/trilium/".to_string(),
        format!("http://{PORTAL}/trilium/"),
        format!("https://{PORTAL}:8443/"),
        format!("https://{PORTAL}//evil.example.test/x"),
        "javascript:alert(1)".into(),
        String::new(),
        "/trilium/".into(),
    ] {
        let err = e.ap.portal(&next).expect_err(&next);
        assert_eq!(err.code, "bad_request", "{next}");
        assert!(err.message.contains("known portal"), "{next}: {}", err.message);
    }
}

/// `(kind, node, minutes, payload, request_id)`.
type Case<'a> = (&'a str, Option<&'a str>, i64, Value, Option<&'a str>);

// -- approval requests on the requester's task (fleet.md R97 item 1) -----------------------------

#[tokio::test]
async fn request_validation() {
    let e = env();
    let pubkey = ssh_pubkey();
    let cases: Vec<Case> = vec![
        ("nope", None, 30, json!({}), None),
        ("admin_ssh", None, 30, json!({"pubkey": "x", "nodes": ["alcor"]}), None),
        ("admin_ssh", None, 30, json!({"pubkey": "ssh-ed25519 AAAA", "nodes": []}), None),
        ("admin_ssh", None, 30, json!({"pubkey": pubkey, "nodes": ["Not_A_Node"]}), None),
        ("admin_ssh", None, 30, json!({"pubkey": pubkey}), None),
        ("trust_raise", Some("mizar"), 5, trust_payload(), None),
        ("trust_raise", Some("mizar"), 14, trust_payload(), None),
        ("trust_raise", Some("mizar"), 1441, trust_payload(), None),
        ("trust_raise", Some("mizar"), 30, json!({"session_id": "cc-1", "from": "low"}), None),
        ("trust_raise", Some("Bad Node"), 30, trust_payload(), None),
        ("browser_signin", None, 30, json!({"seat": "s1"}), None),
        ("vault_hold", Some("deneb"), 30, json!(["not", "an", "object"]), None),
        ("plugin_commits", None, 30, json!({"plugins": []}), None),
        ("plugin_commits", None, 30, json!({"plugins": [{"plugin": "zotero", "commit": "abc"}]}), None),
        ("plugin_commits", None, 30, json!({"plugins": [{"plugin": "zotero", "commit": A}, {"plugin": "zotero", "commit": B}]}), None),
        ("plugin_commits", None, 30, json!({"plugins": [{"plugin": "zotero", "commit": A, "extra": 1}]}), None),
        ("plugin_commits", None, 30, json!({"plugins": [{"plugin": "zotero", "commit": A, "from": "abc"}]}), None),
        ("plugin_commits", None, 30, json!({"plugins": [{"plugin": "Zotero", "commit": A}]}), None),
        ("vault_hold", Some("deneb"), 30, json!({}), Some("bad")),
        ("vault_hold", Some("deneb"), 30, json!({}), Some("qtooshort")),
    ];
    for (kind, node, minutes, payload, rid) in cases {
        let r = e.ap.approval_request(kind, node, "r", minutes, payload.clone(), None, rid).await;
        assert_eq!(code(r), "bad_request", "{kind} {node:?} {minutes} {payload} {rid:?}");
    }
    for m in [15, 1440] {
        e.ap.approval_request("trust_raise", Some("mizar"), "r", m, trust_payload(), None, None).await.unwrap();
    }
    let kinds = request_kinds();
    for k in ["admin_ssh", "trust_raise", "invite", "browser_signin", "vault_hold", "plugin_commits", "device_enrol"] {
        assert!(kinds.contains(&k), "{k}");
    }
}

#[tokio::test]
async fn a_relay_minted_request_id_is_kept() {
    let e = env();
    let rid = format!("q{}", "A".repeat(12));
    let r = e.ap.approval_request("vault_hold", Some("deneb"), "r", 30, json!({}), None, Some(&rid)).await.unwrap();
    assert_eq!(r["request_id"], rid.as_str());
    let again = e.ap.approval_request("vault_hold", Some("deneb"), "r", 30, json!({}), None, Some(&rid)).await;
    let err = again.expect_err("taken");
    assert!(err.message.contains("taken"), "{}", err.message);
}

#[tokio::test]
async fn a_request_attaches_to_the_requesters_task() {
    let e = env();
    let opened = e.store.task_open("Fix the tunnel", Open { session_id: Some("cc-1".into()), node: Some("mizar".into()), ..Open::default() }, None).unwrap();
    let task = s(&opened["task_id"]).to_string();
    let r1 = e.ap.approval_request("trust_raise", Some("mizar"), "needs high", 30, trust_payload(), Some("cc-1"), None).await.unwrap();
    assert_eq!(r1["task_id"], task.as_str());
    let card = e.store.task_get(&task).unwrap();
    assert_eq!((card["section"].as_str(), card["status"].as_str()), (Some("needs_you"), Some("approval")));
    assert_eq!(card["title"], "Fix the tunnel");
    assert_eq!(card["meta"]["reason"], "trust_raise: needs high (30 min)");
    let r2 = e.hold(Some("cc-1")).await;
    assert_eq!(r2["task_id"], task.as_str());
    assert_eq!(r1["expires"].as_f64().unwrap(), e.clock.now() + REQUEST_TTL_S);

    let (d, mut pk) = e.enrol("phone");
    let a = e.approval_assertion(&mut pk, s(&r1["request_id"]));
    e.ap.approve(s(&r1["request_id"]), &a).await.unwrap();
    let card = e.store.task_get(&task).unwrap();
    assert_eq!(card["section"], "needs_you", "another request of the task still waits");
    e.clock.advance(5.0);
    e.ap.reject(s(&r2["request_id"]), &d).await.unwrap();
    let card = e.store.task_get(&task).unwrap();
    assert_eq!(card["section"], "working");
    assert_eq!(card["status"], Value::Null);
    assert_eq!(card["meta"]["reason"], Value::Null);

    let list = e.ap.approvals_of(&task);
    assert_eq!(list.len(), 2);
    assert_eq!(list[0]["request_id"], r1["request_id"]);
    assert_eq!(list[0]["state"], "approved");
    assert_eq!(list[0]["summary"], "trust_raise: needs high (30 min)");
    assert_eq!(list[1]["state"], "rejected");
    assert_eq!(list[1]["summary"], "vault_hold: checks tripped (30 min)");
    for row in &list {
        let keys: Vec<&String> = row.as_object().unwrap().keys().collect();
        assert_eq!(keys.len(), 5, "{row}");
        assert!(row["requested_at"].as_f64().is_some() && row["decided_at"].as_f64().is_some(), "{row}");
    }
    assert!(list[1]["decided_at"].as_f64() > list[0]["decided_at"].as_f64());
}

#[tokio::test]
async fn a_session_with_no_card_gets_one() {
    let e = env();
    let r = e.hold(Some("cc-new")).await;
    let task = s(&r["task_id"]).to_string();
    assert_eq!(e.store.task_for_session("cc-new").unwrap().as_deref(), Some(task.as_str()));
    let card = e.store.task_get(&task).unwrap();
    assert_eq!((card["section"].as_str(), card["status"].as_str()), (Some("needs_you"), Some("approval")));
    assert_eq!(card["title"], "vault_hold on deneb: checks tripped");
    let r2 = e.hold(Some("cc-new")).await;
    assert_eq!(r2["task_id"], task.as_str());
    let anon1 = e.hold(None).await;
    let anon2 = e.hold(None).await;
    assert_ne!(anon1["task_id"], anon2["task_id"]);
    assert_ne!(anon1["task_id"], task.as_str());
}

#[tokio::test]
async fn approvals_of_reads_a_lapsed_request_as_expired() {
    let e = env();
    let r = e.hold(Some("cc-1")).await;
    let task = s(&r["task_id"]).to_string();
    assert_eq!(e.ap.approvals_of(&task)[0]["state"], "pending");
    assert_eq!(e.ap.approvals_of(&task)[0]["decided_at"], Value::Null);
    e.clock.advance(REQUEST_TTL_S + 1.0);
    assert_eq!(e.ap.approvals_of(&task)[0]["state"], "expired");
    assert_eq!(e.ap.approval_get(s(&r["request_id"])).unwrap()["status"], "expired");
    assert!(e.ap.approvals_of("ZZZZZZZZ").is_empty());
}

#[tokio::test]
async fn sweep_expires_and_a_decided_request_stays_decided() {
    let e = env();
    let (_, mut pk) = e.enrol("phone");
    let r = e.hold(Some("cc-1")).await;
    let rid = s(&r["request_id"]).to_string();
    let a = e.approval_assertion(&mut pk, &rid);
    assert!(e.ap.next_expiry().is_some());
    e.clock.advance(REQUEST_TTL_S + 1.0);
    e.ap.sweep().await;
    assert_eq!(e.ap.approval_get(&rid).unwrap()["status"], "expired");
    assert_eq!(e.ap.next_expiry(), None);
    let card = e.store.task_get(s(&r["task_id"])).unwrap();
    assert_eq!(card["section"], "working");
    assert_eq!(code(e.ap.approve(&rid, &a).await), "decided");
    assert_eq!(code(e.ap.reject(&rid, "dX").await), "decided");
}

#[tokio::test]
async fn reject_and_wait() {
    let e = env();
    let (d, _) = e.enrol("phone");
    let r = e.ap.approval_request("trust_raise", Some("mizar"), "r", 30, trust_payload(), None, None).await.unwrap();
    let rid = s(&r["request_id"]).to_string();
    let pending = e.ap.approval_wait(&rid, 0.0).await.unwrap();
    assert_eq!(pending["status"], "pending");
    let out = e.ap.reject(&rid, &d).await.unwrap();
    assert_eq!(out["status"], "denied");
    let got = e.ap.approval_wait(&rid, 1.0).await.unwrap();
    assert_eq!(got["status"], "denied");
    assert_eq!(got["grant"], Value::Null);
    assert_eq!(code(e.ap.reject(&rid, &d).await), "decided");
    assert_eq!(code(e.ap.approval_get("qZZZZZZZZZZZZ")), "unknown_request");
}

#[tokio::test]
async fn only_the_requester_cancels() {
    let e = env();
    let me = format!("cc{}", "1".repeat(32));
    let r = e.ap.approval_request("trust_raise", Some("mizar"), "r", 30, trust_payload(), Some(&me), None).await.unwrap();
    let rid = s(&r["request_id"]).to_string();
    for who in [None, Some(format!("cc{}", "2".repeat(32)))] {
        assert_eq!(code(e.ap.approval_cancel(&rid, who.as_deref()).await), "not_requester");
    }
    let anon = e.hold(None).await;
    assert_eq!(code(e.ap.approval_cancel(s(&anon["request_id"]), None).await), "not_requester");
    let out = e.ap.approval_cancel(&rid, Some(&me)).await.unwrap();
    assert_eq!(out["status"], "cancelled");
    let card = e.store.task_get(s(&r["task_id"])).unwrap();
    assert_eq!(card["section"], "working");
    assert_eq!(e.ap.approvals_of(s(&r["task_id"]))[0]["state"], "cancelled");
}

#[tokio::test]
async fn a_waiting_request_wakes_on_decision() {
    let e = env();
    let (_, mut pk) = e.enrol("phone");
    let r = e.ap.approval_request("browser_signin", None, "r", 30, json!({"seat": "s1", "site": "github"}), None, None).await.unwrap();
    let rid = s(&r["request_id"]).to_string();
    let ap = e.ap.clone();
    let id = rid.clone();
    let waiter = tokio::spawn(async move { ap.approval_wait(&id, 30.0).await });
    tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    let a = e.approval_assertion(&mut pk, &rid);
    e.ap.approve(&rid, &a).await.unwrap();
    let got = tokio::time::timeout(std::time::Duration::from_secs(2), waiter).await.unwrap().unwrap().unwrap();
    assert_eq!(got["status"], "approved");
}

// -- approvals and grants ----------------------------------------------------------------------

#[tokio::test]
async fn approve_options_bind_the_request() {
    let e = env();
    e.enrol("phone");
    let r = e.ap.approval_request("trust_raise", Some("mizar"), "r", 30, trust_payload(), None, None).await.unwrap();
    let rid = s(&r["request_id"]);
    let o = e.ap.approve_options(rid).unwrap();
    assert_eq!(o["challenge"], b64u(&approval_challenge(rid, "trust_raise", &trust_payload())).as_str());
    assert_eq!(o["rpId"], RP_ID);
    assert_eq!(o["allowCredentials"].as_array().unwrap().len(), 1);
    let want = Sha256::digest(canonical(&json!({"request_id": rid, "kind": "trust_raise", "payload": trust_payload()}))).to_vec();
    assert_eq!(approval_challenge(rid, "trust_raise", &trust_payload()), want);
    assert_eq!(code(e.ap.approve_options("qZZZZZZZZZZZZ")), "unknown_request");
}

#[tokio::test]
async fn an_assertion_for_one_request_is_refused_on_another() {
    let e = env();
    let (_, mut pk) = e.enrol("phone");
    let ra = e.hold(None).await;
    let rb = e.hold(None).await;
    let for_a = e.approval_assertion(&mut pk, s(&ra["request_id"]));
    assert_eq!(code(e.ap.approve(s(&rb["request_id"]), &for_a).await), "bad_passkey");
    assert_eq!(e.ap.approval_get(s(&rb["request_id"])).unwrap()["status"], "pending");
    assert_eq!(code(e.ap.approve(s(&rb["request_id"]), &Value::Null).await), "need_passkey");
    let mut stranger = SoftAuthenticator::new(ORIGIN, RP_ID);
    let unknown = e.approval_assertion(&mut stranger, s(&rb["request_id"]));
    assert_eq!(code(e.ap.approve(s(&rb["request_id"]), &unknown).await), "unknown_passkey");
    e.ap.approve(s(&ra["request_id"]), &for_a).await.unwrap();
}

#[tokio::test]
async fn a_replayed_sign_count_is_refused() {
    let e = env();
    let (_, mut pk) = e.enrol("phone");
    let ra = e.hold(None).await;
    let rb = e.hold(None).await;
    let old_b = e.approval_assertion(&mut pk, s(&rb["request_id"]));
    let a = e.approval_assertion(&mut pk, s(&ra["request_id"]));
    e.ap.approve(s(&ra["request_id"]), &a).await.unwrap();
    let err = e.ap.approve(s(&rb["request_id"]), &old_b).await.expect_err("replayed count");
    assert_eq!(err.code, "bad_passkey");
    assert!(err.message.contains("sign count"), "{}", err.message);
}

#[tokio::test]
async fn admin_ssh_grant_and_certificate() {
    let e = env();
    let (d, mut pk) = e.enrol("phone");
    let pubkey = ssh_pubkey();
    let payload = json!({"pubkey": pubkey, "nodes": ["capella", "altair"]});
    let r = e.ap.approval_request("admin_ssh", None, "fix sshd", 30, payload.clone(), Some("cc-123"), None).await.unwrap();
    let card = e.store.task_get(s(&r["task_id"])).unwrap();
    assert_eq!((card["section"].as_str(), card["status"].as_str()), (Some("needs_you"), Some("approval")));
    assert_eq!(card["title"], "admin_ssh on capella,altair: fix sshd");
    let rid = s(&r["request_id"]).to_string();
    let a = e.approval_assertion(&mut pk, &rid);
    let out = e.ap.approve(&rid, &a).await.unwrap();
    assert_eq!(out["status"], "approved");
    let got = e.ap.approval_get(&rid).unwrap();
    assert_eq!(got["status"], "approved");
    let gp = e.grant_pub();
    let body = verify_grant(grant_of(&got), &gp, &Expect { kind: Some("admin_ssh"), node: Some("altair"), subject: Some("cc-123"), payload: Some(&payload) }, e.now()).unwrap();
    assert_eq!(body["not_after"].as_i64().unwrap() - body["not_before"].as_i64().unwrap(), 30 * 60);
    assert_eq!(body["not_before"].as_i64().unwrap(), e.now());
    assert_eq!(body["approved_by_device"], d.as_str());
    assert_eq!(body["request_id"], rid.as_str());
    assert_eq!(body["nodes"], json!(["altair", "capella"]));
    assert_eq!(verify_grant(grant_of(&got), &gp, &Expect { node: Some("deneb"), ..Expect::default() }, e.now()), Err(GrantError::WrongNode("deneb".into())));
    assert!(verify_grant(grant_of(&got), &gp, &Expect::default(), e.now() + 30 * 60).is_err(), "the grant lasts its minutes");
    let mut tampered = grant_of(&got).clone();
    tampered["grant"]["not_after"] = json!(body["not_after"].as_i64().unwrap() + 1);
    assert_eq!(verify_grant(&tampered, &gp, &Expect::default(), e.now()), Err(GrantError::BadSignature));
    assert_eq!(got["ssh_cert"], got["cert"]);
    let cert = ssh_key::Certificate::from_openssh(s(&got["cert"])).unwrap();
    assert_eq!(cert.valid_principals(), ["admin@altair", "admin@capella"]);
    assert_eq!(cert.key_id(), s(&body["grant_id"]));
    assert_eq!(cert.valid_after() as i64, body["not_before"].as_i64().unwrap());
    assert_eq!(cert.valid_before() as i64, body["not_after"].as_i64().unwrap());
    let ca = ssh_key::PublicKey::from_openssh(&e.ap.keys.ssh_ca_pub()).unwrap();
    assert_eq!(cert.signature_key(), ca.key_data());
    let view = e.ap.approval_view(&rid).unwrap();
    assert!(view.get("grant").is_none() && view.get("cert").is_none() && view.get("ssh_cert").is_none(), "{view}");
}

#[tokio::test]
async fn a_grant_names_kind_node_subject_and_payload() {
    let e = env();
    let (_, mut pk) = e.enrol("phone");
    let r = e.ap.approval_request("trust_raise", Some("mizar"), "r", 90, trust_payload(), None, None).await.unwrap();
    let rid = s(&r["request_id"]).to_string();
    let a = e.approval_assertion(&mut pk, &rid);
    e.ap.approve(&rid, &a).await.unwrap();
    let got = e.ap.approval_get(&rid).unwrap();
    let gp = e.grant_pub();
    let body = verify_grant(grant_of(&got), &gp, &Expect { kind: Some("trust_raise"), node: Some("mizar"), subject: Some("tony"), payload: Some(&trust_payload()) }, e.now()).unwrap();
    assert_eq!(body["not_after"].as_i64().unwrap() - body["not_before"].as_i64().unwrap(), 90 * 60);
    assert_eq!(body["payload_sha256"], hex::encode(Sha256::digest(canonical(&trust_payload()))).as_str());
    assert!(matches!(verify_grant(grant_of(&got), &gp, &Expect { kind: Some("admin_ssh"), ..Expect::default() }, e.now()), Err(GrantError::Mismatch { .. })));
    assert_eq!(verify_grant(grant_of(&got), &gp, &Expect { payload: Some(&json!({"other": 1})), ..Expect::default() }, e.now()), Err(GrantError::WrongPayload));
    assert!(matches!(verify_grant(grant_of(&got), &gp, &Expect { subject: Some("cc-9"), ..Expect::default() }, e.now()), Err(GrantError::Mismatch { .. })));
}

#[tokio::test]
async fn vault_hold_needs_no_payload() {
    let e = env();
    let (_, mut pk) = e.enrol("phone");
    let r = e.ap.approval_request("vault_hold", Some("deneb"), "checks tripped", 30, Value::Null, None, None).await.unwrap();
    let rid = s(&r["request_id"]).to_string();
    let a = e.approval_assertion(&mut pk, &rid);
    e.ap.approve(&rid, &a).await.unwrap();
    let got = e.ap.approval_get(&rid).unwrap();
    assert_eq!(got["status"], "approved");
    assert_eq!(got["payload"], json!({}));
    verify_grant(grant_of(&got), &e.grant_pub(), &Expect { kind: Some("vault_hold"), node: Some("deneb"), payload: Some(&json!({})), ..Expect::default() }, e.now()).unwrap();
}

#[tokio::test]
async fn plugin_commits_grant_names_every_commit() {
    let e = env();
    let (_, mut pk) = e.enrol("phone");
    let payload = json!({"change": "c1", "plugins": [
        {"plugin": "zotero", "commit": B, "from": A},
        {"plugin": "engcyc", "commit": A, "source": "git@example.test:engcyc.git"}]});
    let r = e.ap.approval_request("plugin_commits", None, "zotero sync and a new engcyc", 30, payload, None, None).await.unwrap();
    let rid = s(&r["request_id"]).to_string();
    let a = e.approval_assertion(&mut pk, &rid);
    e.ap.approve(&rid, &a).await.unwrap();
    let body = &e.ap.approval_get(&rid).unwrap()["grant"]["grant"];
    assert_eq!(body["kind"], "plugin_commits");
    assert_eq!(body["commits"], json!([format!("engcyc@{A}"), format!("zotero@{B}")]));
}

// -- orders (R97: alcor never acts on another node) ----------------------------------------------

#[tokio::test]
async fn agent_opens_a_card_and_an_order_the_node_verifies() {
    let e = env();
    assert_eq!(code(e.ap.agent("deneb", None).await), "not_allowed");
    assert_eq!(code(e.ap.agent("Bad Node", None).await), "not_allowed");
    let long = "n".repeat(65);
    for bad in ["", "has space", "x/../y", long.as_str()] {
        assert_eq!(code(e.ap.agent(AGENT_NODE, Some(bad)).await), "bad_request", "{bad:?}");
    }
    assert!(e.ap.order_pull(AGENT_NODE, None, 0.0).await.unwrap().as_array().unwrap().is_empty(), "a refused agent issues no order");
    let out = e.ap.agent(AGENT_NODE, Some("note_1-x")).await.unwrap();
    let task = s(&out["task_id"]).to_string();
    let card = e.store.task_get(&task).unwrap();
    assert_eq!(card["section"], "needs_you");
    assert_eq!(card["source_note"], "note_1-x");
    assert_eq!(card["peer"], AGENT_NODE);
    let envs = e.ap.order_pull(AGENT_NODE, None, 0.0).await.unwrap();
    let envs = envs.as_array().unwrap();
    assert_eq!(envs.len(), 1);
    let gp = e.grant_pub();
    let body = verify_order(&envs[0], &gp, AGENT_NODE, e.now()).unwrap();
    assert_eq!(body["kind"], "agent");
    assert_eq!(body["order_id"], out["order_id"]);
    assert_eq!(body["payload"], json!({"task_id": task, "node": AGENT_NODE, "note_id": "note_1-x"}));
    assert_eq!(body["not_after"].as_i64().unwrap(), e.now() + 600);
    assert!(matches!(verify_order(&envs[0], &gp, "deneb", e.now()), Err(GrantError::WrongOrderNode { .. })));
    assert_eq!(verify_order(&envs[0], &gp, AGENT_NODE, e.now() + 600), Err(GrantError::Expired));
    let row = e.ap.order_get(s(&body["order_id"])).unwrap().unwrap();
    assert!(row["pulled_at"].as_f64().is_some());

    let plain = e.ap.agent(AGENT_NODE, None).await.unwrap();
    let envs = e.ap.order_pull(AGENT_NODE, None, 0.0).await.unwrap();
    let last = envs.as_array().unwrap().iter().find(|x| x["order"]["order_id"] == plain["order_id"]).unwrap().clone();
    assert_eq!(last["order"]["payload"], json!({"task_id": plain["task_id"], "node": AGENT_NODE}));
    assert_eq!(e.store.task_get(s(&plain["task_id"])).unwrap()["source_note"], Value::Null);
}

#[tokio::test]
async fn orders_pull_by_kind_and_are_done_once() {
    let e = env();
    let env1 = e.ap.issue_order("agent", AGENT_NODE, json!({"task_id": "AAAAAAAA", "node": AGENT_NODE})).unwrap();
    let id = s(&env1["order"]["order_id"]).to_string();
    let other = vec!["other_kind".to_string()];
    assert!(e.ap.order_pull(AGENT_NODE, Some(other.as_slice()), 0.0).await.unwrap().as_array().unwrap().is_empty());
    let kinds = vec!["agent".to_string()];
    assert_eq!(e.ap.order_pull(AGENT_NODE, Some(kinds.as_slice()), 0.0).await.unwrap().as_array().unwrap().len(), 1);
    assert_eq!(e.ap.order_pull(AGENT_NODE, Some(Vec::<String>::new().as_slice()), 0.0).await.unwrap().as_array().unwrap().len(), 1);
    assert!(e.ap.order_pull("deneb", None, 0.0).await.unwrap().as_array().unwrap().is_empty());
    assert_eq!(e.ap.order_done(&id, true, "started").unwrap()["recorded"], true);
    assert_eq!(e.ap.order_done(&id, false, "again").unwrap()["recorded"], false);
    let row = e.ap.order_get(&id).unwrap().unwrap();
    assert_eq!((row["ok"].as_i64(), row["note"].as_str()), (Some(1), Some("started")));
    assert!(e.ap.order_pull(AGENT_NODE, None, 0.0).await.unwrap().as_array().unwrap().is_empty());
    assert_eq!(e.ap.order_get("oZZZZZZZZZZZZ").unwrap(), None);
}

#[tokio::test]
async fn an_order_lapses_after_10_minutes() {
    let e = env();
    e.ap.issue_order("agent", AGENT_NODE, json!({})).unwrap();
    e.clock.advance(601.0);
    assert!(e.ap.order_pull(AGENT_NODE, None, 0.0).await.unwrap().as_array().unwrap().is_empty());
}

#[tokio::test]
async fn order_pull_long_polls() {
    let e = env();
    let ap = e.ap.clone();
    let waiter = tokio::spawn(async move { ap.order_pull(AGENT_NODE, None, 5.0).await });
    tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    e.ap.issue_order("agent", AGENT_NODE, json!({})).unwrap();
    let got = tokio::time::timeout(std::time::Duration::from_secs(2), waiter).await.unwrap().unwrap().unwrap();
    assert_eq!(got.as_array().unwrap().len(), 1);
}

// -- devices ------------------------------------------------------------------------------------

#[tokio::test]
async fn a_revoked_device_stops() {
    let e = env();
    let (d, mut pk) = e.enrol("phone");
    let (keep, _) = e.enrol("laptop");
    let c1 = e.ap.session_new(&d, None).unwrap();
    let c2 = e.ap.session_new(&d, None).unwrap();
    let ck = e.ap.session_new(&keep, None).unwrap();
    let b = e.ap.signin_begin().unwrap();
    assert_eq!(b["options"]["allowCredentials"].as_array().unwrap().len(), 2);
    let out = e.ap.device_revoke(&d).unwrap();
    assert_eq!(out, json!({"device_id": d, "revoked": true, "sessions_ended": 2}));
    assert_eq!(e.ap.session_device(&c1).unwrap(), None);
    assert_eq!(e.ap.session_device(&c2).unwrap(), None);
    assert_eq!(e.ap.session_device(&ck).unwrap().as_deref(), Some(keep.as_str()));
    let a = pk.assert(&b["options"]);
    assert_eq!(code(e.ap.signin_finish(s(&b["nonce"]), &a, None)), "unknown_passkey");
    let r = e.hold(None).await;
    let a = e.approval_assertion(&mut pk, s(&r["request_id"]));
    assert_eq!(code(e.ap.approve(s(&r["request_id"]), &a).await), "unknown_passkey");
    assert_eq!(e.ap.signin_begin().unwrap()["options"]["allowCredentials"].as_array().unwrap().len(), 1);
    assert_eq!(e.ap.device_revoke(&d).unwrap()["revoked"], false);
    let list = e.ap.devices().unwrap();
    let row = list.as_array().unwrap().iter().find(|x| x["device_id"] == d.as_str()).unwrap();
    assert!(row["revoked_at"].as_f64().is_some());
    // A session minted for a revoked device is dead on arrival.
    let late = e.ap.session_new(&d, None).unwrap();
    assert_eq!(e.ap.session_device(&late).unwrap(), None);
}

// -- the 2fa burst (R137) -------------------------------------------------------------------------

#[tokio::test]
async fn twofa_state_reads_the_2fa_verbs() {
    let e = env();
    let st = e.ap.twofa_state().await.unwrap();
    assert_eq!(st["devices"], json!([{"name": "phone"}]));
    assert_eq!(st["pending"]["phone"][0]["urgid"], "u1");
    let verbs: Vec<String> = e.tools.calls().into_iter().map(|c| c.0).collect();
    assert_eq!(verbs, ["devices", "pending"]);
}

#[tokio::test]
async fn burst_takes_1_2_or_4_and_a_nonce_once() {
    let e = env();
    let (_, mut pk) = e.enrol("phone");
    for bad in [0, 3, 5, 8] {
        assert_eq!(code(e.ap.burst_options(bad, "phone")), "bad_request", "{bad}");
    }
    let o = e.ap.burst_options(2, "phone").unwrap();
    let n = s(&o["nonce"]).to_string();
    let a = pk.assert(&o["options"]);
    assert_eq!(code(e.ap.burst(&n, 3, "phone", &a).await), "bad_request");
    let out = e.ap.burst(&n, 2, "phone", &a).await.unwrap();
    assert_eq!(out, json!({"ok": true}));
    assert_eq!(e.tools.calls(), vec![("burst".to_string(), json!({"device": "phone", "count": 2}))]);
    let a2 = pk.assert(&o["options"]);
    assert_eq!(code(e.ap.burst(&n, 2, "phone", &a2).await), "bad_login");
    assert_eq!(e.tools.calls().len(), 1);
}

#[tokio::test]
async fn burst_binds_count_and_device() {
    let e = env();
    let (_, mut pk) = e.enrol("phone");
    let o = e.ap.burst_options(1, "phone").unwrap();
    let a = pk.assert(&o["options"]);
    assert_eq!(code(e.ap.burst(s(&o["nonce"]), 4, "phone", &a).await), "bad_passkey");
    let o = e.ap.burst_options(4, "phone").unwrap();
    let a = pk.assert(&o["options"]);
    assert_eq!(code(e.ap.burst(s(&o["nonce"]), 4, "tablet", &a).await), "bad_passkey");
    assert!(e.tools.calls().is_empty());
    let o = e.ap.burst_options(4, "phone").unwrap();
    let a = pk.assert(&o["options"]);
    e.ap.burst(s(&o["nonce"]), 4, "phone", &a).await.unwrap();
    assert_eq!(e.tools.calls(), vec![("burst".to_string(), json!({"device": "phone", "count": 4}))]);
}
