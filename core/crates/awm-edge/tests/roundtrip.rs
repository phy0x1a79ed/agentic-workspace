use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use awm_edge::{caller_node, serve, EdgeClient, EdgeError};
use awm_gateway::peer::WsPipe;
use awm_proto::{Caller, Lane, WsMsg};
use serde_json::json;

fn duplex_client(opens: Arc<AtomicUsize>, drop_after: Option<usize>) -> EdgeClient {
    EdgeClient::with_opener(Arc::new(move |peer: &str| {
        opens.fetch_add(1, Ordering::SeqCst);
        let (client_end, server_end) = tokio::io::duplex(1 << 16);
        let (cr, cw) = tokio::io::split(client_end);
        let (sr, sw) = tokio::io::split(server_end);
        let served = Arc::new(AtomicUsize::new(0));
        let peer = peer.to_string();
        tokio::spawn(serve(
            sr,
            sw,
            "home".into(),
            move |name, args, caller: Caller| {
                let n = served.fetch_add(1, Ordering::SeqCst);
                let peer = peer.clone();
                async move {
                    if drop_after == Some(n) {
                        return Err("drop".into());
                    }
                    assert_eq!(caller.lane, Lane::Edge);
                    assert_eq!(caller.node, "home");
                    Ok(json!({"peer": peer, "tool": name, "args": args, "trust": caller.trust}))
                }
            },
            // An echo stream: it greets with the path and the caller's node, then answers
            // each message with itself and closes on "bye".
            |path: String, caller: Caller| async move {
                if path.contains("refused") {
                    return Err("no such stream".to_string());
                }
                let (mine, theirs) = WsPipe::pair();
                let WsPipe { tx, mut rx } = theirs;
                tx.send(WsMsg::Text(format!("{path} for {}", caller.node))).unwrap();
                tokio::spawn(async move {
                    while let Some(m) = rx.recv().await {
                        if m == WsMsg::Text("bye".into()) {
                            let _ = tx.send(WsMsg::Close(Some("done".into())));
                            break;
                        }
                        let _ = tx.send(m);
                    }
                });
                Ok(mine)
            },
        ));
        Ok((Box::new(cr) as _, Box::new(cw) as _, None))
    }))
}

#[tokio::test]
async fn calls_overlap_on_one_warm_channel() {
    let opens = Arc::new(AtomicUsize::new(0));
    let c = Arc::new(duplex_client(opens.clone(), None));
    let caller = Caller { node: "spoofed".into(), lane: Lane::Local, session: Some("s1".into()), trust: Some("cooperative".into()),
                         scope: Some("awm/x".into()), origin: None, ..Caller::default() };
    let mut tasks = Vec::new();
    for i in 0..20 {
        let (c, caller) = (c.clone(), caller.clone());
        tasks.push(tokio::spawn(async move { c.invoke("work", "journal", json!({"i": i}), caller).await.unwrap() }));
    }
    for (i, t) in tasks.into_iter().enumerate() {
        let v = t.await.unwrap();
        assert_eq!(v["args"]["i"], i);
        assert_eq!(v["trust"], "cooperative");
    }
    assert_eq!(opens.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn a_stream_carries_text_and_bytes_both_ways_and_ends() {
    let c = duplex_client(Arc::new(AtomicUsize::new(0)), None);
    let mut pipe = c.open_ws("work", "/hub/session/ssh/lease", Caller::default()).await.unwrap();
    assert_eq!(pipe.rx.recv().await, Some(WsMsg::Text("/hub/session/ssh/lease for home".into())));
    pipe.tx.send(WsMsg::Binary(vec![1, 2, 255])).unwrap();
    assert_eq!(pipe.rx.recv().await, Some(WsMsg::Binary(vec![1, 2, 255])));
    // A call still runs beside the open stream.
    assert_eq!(c.invoke("work", "journal", json!({}), Caller::default()).await.unwrap()["tool"], "journal");
    pipe.tx.send(WsMsg::Text("bye".into())).unwrap();
    assert_eq!(pipe.rx.recv().await, Some(WsMsg::Close(Some("done".into()))));

    let mut refused = c.open_ws("work", "/hub/emit/refused/x", Caller::default()).await.unwrap();
    assert_eq!(refused.rx.recv().await, Some(WsMsg::Close(Some("no such stream".into()))));
}

#[tokio::test]
async fn a_remote_error_is_an_error() {
    let c = duplex_client(Arc::new(AtomicUsize::new(0)), Some(0));
    let err = c.invoke("work", "x", json!({}), Caller::default()).await.unwrap_err();
    assert!(matches!(err, EdgeError::Remote(ref m) if m == "drop"));
}

#[tokio::test]
async fn an_unreachable_peer_fails_cleanly_and_the_next_call_retries() {
    let opens = Arc::new(AtomicUsize::new(0));
    let o = opens.clone();
    let c = EdgeClient::with_opener(Arc::new(move |_peer: &str| {
        o.fetch_add(1, Ordering::SeqCst);
        // A link that closes at once, as an ssh to a sleeping node does.
        let (client_end, server_end) = tokio::io::duplex(64);
        drop(server_end);
        let (r, w) = tokio::io::split(client_end);
        Ok((Box::new(r) as _, Box::new(w) as _, None))
    }));
    for _ in 0..2 {
        let err = c.invoke("asleep", "x", json!({}), Caller::default()).await.unwrap_err();
        assert!(matches!(err, EdgeError::Unreachable { .. }), "{err}");
    }
    assert_eq!(opens.load(Ordering::SeqCst), 2);
}

#[test]
fn the_key_names_the_calling_node() {
    let auth = "publickey ssh-ed25519 AAAAmizar\n";
    let keys = "ssh-ed25519 AAAAlyra awm:lyra:user\nrestrict,command=\"awm port\" ssh-ed25519 AAAAx awm:work:service\nssh-ed25519 AAAAmizar awm:mizar:user\n";
    assert_eq!(caller_node(auth, keys).as_deref(), Some("mizar"));
    assert_eq!(caller_node("publickey ssh-ed25519 AAAAx\n", keys), None);
    assert_eq!(caller_node("publickey ssh-ed25519 AAAAzzz\n", keys), None);
}

#[tokio::test]
async fn edge_peers_follow_the_edge_list() {
    use awm_edge::EdgePeers;
    use awm_gateway::peer::{PeerError, PeerTransport};
    use awm_proto::{CallContext, Surface};
    let edges = awm_trust::edges::EdgeList::parse(
        "nodes:\n  home: {role: desktop, managed: true}\n  work: {role: work, managed: true}\n\
         edges:\n  home: {work: lan}\n",
    )
    .unwrap();
    let peers = EdgePeers::new("home", edges, duplex_client(Arc::new(AtomicUsize::new(0)), None));
    assert_eq!(peers.peers(), vec!["work".to_string()]);
    let mut ctx = CallContext::local("home", Surface::Cli);
    ctx.caller.trust = Some("trusted".into());
    let out = peers.invoke("work", "journal", json!({"verb": "fetch"}), &ctx).await.unwrap();
    let v: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert_eq!(v["trust"], "trusted");
    assert!(matches!(peers.invoke("relay", "journal", json!({}), &ctx).await, Err(PeerError::Unknown(_))));
}

#[tokio::test]
async fn a_retiring_node_is_never_dialed() {
    use awm_edge::EdgePeers;
    use awm_gateway::peer::{PeerError, PeerTransport};
    use awm_proto::{CallContext, Surface};
    let edges = awm_trust::edges::EdgeList::parse(
        "nodes:\n  home: {role: desktop, managed: true}\n  work: {role: work}\n  old: {role: retiring}\n\
         edges:\n  home: {work: lan, old: lan}\n",
    )
    .unwrap();
    let opens = Arc::new(AtomicUsize::new(0));
    let peers = EdgePeers::new("home", edges, duplex_client(opens.clone(), None));
    assert_eq!(peers.peers(), vec!["work".to_string()]);
    let ctx = CallContext::local("home", Surface::Cli);
    fn refused<T>(r: Result<T, PeerError>) {
        match r {
            Err(PeerError::Refused { body, .. }) => assert!(body.contains("retiring"), "{body}"),
            Err(e) => panic!("not refused as retiring: {e}"),
            Ok(_) => panic!("a retiring node answered"),
        }
    }
    refused(peers.invoke("old", "journal", json!({"verb": "fetch"}), &ctx).await);
    refused(peers.tools("old").await);
    refused(peers.table_changed("old", "home").await);
    assert!(peers.open_ws("old", "/hub/emit/x/y", &ctx).await.is_err());
    assert_eq!(opens.load(Ordering::SeqCst), 0, "the opener was called for a retiring node");
    peers.invoke("work", "journal", json!({}), &ctx).await.unwrap();
    assert_eq!(opens.load(Ordering::SeqCst), 1);
}

