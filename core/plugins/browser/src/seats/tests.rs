//! Browser seats against a fake Chrome: confinement, trust, the viewer, and a restart.

use super::*;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex as SyncMutex;

/// Windows hold targets. A new tab lands in the focused window, as Chrome does.
pub struct FakeChrome {
    name: String,
    ids: AtomicU64,
    pub generation: AtomicU64,
    inner: SyncMutex<Inner>,
}

#[derive(Default)]
pub struct Inner {
    pub windows: HashMap<String, i64>,
    pub urls: HashMap<String, String>,
    pub contexts: Vec<String>,
    pub focused: Option<i64>,
    pub calls: Vec<(String, Value, Option<String>)>,
}

impl FakeChrome {
    pub fn new(name: &str) -> Self {
        FakeChrome { name: name.into(), ids: AtomicU64::new(1), generation: AtomicU64::new(0), inner: SyncMutex::default() }
    }

    fn next(&self) -> u64 {
        self.ids.fetch_add(1, Ordering::SeqCst)
    }

    pub fn with<T>(&self, f: impl FnOnce(&mut Inner) -> T) -> T {
        f(&mut self.inner.lock().unwrap())
    }

    fn answer(&self, method: &str, params: &Value) -> Value {
        let mut st = self.inner.lock().unwrap();
        match method {
            "Target.createBrowserContext" => {
                let ctx = format!("ctx{}", self.next());
                st.contexts.push(ctx.clone());
                json!({"browserContextId": ctx})
            }
            "Target.createTarget" => {
                let t = format!("{}-t{}", self.name, self.next());
                if params.get("newWindow") == Some(&json!(true)) || st.focused.is_none() {
                    st.focused = Some(1000 + self.next() as i64);
                }
                let w = st.focused.unwrap();
                st.windows.insert(t.clone(), w);
                st.urls.insert(t.clone(), params["url"].as_str().unwrap_or("about:blank").into());
                json!({"targetId": t})
            }
            "Target.activateTarget" => {
                st.focused = st.windows.get(params["targetId"].as_str().unwrap()).copied();
                json!({})
            }
            "Browser.getWindowForTarget" => json!({"windowId": st.windows[params["targetId"].as_str().unwrap()]}),
            "Target.attachToTarget" => json!({"sessionId": format!("s{}-{}", self.generation.load(Ordering::SeqCst), params["targetId"].as_str().unwrap())}),
            "Target.closeTarget" => {
                let t = params["targetId"].as_str().unwrap();
                st.windows.remove(t);
                st.urls.remove(t);
                json!({"success": true})
            }
            "Target.getTargets" => {
                let mut infos: Vec<Value> = st.windows.keys().map(|t| json!({"targetId": t, "type": "page", "url": st.urls[t], "title": ""})).collect();
                infos.sort_by_key(|v| v["targetId"].as_str().unwrap().to_string());
                json!({"targetInfos": infos})
            }
            other => json!({"ok": other}),
        }
    }

    pub fn calls(&self) -> Vec<(String, Value, Option<String>)> {
        self.with(|st| st.calls.clone())
    }
}

impl Cdp for FakeChrome {
    fn send<'a>(&'a self, method: &'a str, params: Value, session: Option<&'a str>) -> CdpFuture<'a> {
        Box::pin(async move {
            let out = self.answer(method, &params);
            self.with(|st| st.calls.push((method.to_string(), params, session.map(str::to_string))));
            Ok(out)
        })
    }
    fn generation(&self) -> u64 {
        self.generation.load(Ordering::SeqCst)
    }
}

fn book() -> (Arc<FakeChrome>, SeatBook) {
    let chrome = Arc::new(FakeChrome::new("shared"));
    (chrome.clone(), SeatBook::new(chrome, None, None))
}

fn seat_msg<T: std::fmt::Debug>(r: Result<T, BookError>) -> String {
    match r {
        Err(BookError::Seat(m)) => m,
        other => panic!("expected a seat refusal, got {other:?}"),
    }
}

impl std::fmt::Debug for Seat {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Seat({})", self.seat)
    }
}

#[tokio::test]
async fn a_trusted_seat_gets_tonys_profile_and_a_cooperative_one_a_blank_context() {
    let (chrome, book) = book();
    let t = book.open("job-t", "trusted", None, false).await.unwrap();
    let c = book.open("job-c", "cooperative", None, false).await.unwrap();
    assert!(t.profile == "tony" && t.context.is_none());
    assert!(c.profile == "blank" && chrome.with(|s| s.contexts.contains(c.context.as_ref().unwrap())));
    assert_ne!(t.window, c.window);
}

#[tokio::test]
async fn a_seat_reaches_only_its_own_tabs() {
    let (_, book) = book();
    let a = book.open("job-a", "trusted", None, false).await.unwrap();
    let b = book.open("job-b", "trusted", None, false).await.unwrap();
    let tb = &b.tabs[0];
    seat_msg(book.tab_close(&a.seat, "job-a", tb).await);
    seat_msg(book.cdp(&a.seat, "job-a", tb, "Runtime.evaluate", json!({"expression": "1"})).await);
    seat_msg(book.tab_list(&b.seat, "job-a").await);
}

#[tokio::test]
async fn no_seat_reaches_the_raw_debug_port() {
    let (_, book) = book();
    let a = book.open("job-a", "trusted", None, false).await.unwrap();
    for m in ["Target.createTarget", "Browser.close", "Storage.getCookies", "Network.getAllCookies", "SystemInfo.getInfo"] {
        assert!(seat_msg(book.cdp(&a.seat, "job-a", &a.tabs[0], m, Value::Null).await).contains("not open"));
    }
}

struct FakeBoxes {
    launched: SyncMutex<Vec<String>>,
    forgotten: SyncMutex<Vec<String>>,
}

impl Launcher for Arc<FakeBoxes> {
    fn launch(&self, seat_id: &str, profile: &str) -> LaunchFuture {
        self.launched.lock().unwrap().push(profile.into());
        let name = format!("box-{seat_id}");
        Box::pin(async move { Ok((name, 4000)) })
    }
    fn stop(&self, _name: &str) -> Pin<Box<dyn Future<Output = ()> + Send>> {
        Box::pin(async {})
    }
    fn connect(&self, _port: u16) -> Arc<dyn Cdp> {
        Arc::new(FakeChrome::new("box"))
    }
    fn forget(&self, seat_id: &str) {
        self.forgotten.lock().unwrap().push(seat_id.into());
    }
}

#[tokio::test]
async fn own_container_carries_the_profile_the_level_allows_and_close_deletes_its_copy() {
    let boxes = Arc::new(FakeBoxes { launched: SyncMutex::default(), forgotten: SyncMutex::default() });
    let book = SeatBook::new(Arc::new(FakeChrome::new("shared")), Some(Arc::new(boxes.clone())), None);
    let t = book.open("job-t", "trusted", None, true).await.unwrap();
    book.open("job-t2", "trusted", Some("blank"), true).await.unwrap();
    book.open("job-c", "cooperative", None, true).await.unwrap();
    assert_eq!(*boxes.launched.lock().unwrap(), vec!["tony", "blank", "blank"]);
    book.close(&t.seat, "job-t").await.unwrap();
    assert_eq!(*boxes.forgotten.lock().unwrap(), vec![t.seat.clone()]);
}

#[tokio::test]
async fn a_restart_adopts_the_service_tabs_already_open() {
    let chrome = Arc::new(FakeChrome::new("shared"));
    let first = SeatBook::new(chrome.clone(), None, None);
    let teams = first.service_tab("teams", "https://teams.microsoft.com/v2/").await.unwrap();
    let again = SeatBook::new(chrome.clone(), None, None);
    assert_eq!(again.service_tab("teams", "https://teams.microsoft.com").await.unwrap(), teams);
    assert_eq!(chrome.calls().iter().filter(|c| c.0 == "Target.createTarget").count(), 1);
}

#[tokio::test]
async fn seats_survive_a_restart_and_a_chrome_reconnect() {
    let tmp = tempfile::tempdir().unwrap();
    let store = tmp.path().join("seats.json");
    let chrome = Arc::new(FakeChrome::new("shared"));
    let first = SeatBook::new(chrome.clone(), None, Some(store.clone()));
    let a = first.open("mizar/job-a", "trusted", None, false).await.unwrap();
    let gone = first.open("mizar/job-b", "trusted", None, false).await.unwrap();
    assert_eq!(std::fs::metadata(&store).unwrap().permissions().mode() & 0o777, 0o600);
    drop(first);

    // Chrome restarted: the connection is new and one seat's window went with it.
    chrome.generation.store(1, Ordering::SeqCst);
    chrome.with(|s| {
        s.windows.remove(&gone.tabs[0]);
    });
    let book = SeatBook::new(chrome.clone(), None, Some(store.clone()));
    assert_eq!(book.load().await.unwrap(), 2);
    let closes_before = chrome.calls().iter().filter(|c| c.0 == "Target.closeTarget").count();
    book.verify().await.unwrap();
    assert_eq!(chrome.calls().iter().filter(|c| c.0 == "Target.closeTarget").count(), closes_before, "a restart closes no window");
    let seats = book.list(None).await;
    assert_eq!(seats.len(), 1);
    book.navigate(&a.seat, "mizar/job-a", &a.tabs[0], "https://example.org").await.unwrap();
    assert_eq!(chrome.calls().last().unwrap().2, Some(format!("s1-{}", a.tabs[0])), "the tab is attached again on the new connection");
}

use std::os::unix::fs::PermissionsExt;
