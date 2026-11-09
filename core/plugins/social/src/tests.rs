//! The plugin's surface against the Python one.

use std::collections::BTreeSet;

use serde_json::{json, Value};

/// The surface changes the port plan lists; every other verb is the Python's byte for byte.
const REMOVED: [&str; 10] = ["keepers", "keep_reset", "keep_adopt", "bucket_sites", "bucket_versions", "bucket_restore", "bucket_share", "bucket_shares", "bucket_unshare", "bucket_resolve"];
const ADDED: [&str; 4] = ["forward", "seat_remote_control", "seat_pull", "seat_report"];
const REWORDED: [&str; 10] = ["send", "react", "seats", "mount", "search", "buckets", "bucket_ls", "bucket_get", "bucket_mv", "bucket_search"];

#[test]
fn the_surface_is_the_python_one_but_the_listed_changes() {
    let golden: Value = serde_json::from_str(include_str!("../tests/golden/social.api.json")).unwrap();
    let ours: Value = serde_json::from_str(crate::MANIFEST).unwrap();
    let names = |v: &Value| v["functions"].as_array().unwrap().iter().map(|f| f["name"].as_str().unwrap().to_string()).collect::<BTreeSet<_>>();
    let (g, o) = (names(&golden), names(&ours));
    assert_eq!(g.difference(&o).cloned().collect::<BTreeSet<_>>(), REMOVED.iter().map(|s| s.to_string()).collect());
    assert_eq!(o.difference(&g).cloned().collect::<BTreeSet<_>>(), ADDED.iter().map(|s| s.to_string()).collect());
    let mut emitters = golden["emitters"].as_array().unwrap().clone();
    emitters.push(json!({"topic": "seats"}));
    assert_eq!((ours["emitters"].clone(), ours["sessions"].clone()), (json!(emitters), golden["sessions"].clone()));
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

fn test_env() -> awm_plugin_sdk::Env {
    awm_plugin_sdk::Env { hub_url: "http://127.0.0.1:1".into(), service: "social".into(), plugin: "social".into(), node: "testnode".into(), awm_dir: std::env::temp_dir(), token_file: None }
}

// -- the verbs over a fake connector ---------------------------------------------------

mod verbs {
    use std::sync::{Arc, Mutex};

    use async_trait::async_trait;
    use awm_plugin_sdk::{Caller, Level, TestService};
    use serde_json::{json, Value};

    use crate::config::{AccountConfig, SeatChat, SeatRule, SeatsConfig};
    use crate::connectors::{self, Channel, Connector, Identity, Inbound, OutFile, Sent};

    #[derive(Default)]
    struct Fake {
        cfg: AccountConfig,
        sent: Mutex<Vec<(String, String, Option<String>)>>,
    }

    #[async_trait]
    impl Connector for Fake {
        fn platform(&self) -> &str {
            "slack"
        }
        fn account(&self) -> &AccountConfig {
            &self.cfg
        }
        fn keeps_history(&self) -> bool {
            true
        }
        async fn start(self: Arc<Self>) -> anyhow::Result<()> {
            Ok(())
        }
        async fn send(&self, channel: &str, text: &str, thread: Option<&str>, _f: &[OutFile]) -> connectors::Result<Sent> {
            self.sent.lock().unwrap().push((channel.into(), text.into(), thread.map(str::to_string)));
            Ok(serde_json::from_value(json!({"message_id": "9.9", "channel_id": channel, "ts": "9.9"})).unwrap())
        }
        async fn list_channels(&self, _dms: bool) -> connectors::Result<Vec<Channel>> {
            self.list_seats().await
        }
        async fn list_seats(&self) -> connectors::Result<Vec<Channel>> {
            Ok(vec![
                Channel { id: "D0TEST".into(), name: "tester".into(), kind: "dm".into() },
                Channel { id: "C0TEST".into(), name: "general".into(), kind: "channel".into() },
                Channel { id: "D0OPER".into(), name: "operator".into(), kind: "dm".into() },
            ])
        }
        async fn identity(&self) -> connectors::Result<Identity> {
            Ok(Identity::default())
        }
        fn live(&self) -> bool {
            true
        }
    }

    struct Rig {
        svc: TestService,
        app: Arc<crate::app::Social>,
        fake: Arc<Fake>,
        _tmp: tempfile::TempDir,
    }

    async fn rig() -> Rig {
        let tmp = tempfile::tempdir().unwrap();
        let db = crate::db::Db::open(&tmp.path().join("social.db")).unwrap();
        let account = AccountConfig { name: "opal".into(), platform: "slack".into(), token: "xoxb-test".into(), trust: "cooperative".into(), trusted_chats: vec!["D0OPER".into()], enabled: true, ..Default::default() };
        let fake = Arc::new(Fake { cfg: account.clone(), ..Default::default() });
        let conns: crate::app::Conns = Default::default();
        conns.write().unwrap().insert("opal".into(), fake.clone());
        let cfg = SeatsConfig {
            paused: true,
            rules: vec![SeatRule { account: "opal".into(), kind: "dm".into(), node: "altair".into(), level: Level::Cooperative, scope: "seats/{slug}".into(), except: vec![], agent: None }],
            chats: vec![SeatChat { account: "opal".into(), channel: "D0OPER".into(), node: "altair".into(), level: Level::Trusted, scope: "seats/operator".into(), agent: None }],
            ..Default::default()
        };
        let seats = crate::seats::Seats::new(db.clone(), cfg, vec![account.clone()]);
        let hub = awm_plugin_sdk::Hub::new("http://127.0.0.1:1", None);
        let app = crate::app::Social::new("alcor".into(), hub, db, vec![account], conns, vec![], seats);
        let svc = crate::service(awm_plugin_sdk::Service::new(super::test_env()), app.clone()).into_test().await.unwrap();
        Rig { svc, app, fake, _tmp: tmp }
    }

    /// A call from job `job` on altair at `level`, as the door stamps it.
    fn from(job: &str, level: &str, mut args: Value) -> Value {
        args["_caller_trust"] = json!(level);
        args["_caller_session"] = json!({"kind": "bg", "job": job, "name": format!("seat:{job}")});
        args
    }

    impl Rig {
        async fn call(&self, verb: &str, job: &str, level: &str, args: Value) -> Result<Value, awm_plugin_sdk::VerbError> {
            self.svc.call_as(verb, from(job, level, args), Caller::default(), Some("peer:altair")).await
        }
    }

    #[tokio::test]
    async fn mounting_a_declared_dm_adopts_it_and_send_defaults_to_the_latest_line() {
        let r = rig().await;
        let out = r.call("mount", "j1", "cooperative", json!({"account": "opal", "channel": "D0TEST"})).await.unwrap();
        assert_eq!(out["ok"], true, "{out}");
        let seat = r.app.seats.store.get("opal", "D0TEST").await.unwrap();
        assert_eq!(seat.node, "altair");
        let rows = r.app.seats.store.waiting("altair", 10).await;
        assert_eq!((rows[0].kind.as_str(), rows[0].body["job"].as_str()), ("adopt", Some("j1")), "the host is told to adopt it");
        assert!(r.call("send", "j1", "cooperative", json!({"text": "hi"})).await.unwrap_err().message.contains("No pushed line"));
        r.app.seats.store.set_line("altair", "j1", "opal", "D0TEST", "5.0", "6.0", 1.0).await;
        r.call("send", "j1", "cooperative", json!({"text": "answer"})).await.unwrap();
        r.call("send", "j1", "cooperative", json!({"channel": "DM", "text": "by label"})).await.unwrap();
        let sent = r.fake.sent.lock().unwrap().clone();
        assert_eq!(sent[0], ("D0TEST".into(), "answer".into(), Some("5.0".into())));
        assert_eq!(sent[1].0, "D0TEST", "the label DM names the held DM");
    }

    #[tokio::test]
    async fn a_cooperative_session_answers_only_what_it_holds_and_never_a_trusted_chat() {
        let r = rig().await;
        r.call("mount", "j1", "cooperative", json!({"account": "opal", "channel": "D0TEST"})).await.unwrap();
        let e = r.call("send", "j1", "cooperative", json!({"account": "opal", "channel": "C0TEST", "text": "x"})).await.unwrap_err();
        assert_eq!(e.error_class, "PermissionError");
        let out = r.call("mount", "j2", "cooperative", json!({"account": "opal", "channel": "D0OPER", "force": true})).await.unwrap();
        assert!(out["error"].as_str().unwrap().contains("cannot hold a trusted chat"), "{out}");
        let held = r.call("mount", "j2", "cooperative", json!({"account": "opal", "channel": "D0TEST"})).await.unwrap();
        assert!(held["error"].as_str().unwrap().contains("Pass force=true"));
    }

    #[tokio::test]
    async fn history_is_bound_to_held_chats_and_a_forward_opens_one_for_a_day() {
        let r = rig().await;
        let h = crate::db::History::new(r.app.db.clone(), "opal");
        for ch in ["D0TEST", "C0TEST"] {
            h.record(&Inbound { channel_id: ch.into(), message_id: format!("{ch}-1"), ts: "1.0".into(), ..Default::default() }, false, false).await;
        }
        r.call("mount", "j1", "cooperative", json!({"account": "opal", "channel": "C0TEST"})).await.unwrap();
        let got = r.call("history", "j1", "cooperative", json!({"account": "opal"})).await.unwrap();
        assert_eq!(got["count"], 1);
        assert!(r.call("history", "j1", "cooperative", json!({"account": "opal", "channel": "D0TEST"})).await.is_err());
        r.app.seats.store.grant_read(("opal", "C0TEST"), ("opal", "D0TEST"), crate::db::now() + 60.0).await;
        assert_eq!(r.call("history", "j1", "cooperative", json!({"account": "opal"})).await.unwrap()["count"], 2);
        assert_eq!(r.call("history", "x", "trusted", json!({"account": "opal"})).await.unwrap()["count"], 2, "an unbound trusted read");
    }

    #[test]
    fn only_operations_on_this_node_flips_remote_control() {
        use crate::app::switch_caller;
        assert!(switch_caller(Some("operations"), false));
        assert!(!switch_caller(Some("operations"), true), "a peer");
        assert!(!switch_caller(None, false), "the CLI");
        assert!(!switch_caller(Some("trilium"), false), "another plugin");
    }

    #[tokio::test]
    async fn the_cli_and_a_peer_cannot_flip_remote_control() {
        let r = rig().await;
        let args = json!({"seat": "opal/D0OPER", "on": true});
        assert_eq!(r.svc.call("seat_remote_control", args.clone()).await.unwrap_err().error_class, "PermissionError");
        assert_eq!(r.call("seat_remote_control", "op", "trusted", args).await.unwrap_err().error_class, "PermissionError");
    }

    #[tokio::test]
    async fn a_reply_in_a_quiet_thread_reaches_nobody() {
        let r = rig().await;
        r.call("mount", "j1", "trusted", json!({"account": "opal", "channel": "C0TEST"})).await.unwrap();
        r.call("send", "j1", "trusted", json!({"account": "opal", "channel": "C0TEST", "text": "announcement", "quiet": true})).await.unwrap();
        assert!(r.app.db.is_quiet("opal", "C0TEST", "9.9").await);
        let reply = Inbound { account: "opal".into(), platform: "slack".into(), channel_id: "C0TEST".into(), thread_id: "9.9".into(), message_id: "10.0".into(), to_holder: true, ..Default::default() };
        r.app.on_message(reply).await;
        assert!(r.app.seats.store.waiting("altair", 10).await.iter().all(|row| row.kind != "line"));
    }

    #[tokio::test]
    async fn the_bucket_root_is_never_deleted() {
        let r = rig().await;
        let e = r.call("bucket_rm", "op", "trusted", json!({"bucket": "b", "path": "/"})).await.unwrap_err();
        assert!(e.message.contains("never deleted"));
    }
}
