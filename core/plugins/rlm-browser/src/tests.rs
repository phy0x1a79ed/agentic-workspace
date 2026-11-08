//! rlm-browser against a fake Chrome on a real loopback socket: modes, teardown, the guest
//! window, and the manifest.

use super::*;
use crate::engine::{Config, Manager};
use axum::extract::ws::{Message, WebSocketUpgrade};
use axum::extract::{Path as AxPath, State};
use axum::routing::get;
use axum::{Json, Router};
use std::sync::Mutex as SyncMutex;

#[derive(Default)]
struct Chrome {
    targets: Vec<(String, Option<String>)>,
    contexts: Vec<String>,
    calls: Vec<String>,
    n: u64,
}

type Shared = Arc<SyncMutex<Chrome>>;

fn browser_answer(st: &Shared, m: &Value) -> Value {
    let mut c = st.lock().unwrap();
    let method = m["method"].as_str().unwrap_or("").to_string();
    c.calls.push(method.clone());
    c.n += 1;
    let n = c.n;
    match method.as_str() {
        "Target.createBrowserContext" => {
            c.contexts.push(format!("ctx{n}"));
            json!({"browserContextId": format!("ctx{n}")})
        }
        "Target.createTarget" => {
            let ctx = m["params"]["browserContextId"].as_str().map(str::to_string);
            c.targets.push((format!("T{n}"), ctx));
            json!({"targetId": format!("T{n}")})
        }
        "Target.getTargets" => {
            json!({"targetInfos": c.targets.iter().map(|(t, ctx)| json!({"targetId": t, "type": "page", "url": "about:blank", "title": "", "attached": false, "browserContextId": ctx.clone().unwrap_or("default".into())})).collect::<Vec<_>>()})
        }
        "Target.closeTarget" => {
            let t = m["params"]["targetId"].as_str().unwrap_or("").to_string();
            c.targets.retain(|(x, _)| *x != t);
            json!({"success": true})
        }
        "Target.disposeBrowserContext" => {
            let g = m["params"]["browserContextId"].as_str().map(str::to_string);
            c.targets.retain(|(_, ctx)| *ctx != g);
            json!({})
        }
        _ => json!({}),
    }
}

async fn fake_chrome(tony_tabs: usize) -> (String, Shared) {
    let st: Shared = Arc::default();
    for i in 0..tony_tabs {
        st.lock().unwrap().targets.push((format!("tony{i}"), None));
    }
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = Router::new()
        .route("/json/version", get(|| async { Json(json!({"webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/b"})) }))
        .route("/json/protocol", get(|| async { Json(json!({"domains": [{"domain": "Page"}]})) }))
        .route(
            "/devtools/browser/b",
            get(|ws: WebSocketUpgrade, State(st): State<Shared>| async move {
                ws.on_upgrade(move |mut sock| async move {
                    while let Some(Ok(Message::Text(t))) = sock.recv().await {
                        let m: Value = serde_json::from_str(&t).unwrap();
                        let r = browser_answer(&st, &m);
                        let _ = sock.send(Message::Text(json!({"id": m["id"], "result": r}).to_string().into())).await;
                    }
                })
            }),
        )
        .route(
            "/devtools/page/{tab}",
            get(|ws: WebSocketUpgrade, AxPath(tab): AxPath<String>, State(st): State<Shared>| async move {
                ws.on_upgrade(move |mut sock| async move {
                    while let Some(Ok(Message::Text(t))) = sock.recv().await {
                        let m: Value = serde_json::from_str(&t).unwrap();
                        st.lock().unwrap().calls.push(format!("{tab}:{}", m["method"].as_str().unwrap_or("")));
                        let r = json!({"result": {"value": "v"}, "data": "png"});
                        let _ = sock.send(Message::Text(json!({"id": m["id"], "result": r}).to_string().into())).await;
                    }
                })
            }),
        )
        .with_state(st.clone());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    (format!("http://{addr}"), st)
}

fn manager(dir: &std::path::Path, attach: &str) -> Manager {
    let cfg = Config { backend: "host".into(), attach_url: attach.into(), image: String::new(), shm: String::new(), profiles: dir.join("profiles") };
    Manager::new(cfg, store::Store::open(&dir.join("rlm-browser.db")).unwrap(), None)
}

fn calls(st: &Shared) -> Vec<String> {
    st.lock().unwrap().calls.clone()
}

#[tokio::test]
async fn a_guest_gets_its_own_window_in_the_attached_chrome() {
    let tmp = tempfile::tempdir().unwrap();
    let (base, st) = fake_chrome(2).await;
    let m = manager(tmp.path(), &base);
    let s = m.acquire("g", Some("attached"), &json!({}), Level::Cooperative).await.unwrap();
    let id = s["session_id"].as_str().unwrap().to_string();
    let tabs = m.tab_list(&id).await.unwrap()["tabs"].as_array().unwrap().clone();
    assert_eq!(tabs.len(), 1, "a guest sees only its own blank tab");
    for refused in [m.tab_close(&id, "tony0").await, m.tab_activate(&id, "tony0").await, m.observe(&id, Some("tony0")).await] {
        assert!(matches!(refused, Err(EngineError::Value(_))));
    }
    for (method, tab) in [("Runtime.evaluate", Some(engine::BROWSER_TAB)), ("Target.getTargets", None), ("Storage.getCookies", None), ("Network.getAllCookies", None)] {
        assert!(matches!(m.cdp(&id, method, tab, Value::Null).await, Err(EngineError::Value(_))), "{method}");
    }
    let own = tabs[0]["tab_id"].as_str().unwrap();
    m.cdp(&id, "Runtime.evaluate", Some(own), json!({"expression": "1"})).await.unwrap();
    assert!(calls(&st).contains(&format!("{own}:Runtime.evaluate")));
    m.reset(&id).await.unwrap();
    assert_eq!(st.lock().unwrap().targets.iter().filter(|(t, _)| t.starts_with("tony")).count(), 2, "a guest's reset keeps Tony's tabs");
    m.release(&id).await.unwrap();
    assert!(calls(&st).contains(&"Target.disposeBrowserContext".to_string()));
    assert!(!calls(&st).contains(&"Browser.close".to_string()));
}

fn env() -> awm_plugin_sdk::Env {
    awm_plugin_sdk::Env { hub_url: "http://127.0.0.1:1".into(), service: TOOL.into(), plugin: TOOL.into(), node: "mizar".into(), awm_dir: "/nonexistent".into(), token_file: None }
}

#[tokio::test]
async fn the_ready_api_matches_the_golden_manifest_and_levels_only_lower() {
    let tmp = tempfile::tempdir().unwrap();
    let golden: Value = serde_json::from_str(include_str!("../tests/golden/rlm-browser.api.json")).unwrap();
    let m = Arc::new(manager(tmp.path(), "http://127.0.0.1:1"));
    assert_eq!(service(Service::new(env()), m.clone()).manifest(), golden);

    let (base, st) = fake_chrome(1).await;
    let svc = service(Service::new(env()), Arc::new(manager(tmp.path(), &base))).into_test().await.unwrap();
    let caller = awm_plugin_sdk::Caller { node: "mizar".into(), ..Default::default() };
    let args = json!({"game": "g", "mode": "attached", "opts": {"attach_url": base, "level": "cooperative"}, "_caller_trust": "trusted"});
    svc.call_as("acquire", args, caller, None).await.unwrap();
    assert!(calls(&st).contains(&"Target.createBrowserContext".to_string()), "a forwarded cooperative caller gets a guest window");
}

#[tokio::test]
async fn releasing_an_owned_chrome_after_a_restart_closes_it_through_cdp() {
    let tmp = tempfile::tempdir().unwrap();
    let (base, st) = fake_chrome(0).await;
    let port: u16 = base.rsplit(':').next().unwrap().parse().unwrap();
    let m = manager(tmp.path(), "http://127.0.0.1:1");
    let row = store::Store::open(&tmp.path().join("rlm-browser.db")).unwrap().create("g", "headless", "host", port, "", "", 0, None, "").await.unwrap();
    m.release(row["session_id"].as_str().unwrap()).await.unwrap();
    assert!(calls(&st).contains(&"Browser.close".to_string()));
}

/// A real headless Chrome, when this machine has one.
#[tokio::test]
async fn a_real_headless_chrome_serves_the_verbs() {
    if awm_cdp::find_chrome().is_none() {
        eprintln!("no Chrome on PATH: skipped");
        return;
    }
    let tmp = tempfile::tempdir().unwrap();
    let m = manager(tmp.path(), "http://127.0.0.1:1");
    let s = m.acquire("probe", None, &json!({}), Level::Untrusted).await.unwrap();
    let id = s["session_id"].as_str().unwrap();
    let tab = m.tab_open(id, Some("data:text/html,<title>hi</title>")).await.unwrap();
    let mut seen = Value::Null;
    for _ in 0..100 {
        seen = m.observe(id, tab["tab_id"].as_str()).await.unwrap();
        if seen["title"] == "hi" {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
    assert_eq!(seen["title"], "hi");
    assert!(m.screenshot(id, None, false).await.unwrap()["image"].is_string());
    assert!(m.commands(Some(id)).await.unwrap()["protocol"]["domains"].is_array());
    m.release(id).await.unwrap();
}
