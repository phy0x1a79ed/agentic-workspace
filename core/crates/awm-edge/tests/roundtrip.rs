use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use awm_edge::{caller_node, serve, EdgeClient, EdgeError};
use awm_proto::{Caller, Lane};
use serde_json::json;

fn duplex_client(opens: Arc<AtomicUsize>, drop_after: Option<usize>) -> EdgeClient {
    EdgeClient::with_opener(Arc::new(move |peer: &str| {
        opens.fetch_add(1, Ordering::SeqCst);
        let (client_end, server_end) = tokio::io::duplex(1 << 16);
        let (cr, cw) = tokio::io::split(client_end);
        let (sr, sw) = tokio::io::split(server_end);
        let served = Arc::new(AtomicUsize::new(0));
        let peer = peer.to_string();
        tokio::spawn(serve(sr, sw, "home".into(), move |name, args, caller: Caller| {
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
        }));
        Ok((Box::new(cr) as _, Box::new(cw) as _, None))
    }))
}

#[tokio::test]
async fn calls_overlap_on_one_warm_channel() {
    let opens = Arc::new(AtomicUsize::new(0));
    let c = Arc::new(duplex_client(opens.clone(), None));
    let caller = Caller { node: "spoofed".into(), lane: Lane::Local, session: Some("s1".into()), trust: Some("cooperative".into()) };
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
