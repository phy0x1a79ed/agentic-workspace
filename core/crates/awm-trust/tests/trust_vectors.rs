//! trustpolicy's answers (trust_vectors.json, written by
//! awm/service_components/trustpolicy/make_trust_vectors.py) hold here too, text included.

use awm_trust::grant::load_public_key;
use awm_trust::trust::{EFFORTS, STEER_VERBS};
use awm_trust::{check_effort, journal_bound, level_for_start, may_steer, EdgeList, Level, Raise, FIRST_LOCAL_MESSAGE_HINT};
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("trust_vectors.json")).unwrap()
}

fn level(v: &Value) -> Option<Level> {
    v.as_str().map(|s| s.parse().unwrap())
}

#[test]
fn constants() {
    let v = vectors();
    let levels: Vec<&str> = Level::ALL.iter().map(|l| l.as_str()).collect();
    assert_eq!(serde_json::to_value(levels).unwrap(), v["levels"]);
    let mut steer = STEER_VERBS.to_vec();
    steer.sort();
    assert_eq!(serde_json::to_value(steer).unwrap(), v["steer_verbs"]);
    assert_eq!(FIRST_LOCAL_MESSAGE_HINT, v["first_local_message_hint"]);
}

#[test]
fn starts() {
    let v = vectors();
    let pk = load_public_key(v["public_key_b64u"].as_str().unwrap()).unwrap();
    for case in v["starts"].as_array().unwrap() {
        let raise = (!case["grant"].is_null()).then(|| Raise {
            session_id: case["session_id"].as_str().unwrap(),
            node: case["node"].as_str(),
            envelope: &case["grant"],
            key: &pk,
            now: case["now"].as_i64().unwrap(),
        });
        let got = level_for_start(level(&case["starter"]).unwrap(), level(&case["requested"]), raise);
        match (got, case.get("level")) {
            (Ok(l), Some(want)) => assert_eq!(l.as_str(), want, "{case}"),
            (Err(e), None) => assert_eq!(e.to_string(), case["error"].as_str().unwrap(), "{case}"),
            (got, _) => panic!("{case}: {got:?}"),
        }
    }
}

#[test]
fn steers() {
    for case in vectors()["steers"].as_array().unwrap() {
        let got = may_steer(level(&case["actor"]).unwrap(), case["verb"].as_str().unwrap(), level(&case["target"]));
        assert_eq!(got.is_ok(), case["ok"].as_bool().unwrap(), "{case}");
        assert_eq!(got.err().map(|e| e.to_string()).unwrap_or_default(), case["why"].as_str().unwrap(), "{case}");
    }
}

#[test]
fn bounds() {
    let v = vectors();
    let edges = EdgeList::parse(&v["edges"].to_string()).unwrap();
    for case in v["bounds"].as_array().unwrap() {
        let (project, scope) = (case["project"].as_str(), case["scope"].as_str());
        let b = journal_bound(&edges, level(&case["level"]).unwrap(), case["node"].as_str().unwrap(), project.unwrap_or(""), scope.unwrap_or(""));
        assert_eq!(serde_json::to_value(&b.read_nodes).unwrap(), case["read_nodes"], "{case}");
        assert_eq!(b.read_scope.as_deref(), case["read_scope"].as_str(), "{case}");
        assert_eq!(b.write_scope.as_deref(), case["write_scope"].as_str(), "{case}");
        for r in case["reads"].as_array().unwrap() {
            assert_eq!(b.may_read(r[0].as_str().unwrap(), r[1].as_str(), r[2].as_str()), r[3].as_bool().unwrap(), "{case} {r}");
        }
        for w in case["writes"].as_array().unwrap() {
            assert_eq!(b.may_write(w[0].as_str(), w[1].as_str()), w[2].as_bool().unwrap(), "{case} {w}");
        }
    }
}

#[test]
fn efforts() {
    for case in vectors()["efforts"].as_array().unwrap() {
        let got = check_effort(case["effort"].as_str().unwrap()).err().map(|e| e.to_string());
        assert_eq!(got.as_deref(), case["error"].as_str(), "{case}");
    }
    assert!(EFFORTS.iter().all(|e| check_effort(e).is_ok()));
}
