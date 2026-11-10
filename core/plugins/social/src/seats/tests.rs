//! Declaring seats and the queue a host pulls.

use std::sync::Arc;

use awm_trust::Level;
use serde_json::json;

use super::*;
use crate::config::{SeatChat, SeatRule};

struct T {
    _tmp: tempfile::TempDir,
    seats: Arc<Seats>,
}

fn t() -> T {
    let tmp = tempfile::tempdir().unwrap();
    let db = Db::open(&tmp.path().join("social.db")).unwrap();
    let cfg = SeatsConfig {
        paused: true,
        rules: vec![SeatRule { account: "opal".into(), kind: "dm".into(), node: "altair".into(), level: Level::Cooperative, scope: "seats/{slug}".into(), except: vec![], agent: None }],
        chats: vec![SeatChat { account: "opal".into(), channel: "C0TEST".into(), node: "mizar".into(), level: Level::Cooperative, scope: "seats/general".into(), agent: None }],
        ..SeatsConfig::default()
    };
    let accounts = vec![AccountConfig { name: "opal".into(), platform: "slack".into(), ..Default::default() }];
    T { seats: Seats::new(db, cfg, accounts), _tmp: tmp }
}

fn dm(id: &str) -> Inbound {
    Inbound { account: "opal".into(), platform: "slack".into(), channel_id: "D0TEST".into(), kind: ChatKind::Dm, sender_id: "U0".into(), sender_name: "tester".into(), message_id: id.into(), text: "hi".into(), to_holder: true, ..Default::default() }
}

#[tokio::test]
async fn a_message_waits_for_its_host_and_a_resynced_copy_is_routed_once() {
    let t = t();
    assert_eq!(t.seats.route(&dm("1.0")).await, Routed::Seat);
    let mut again = dm("1.0");
    again.resynced = true;
    assert_eq!(t.seats.route(&again).await, Routed::Duplicate);
    let reply = t.seats.pull("altair", 0, 0.0).await;
    assert_eq!(reply["rows"].as_array().unwrap().len(), 1, "one line for one message");
    let row = &reply["rows"][0];
    assert_eq!((row["kind"].as_str(), row["line"].as_str(), row["new"].as_bool()), (Some("line"), Some("[slack/DM/tester, cooperative, msg 1.0] hi"), Some(true)));
    assert_eq!((row["seat"]["node"].as_str(), row["seat"]["scope"].as_str(), row["seat"]["brief"].as_str()), (Some("altair"), Some("seats/tester"), Some("briefs/slack-slack.md")));
    assert_eq!(row["seat"]["name"], "seat:slack/dm/tester");
    assert_eq!(reply["settings"]["paused"], true);
    assert!(t.seats.pull("mizar", 0, 0.0).await["rows"].as_array().unwrap().is_empty(), "another host's row is not mine");
}

#[tokio::test]
async fn a_row_is_resent_until_its_host_acks_it() {
    let t = t();
    t.seats.route(&dm("1.0")).await;
    t.seats.route(&dm("2.0")).await;
    let first = t.seats.pull("altair", 0, 0.0).await;
    let again = t.seats.pull("altair", 0, 0.0).await;
    assert_eq!(first["rows"], again["rows"], "the answer may have been lost");
    let id = first["rows"][0]["id"].as_i64().unwrap();
    let rest = t.seats.pull("altair", id, 0.0).await;
    assert_eq!(rest["rows"].as_array().unwrap().len(), 1, "an ack clears only through its id");
    assert!(t.seats.pull("altair", rest["rows"][0]["id"].as_i64().unwrap(), 0.0).await["rows"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn a_held_pull_answers_when_a_row_arrives_and_an_empty_one_at_its_wait() {
    let t = t();
    let seats = t.seats.clone();
    let started = std::time::Instant::now();
    let held = tokio::spawn(async move { seats.pull("altair", 0, 30.0).await });
    tokio::time::sleep(Duration::from_millis(50)).await;
    t.seats.route(&dm("1.0")).await;
    assert_eq!(held.await.unwrap()["rows"].as_array().unwrap().len(), 1);
    assert!(started.elapsed() < Duration::from_secs(10), "the row woke it");
    assert!(t.seats.pull("mizar", 0, 0.05).await["rows"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn an_applied_outbox_row_is_not_applied_again_and_a_new_epoch_starts_over() {
    let t = t();
    let store = &t.seats.store;
    assert!(store.fresh("altair", "e1", 5).await);
    store.applied("altair", "e1", 5).await;
    assert!(!store.fresh("altair", "e1", 5).await, "a replay");
    assert!(!store.fresh("altair", "e1", 4).await);
    assert!(store.fresh("altair", "e1", 6).await);
    assert!(store.fresh("altair", "e2", 1).await, "a rebuilt host database");
    assert!(store.fresh("mizar", "e1", 1).await, "marks are per host");
}

#[tokio::test]
async fn a_report_fills_what_seats_shows_and_a_change_is_flagged_once() {
    let t = t();
    t.seats.route(&dm("1.0")).await;
    let store = &t.seats.store;
    let report = json!({"state": "occupied", "id": "opal/D0TEST", "queued": 2});
    assert!(store.mirror("opal", "D0TEST", "occupied", "j1", true, "u", report.clone()).await);
    assert!(!store.mirror("opal", "D0TEST", "occupied", "j1", true, "u", report.clone()).await, "nothing evented changed");
    let picked = json!({"state": "occupied", "id": "opal/D0TEST", "queued": 2, "model": "sonnet"});
    assert!(store.mirror("opal", "D0TEST", "occupied", "j1", true, "u", picked).await, "a new model is evented");
    assert!(store.mirror("opal", "D0TEST", "occupied", "j1", true, "u", report.clone()).await);
    let seat = store.get("opal", "D0TEST").await.unwrap();
    assert_eq!((seat.public(), seat.job.as_str()), (report, "j1"));
    assert!(!store.mirror("opal", "NOSEAT", "occupied", "j", false, "", json!({})).await);
}

#[tokio::test]
async fn a_forward_queues_a_line_for_the_target_host_and_grants_a_day_of_reads() {
    let t = t();
    let target = t.seats.declared("opal", "C0TEST", &Place { platform: "slack".into(), server: "Lab".into(), kind: ChatKind::Channel, chat_name: "general".into(), ..Default::default() }).await.unwrap();
    t.seats.forward(&target, ("opal", "D0TEST"), "[forwarded] look").await;
    let rows = t.seats.store.waiting("mizar", 10).await;
    assert_eq!((rows[0].kind.as_str(), rows[0].line.as_str()), ("line", "[forwarded] look"));
    let now = crate::db::now();
    assert_eq!(t.seats.store.reads(vec![("opal".into(), "C0TEST".into())], "opal", now).await, ["D0TEST"]);
    assert!(t.seats.store.reads(vec![("opal".into(), "C0TEST".into())], "opal", now + READ_S + 1.0).await.is_empty());
}

#[tokio::test]
async fn sync_drops_a_seat_the_config_no_longer_declares_and_redeclares_the_rest_by_name() {
    let t = t();
    t.seats.route(&dm("1.0")).await;
    t.seats.declared("opal", "C0TEST", &Place { platform: "slack".into(), server: "Hallam Hive".into(), kind: ChatKind::Channel, chat_name: "general".into(), ..Default::default() }).await;
    let mut cfg = t.seats.cfg.clone();
    cfg.rules[0].except = vec!["D0TEST".into()];
    let seats = Seats::new(t.seats.store.0.clone(), cfg, vec![AccountConfig { name: "opal".into(), platform: "slack".into(), ..Default::default() }]);
    seats.sync().await;
    assert!(seats.store.get("opal", "D0TEST").await.is_none());
    let altair = seats.pull("altair", 0, 0.0).await;
    let roster = altair["rows"].as_array().unwrap().last().unwrap().clone();
    assert_eq!((roster["kind"].as_str(), roster["seats"].clone()), (Some("roster"), json!([])), "altair hosts no seat now");
    let mizar = seats.pull("mizar", 0, 0.0).await;
    assert_eq!((mizar["rows"][0]["kind"].as_str(), mizar["rows"][0]["seat"]["name"].as_str()), (Some("decl"), Some("seat:slack/hallam-hive/general")));
}

#[tokio::test]
async fn a_message_from_an_excepted_sender_drops_its_stored_seat() {
    let t = t();
    t.seats.route(&dm("1.0")).await;
    let mut cfg = t.seats.cfg.clone();
    cfg.rules[0].except = vec!["U0".into()];
    let seats = Seats::new(t.seats.store.0.clone(), cfg, vec![]);
    assert_eq!(seats.route(&dm("2.0")).await, Routed::NotSeat);
    assert!(seats.store.get("opal", "D0TEST").await.is_none());
    assert_eq!(seats.pull("altair", 0, 0.0).await["rows"].as_array().unwrap().last().unwrap()["kind"], "roster");
}
