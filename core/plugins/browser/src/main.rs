//! The `browser` singleton's verbs (fleet.md R102).

mod containers;
mod seats;

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use awm_plugin_sdk::{Call, Service, VerbError};
use serde_json::{json, Value};

use crate::containers::Containers;
use crate::seats::{BookError, SeatBook};

const TOOL: &str = "browser";
const DESCRIPTION: &str = "alcor's browser (fleet.md R102): each session drives its own Chrome window, its seat. One viewer shows them all.";
/// How long an approval request for a sign-in stays open, the shortest grant there is.
const SIGNIN_MINUTES: u64 = 15;

/// `<AWM_DIR>/browser.json`, as the cloud role writes it.
#[derive(Debug, Clone)]
struct Settings {
    dir: PathBuf,
    cdp: String,
    viewer: String,
    service_sites: Vec<(String, String)>,
    seat_image: String,
    tony_profile: Option<PathBuf>,
}

impl Settings {
    fn load(dir: &Path) -> anyhow::Result<Self> {
        let file = dir.join("browser.json");
        let cfg: Value = match std::fs::read(&file) {
            Ok(b) => serde_json::from_slice(&b)?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => json!({}),
            Err(e) => return Err(e.into()),
        };
        let s = |k: &str, d: &str| cfg.get(k).and_then(Value::as_str).unwrap_or(d).to_string();
        let sites = cfg.get("service_sites").and_then(Value::as_object).map(|m| m.iter().filter_map(|(k, v)| v.as_str().map(|u| (k.clone(), u.to_string()))).collect()).unwrap_or_default();
        Ok(Settings {
            dir: dir.to_path_buf(),
            cdp: s("cdp", "http://127.0.0.1:9222"),
            viewer: s("viewer", ""),
            service_sites: sites,
            seat_image: s("seat_image", "awm-browser-seat:latest"),
            tony_profile: cfg.get("tony_profile").and_then(Value::as_str).filter(|p| !p.is_empty()).map(PathBuf::from),
        })
    }
}

fn p(name: &str, ty: &str, required: bool, desc: &str) -> Value {
    json!({"name": name, "type": ty, "required": required, "description": desc})
}

/// The calling session and its level. A seat is keyed on the session and the node it runs on.
/// A caller that is no session, such as a shell on this node, gets no key and so no seat.
fn caller(call: &Call) -> (String, String) {
    let c = &call.caller;
    let claim = call.caller_claim();
    let from_claim = |k: &str| claim.and_then(|v| v.get(k)).and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_string);
    let session = c.session.clone().filter(|s| !s.is_empty()).or_else(|| c.job.clone().filter(|s| !s.is_empty())).or_else(|| from_claim("session_id")).or_else(|| from_claim("job")).or_else(|| from_claim("name"));
    let key = session.map(|s| format!("{}/{s}", c.node)).unwrap_or_default();
    (key, call.caller_trust().unwrap_or("untrusted").to_string())
}

fn answer(r: Result<Value, BookError>) -> Result<Value, VerbError> {
    match r {
        Ok(v) => Ok(v),
        Err(BookError::Seat(msg)) => Ok(json!({"error": msg})),
        Err(BookError::Usage(msg)) => Err(VerbError::value(msg)),
        Err(BookError::Cdp(e)) => Err(VerbError::runtime(e.to_string())),
        Err(BookError::Other(e)) => Err(VerbError::from_anyhow(e)),
    }
}

fn req(call: &Call, name: &str) -> Result<String, VerbError> {
    call.req::<String>(name)
}

type Book = Arc<SeatBook>;

macro_rules! verb {
    ($svc:expr, $book:expr, $name:literal, $desc:expr, $params:expr, |$b:ident, $call:ident, $who:ident| $body:expr) => {
        verb!($svc, $book, [], $name, $desc, $params, |$b, $call, $who| $body)
    };
    ($svc:expr, $book:expr, [$($cap:ident),*], $name:literal, $desc:expr, $params:expr, |$b:ident, $call:ident, $who:ident| $body:expr) => {{
        let book = $book.clone();
        $svc.verb(TOOL, $name, $desc, $params, move |$call: Call| {
            let $b = book.clone();
            $(let $cap = $cap.clone();)*
            async move {
                let $who = caller(&$call);
                let out: Result<Value, BookError> = async { $body }.await;
                answer(out)
            }
        })
    }};
}

fn service(svc: Service, book: Book, settings: Arc<Settings>) -> Service {
    let seat = || p("seat", "string", true, "The seat id that seat_open returned.");
    let tab = || p("tab", "string", true, "A tab id of this seat's window.");
    let url = || p("url", "string", true, "");
    let viewer = settings.viewer.clone();
    let node = svc.context().env().node.clone();
    let svc = svc.api("description", json!(DESCRIPTION)).api("emitters", json!([])).api("sessions", json!([]));
    let svc = verb!(svc, book, "seat_open", "Attach this session to its own browser window. A trusted session gets Tony's profile unless it asks for a blank one. A cooperative session always gets a blank one. Returns the seat id.",
        json!([p("profile", "string", false, "tony or blank."), p("own_container", "boolean", false, "Run this seat in its own container.")]),
        |b, call, who| {
            let own = call.args.get("own_container").map(truthy).unwrap_or(false);
            b.open(&who.0, &who.1, call.str_arg("profile"), own).await.map(|s| s.view())
        });
    let svc = verb!(svc, book, "seat_close", "Close this seat's window, tabs and container.", json!([seat()]), |b, call, who| {
        let id = req(&call, "seat").map_err(|e| BookError::Usage(e.to_string()))?;
        b.close(&id, &who.0).await.map(|_| json!({"closed": id}))
    });
    let svc = verb!(svc, book, "seats", "This session's seats.", json!([]), |b, _call, who| Ok(Value::Array(b.list(Some(&who.0)).await)));
    let svc = verb!(svc, book, "tab_open", "Open a tab in this seat's window.", json!([seat(), url()]), |b, call, who| {
        let (id, u) = (usage(req(&call, "seat"))?, usage(req(&call, "url"))?);
        b.tab_open(&id, &who.0, &u).await.map(|t| json!({"tab": t}))
    });
    let svc = verb!(svc, book, "tab_close", "Close a tab of this seat.", json!([seat(), tab()]), |b, call, who| {
        let (id, t) = (usage(req(&call, "seat"))?, usage(req(&call, "tab"))?);
        b.tab_close(&id, &who.0, &t).await.map(|_| json!({"closed": t}))
    });
    let svc = verb!(svc, book, "tab_activate", "Switch to a tab of this seat.", json!([seat(), tab()]), |b, call, who| {
        let (id, t) = (usage(req(&call, "seat"))?, usage(req(&call, "tab"))?);
        b.tab_activate(&id, &who.0, &t).await.map(|_| json!({"active": t}))
    });
    let svc = verb!(svc, book, "tab_list", "This seat's tabs.", json!([seat()]), |b, call, who| {
        let id = usage(req(&call, "seat"))?;
        b.tab_list(&id, &who.0).await
    });
    let svc = verb!(svc, book, "navigate", "Load a URL in a tab of this seat.", json!([seat(), tab(), url()]), |b, call, who| {
        let (id, t, u) = (usage(req(&call, "seat"))?, usage(req(&call, "tab"))?, usage(req(&call, "url"))?);
        b.navigate(&id, &who.0, &t, &u).await
    });
    let svc = verb!(svc, book, "cdp", "Run a page-level DevTools method on a tab of this seat. Target, Browser, Storage and cookie-wide methods are refused.",
        json!([seat(), tab(), p("method", "string", true, ""), p("params", "object", false, "")]),
        |b, call, who| {
            let (id, t, m) = (usage(req(&call, "seat"))?, usage(req(&call, "tab"))?, usage(req(&call, "method"))?);
            b.cdp(&id, &who.0, &t, &m, call.args.get("params").cloned().unwrap_or(Value::Null)).await
        });
    let svc = verb!(svc, book, "screenshot", "A PNG of a tab, base64.", json!([seat(), tab()]), |b, call, who| {
        let (id, t) = (usage(req(&call, "seat"))?, usage(req(&call, "tab"))?);
        b.cdp(&id, &who.0, &t, "Page.captureScreenshot", json!({"format": "png"})).await
    });
    let v = viewer.clone();
    let svc = verb!(svc, book, [node, v], "signin", "Ask Tony to pass a captcha or sign in on this seat's window. Files a request on the approvals queue and raises the window in the viewer.",
        json!([seat(), p("site", "string", true, ""), p("reason", "string", true, "")]),
        |b, call, who| {
            let (id, site, reason) = (usage(req(&call, "seat"))?, usage(req(&call, "site"))?, usage(req(&call, "reason"))?);
            b.seat_exists(&id, &who.0).await?;
            if reason.trim().is_empty() {
                return Err(BookError::Usage("a request needs a reason Tony can read".into()));
            }
            let args = json!({"kind": "browser_signin", "node": node, "reason": reason, "minutes": SIGNIN_MINUTES, "payload": {"seat": id, "site": site}});
            let filed = call.hub().call("operations", "approval_request", args, None).await.map_err(|e| BookError::Other(e.into()))?;
            b.raise_window(&id, &who.0).await?;
            Ok(json!({"request_id": filed.get("request_id").cloned().unwrap_or(Value::Null), "viewer": v}))
        });
    verb!(svc, book, [viewer], "viewer", "The one viewer that shows every window.", json!([]), |_b, _call, _who| Ok(json!({"viewer": viewer})))
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Null => false,
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

fn usage(r: Result<String, VerbError>) -> Result<String, BookError> {
    r.map_err(|e| BookError::Usage(e.message))
}

/// Check the recorded seats, reap what no seat owns, and open the service tabs, once Chrome answers.
async fn settle(book: Book, boxes: Arc<Containers>, settings: Arc<Settings>) {
    let mut delay = Duration::from_secs(5);
    loop {
        let step = async {
            book.verify().await?;
            let (ids, names) = book.recorded().await;
            boxes.reap(&ids, &names).await.map_err(BookError::Other)?;
            for (site, url) in &settings.service_sites {
                book.service_tab(site, url).await?;
            }
            Ok::<(), BookError>(())
        };
        match step.await {
            Ok(()) => return,
            Err(e) => tracing::warn!(error = ?e, "the browser is not settled yet; trying again"),
        }
        tokio::time::sleep(delay).await;
        delay = (delay * 2).min(Duration::from_secs(60));
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    let awm_dir = awm_plugin_sdk::config::awm_dir();
    awm_statecheck::cli::entry("browser", include_str!("../state.toml"), &awm_plugin_sdk::config::service_dir("browser"), &awm_dir)?;
    let svc = Service::from_env()?;
    let dir = svc.context().env().awm_dir.clone();
    let settings = Arc::new(Settings::load(&dir)?);
    let shared = Arc::new(awm_cdp::Browser::new(&settings.cdp));
    let boxes = Arc::new(Containers { image: settings.seat_image.clone(), seats_dir: settings.dir.join("seats"), tony_profile: settings.tony_profile.clone(), docker: Default::default() });
    let book: Book = Arc::new(SeatBook::new(shared, Some(Arc::new(boxes.clone())), Some(dir.join("seats.json"))));
    let loaded = book.load().await?;
    tracing::info!(seats = loaded, "read the recorded seats");
    let ctx = svc.context();
    let (b, s) = (book.clone(), settings.clone());
    service(svc, book, settings)
        .on_ready(async move {
            ctx.spawn("settle", async move {
                settle(b, boxes, s).await;
                Ok(())
            });
        })
        .run()
        .await
}

#[cfg(test)]
mod tests {
    use super::*;
    use awm_plugin_sdk::{Caller, Env};

    fn env() -> Env {
        Env { hub_url: "http://127.0.0.1:1".into(), service: "browser".into(), plugin: "browser".into(), node: "alcor".into(), awm_dir: PathBuf::from("/nonexistent"), token_file: None }
    }

    fn settings() -> Arc<Settings> {
        Arc::new(Settings { dir: "/nonexistent".into(), cdp: String::new(), viewer: "https://viewer".into(), service_sites: vec![], seat_image: String::new(), tony_profile: None })
    }

    fn built(book: Book) -> Service {
        service(Service::new(env()), book, settings())
    }

    #[test]
    fn the_ready_api_matches_the_golden_manifest() {
        let golden: Value = serde_json::from_str(include_str!("../tests/golden/browser.api.json")).unwrap();
        let book = Arc::new(SeatBook::new(Arc::new(seats::tests::FakeChrome::new("shared")), None, None));
        assert_eq!(built(book).manifest(), golden);
    }

    fn who(trust: Option<&str>) -> Caller {
        Caller { node: "mizar".into(), session: Some("sess-j".into()), trust: trust.map(str::to_string), ..Caller::default() }
    }

    async fn call(svc: &awm_plugin_sdk::TestService, verb: &str, mut args: Value, trust: Option<&str>) -> Value {
        if let Some(t) = trust {
            args["_caller_trust"] = json!(t);
        }
        svc.call_as(verb, args, who(trust), None).await.unwrap()
    }

    #[tokio::test]
    async fn a_call_with_no_level_is_untrusted() {
        let book = Arc::new(SeatBook::new(Arc::new(seats::tests::FakeChrome::new("shared")), None, None));
        let svc = built(book).into_test().await.unwrap();
        let unstamped = call(&svc, "seat_open", json!({}), None).await;
        let untrusted = call(&svc, "seat_open", json!({}), Some("untrusted")).await;
        assert!(unstamped.get("error").is_some() && unstamped == untrusted);
        let seat = call(&svc, "seat_open", json!({}), Some("cooperative")).await;
        assert_eq!(seat["profile"], "blank");
        let refused = call(&svc, "seat_open", json!({"profile": "tony"}), Some("cooperative")).await;
        assert!(refused["error"].as_str().unwrap().contains("trusted"));
    }

    #[tokio::test]
    async fn the_same_session_on_another_node_is_another_owner() {
        let book = Arc::new(SeatBook::new(Arc::new(seats::tests::FakeChrome::new("shared")), None, None));
        let svc = built(book).into_test().await.unwrap();
        let seat = call(&svc, "seat_open", json!({}), Some("trusted")).await;
        let other = Caller { node: "altair".into(), ..who(Some("trusted")) };
        let out = svc.call_as("tab_list", json!({"seat": seat["seat"], "_caller_trust": "trusted"}), other, None).await.unwrap();
        assert_eq!(out["error"], "no such seat for this session");
    }
}
