use super::*;
use serde_json::json;

fn build_set() -> OfferSet {
    let build = Step {
        op: "cargo".into(),
        props: props([("contract", "cargo-build"), ("rev", "abc123"), ("target", "x86_64-unknown-linux-musl")]),
        inputs: vec![],
        slots: vec!["bin".into()],
        spec: json!({"git": "https://example.org/x.git", "rev": "abc123"}),
        to: None,
    };
    let bin = build.slot_ids()[0].1.clone();
    let place = Step {
        op: "place".into(),
        props: PropSet::new(),
        inputs: vec![bin],
        slots: vec![],
        spec: json!({"path": "/tmp/x"}),
        to: Some("mizar".into()),
    };
    serde_json::from_value(json!({"steps": [build, place]})).unwrap()
}

#[test]
fn every_msg_round_trips() {
    let set = build_set();
    let msgs = vec![
        Msg::Offer(OfferArgs { set: set.clone(), promise: None }),
        Msg::Offer(OfferArgs { set: set.clone(), promise: Some("p1".into()) }),
        Msg::Status(PromiseRef { promise: "p1".into() }),
        Msg::Cancel(PromiseRef { promise: "p1".into() }),
        Msg::Dispose(DisposeArgs { ids: vec!["i1".into()], holder: Some("capella".into()) }),
        Msg::Review(ReviewArgs { promise: "p1".into(), scores: [("completeness".into(), 0.9)].into() }),
        Msg::Watch(WatchArgs { promises: vec![] }),
        Msg::Register(RegisterArgs { provider: "capella".into(), versions: [1, 1], props: props([("cargo", "1")]) }),
        Msg::Bid(BidArgs {
            promise: "p1".into(),
            provider: "capella".into(),
            values: Some([("time".into(), 3.0)].into()),
            hit: Some(Hit::InFlight),
            reason: None,
        }),
        Msg::Bid(BidArgs { promise: "p1".into(), provider: "mizar".into(), values: None, hit: None, reason: Some("no cargo".into()) }),
        Msg::Started(StartedArgs { promise: "p1".into(), provider: "capella".into() }),
        Msg::Settle(SettleArgs {
            promise: "p1".into(),
            provider: "capella".into(),
            outcome: Outcome::Kept { facts: vec![Fact::at("i1", "capella")], sizes: [("i1".into(), 42)].into() },
        }),
        Msg::Settle(SettleArgs { promise: "p1".into(), provider: "capella".into(), outcome: Outcome::Broken { message: "exit 1".into() } }),
        Msg::Award(AwardArgs { promise: "p1".into(), set }),
        Msg::Event(PromiseStatus { promise: "p1".into(), phase: Phase::Awarded { provider: "capella".into() }, life: None }),
        Msg::Return(json!({"promise": "p1"})),
        raise_msg(raise::NO_BIDS, "nobody bid"),
        Msg::DoesNotUnderstand(DnuArgs { selector: "frob".into() }),
    ];
    for m in msgs {
        let line = Envelope::new("a", "b", &m).encode();
        let back = Envelope::decode(&line).unwrap();
        assert_eq!(back.msg().unwrap(), m, "{line}");
    }
}

#[test]
fn refusal_table() {
    let ok = Envelope::new("caller", "market", &Msg::Status(PromiseRef { promise: "p".into() }));
    let base = serde_json::to_value(&ok).unwrap();
    let with = |f: &dyn Fn(&mut Value)| {
        let mut v = base.clone();
        f(&mut v);
        v.to_string()
    };

    // An unknown envelope key is refused at decode.
    assert!(Envelope::decode(&with(&|v| v["extra"] = json!(1))).is_err());
    // An unknown args key is refused as BadArgs.
    let env = Envelope::decode(&with(&|v| v["args"]["extra"] = json!(1))).unwrap();
    assert!(matches!(env.msg(), Err(Refusal::BadArgs(_))));
    // An unknown key deep in an offer set is refused.
    let mut offer = serde_json::to_value(Envelope::new("c", "m", &Msg::Offer(OfferArgs { set: build_set(), promise: None }))).unwrap();
    offer["args"]["set"]["steps"][0]["bogus"] = json!(true);
    assert!(matches!(Envelope::decode(&offer.to_string()).unwrap().msg(), Err(Refusal::BadArgs(_))));
    // An unknown selector is doesNotUnderstand.
    let env = Envelope::decode(&with(&|v| v["selector"] = json!("frob"))).unwrap();
    let r = env.msg().unwrap_err();
    assert_eq!(r, Refusal::DoesNotUnderstand("frob".into()));
    let reply = env.refusal_reply(&r);
    assert_eq!(reply.msg().unwrap(), Msg::DoesNotUnderstand(DnuArgs { selector: "frob".into() }));
    // A version outside the range raises UnsupportedVersion with the supported range.
    let env = Envelope::decode(&with(&|v| v["v"] = json!(99))).unwrap();
    let r = env.msg().unwrap_err();
    assert_eq!(r, Refusal::UnsupportedVersion);
    match env.refusal_reply(&r).msg().unwrap() {
        Msg::Raise(a) => {
            assert_eq!(a.name, raise::UNSUPPORTED_VERSION);
            assert_eq!(a.supported, Some(SUPPORTED));
        }
        m => panic!("{m:?}"),
    }
}

#[test]
fn reply_answers_its_ask() {
    let ask = Envelope::new("caller", "market", &Msg::Status(PromiseRef { promise: "p".into() }));
    let reply = ask.reply(&Msg::Return(json!(null)));
    assert_eq!(reply.reply_to.as_deref(), Some(ask.id.as_str()));
    assert_eq!((reply.v, reply.from.as_str(), reply.to.as_str()), (ask.v, "market", "caller"));
}

#[test]
fn offer_set_defaults() {
    let set: OfferSet = serde_json::from_value(json!({"steps": [{"op": "task"}]})).unwrap();
    assert_eq!(set.picker, "robust-z");
    assert_eq!(set.axes[0].name, TIME_AXIS);
    assert!(set.window_ms < 1000);
    assert_eq!(set.resources, Resources::default());
}

#[test]
fn identity_hashes_declared_props_only() {
    // Two providers parse the same declaration in different key order and agree.
    let a: PropSet = serde_json::from_str(r#"{"db":"uniref50","version":"2024_01"}"#).unwrap();
    let b: PropSet = serde_json::from_str(r#"{"version":"2024_01","db":"uniref50"}"#).unwrap();
    assert_eq!(instance_id(&a), instance_id(&b));
    assert_ne!(instance_id(&a), instance_id(&props([("db", "uniref50")])));

    // Slot ids differ per slot and are stable for the same request.
    let set = build_set();
    let step = &set.steps[0];
    assert_eq!(step.key(), step.clone().key());
    let mut two = step.clone();
    two.slots.push("log".into());
    let ids = two.slot_ids();
    assert_ne!(ids[0].1, ids[1].1);
    assert_eq!(slot_id(&two.key(), "bin"), ids[0].1);
    // Changing a declared param changes the key.
    let mut other = step.clone();
    other.props.insert("rev".into(), "def456".into());
    assert_ne!(other.key(), step.key());
}

#[test]
fn at_fact() {
    let f = Fact::at("i1", "capella");
    assert_eq!(f.as_at(), Some(("i1", "capella")));
}

#[test]
fn catalog_keeps_watch_off_mcp_and_http() {
    let names: Vec<_> = CALLER_VERBS.iter().map(|v| v.name).collect();
    assert_eq!(names, ["offer", "status", "cancel", "dispose", "review", "watch"]);
    assert!(CALLER_VERBS.iter().all(|v| v.cli_only == (v.name == "watch")));
    for v in CALLER_VERBS {
        serde_json::from_str::<Value>(v.schema).unwrap();
    }
}
