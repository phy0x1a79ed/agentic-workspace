//! The `browser` singleton's verbs (fleet.md R102).

mod chrome;
mod containers;
mod grants;
mod seats;

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use awm_plugin_sdk::{Call, Lane, Service, VerbError};
use serde_json::{json, Value};

use crate::chrome::Chrome;
use crate::containers::Containers;
use crate::grants::{Grant, Grants};
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
    /// The nodes whose trusted sessions may have Tony's profile. A trusted level from any
    /// other node counts as cooperative.
    profile_nodes: Vec<String>,
    chrome: Option<chrome::Run>,
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
            profile_nodes: cfg.get("profile_nodes").and_then(Value::as_array).map(|a| a.iter().filter_map(Value::as_str).map(str::to_string).collect()).unwrap_or_default(),
            chrome: cfg.get("chrome").and_then(chrome::Run::from_json),
        })
    }
}

fn p(name: &str, ty: &str, required: bool, desc: &str) -> Value {
    json!({"name": name, "type": ty, "required": required, "description": desc})
}

/// A session's level for Tony's profile. A seat's level is its node's word, and a node whose
/// seats share a user with its door key could claim anything. So a trusted level counts only
/// when every node behind the call is in `profile_nodes`; the node comes from the door's key.
fn profile_level<'a>(profile_nodes: &[String], call: &Call, level: &'a str) -> &'a str {
    let c = &call.caller;
    let listed = std::iter::once(c.node.as_str()).chain(c.origin.as_deref()).all(|n| profile_nodes.iter().any(|x| x == n));
    if level == "trusted" && !listed {
        "cooperative"
    } else {
        level
    }
}

/// The calling session and its level. A seat is keyed on the session and the node it runs on.
/// A caller that is no session, such as a shell on this node, gets no key and so no seat.
fn caller(call: &Call) -> (String, String) {
    let c = &call.caller;
    let claim = call.caller_claim();
    let from_claim = |k: &str| claim.and_then(|v| v.get(k)).and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_string);
    let session = c.session.clone().filter(|s| !s.is_empty()).or_else(|| from_claim("session_id")).or_else(|| from_claim("name"));
    let key = session.map(|s| format!("{}/{s}", c.node)).unwrap_or_default();
    (key, call.caller_trust().unwrap_or("untrusted").to_string())
}

/// The service-tab verbs answer only productivity, calling on its own account from this node.
fn productivity_only(call: &Call) -> Result<(), BookError> {
    let c = &call.caller;
    if call.plugin.as_deref() == Some("productivity") && c.lane == Lane::Service && c.node == call.ctx.env().node && c.session.is_none() {
        return Ok(());
    }
    Err(BookError::Seat("only the productivity service drives the service tabs".into()))
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

fn service(svc: Service, book: Book, settings: Arc<Settings>, chrome: Arc<Chrome>, grants: Arc<Grants>) -> Service {
    let seat = || p("seat", "string", true, "The seat id that seat_open returned.");
    let tab = || p("tab", "string", true, "A tab id of this seat's window.");
    let url = || p("url", "string", true, "");
    let viewer = settings.viewer.clone();
    let node = svc.context().env().node.clone();
    let svc = svc.api("description", json!(DESCRIPTION)).api("emitters", json!([])).api("sessions", json!([]));
    let (c, s) = (chrome.clone(), settings.clone());
    let svc = verb!(svc, book, [c, s, grants], "seat_open", "Attach this session to its own browser window. A trusted session on a node the browser lists gets Tony's profile unless it asks for a blank one. A trusted session on another node gets it once Tony approves that session: asking for profile tony files the request and answers pending until he does. Any other session gets a blank one. Returns the seat id.",
        json!([p("profile", "string", false, "tony or blank."), p("own_container", "boolean", false, "Run this seat in its own container.")]),
        |b, call, who| {
            c.ensure().await?;
            let own = call.args.get("own_container").map(truthy).unwrap_or(false);
            let asked = call.str_arg("profile");
            let mut level = profile_level(&s.profile_nodes, &call, &who.1);
            let mut why = None;
            if let Some((node, session)) = who.0.split_once('/').filter(|_| level != who.1) {
                let unlisted = format!("{node} is not in the browser's profile_nodes");
                match grants.check(node, session, asked == Some("tony")).await? {
                    Grant::Live => level = "trusted",
                    Grant::Pending(rid) => return Ok(json!({"pending": true, "request_id": rid,
                        "why": format!("{unlisted}, so Tony approves this session's use of his profile. Request {rid} waits for him; call seat_open again once he approves.")})),
                    Grant::None => why = Some(format!("{unlisted}, so this seat is blank. Call seat_open with profile tony to ask Tony to approve this session.")),
                }
            }
            let mut view = b.open(&who.0, level, asked, own).await?.view();
            if let Some(w) = why {
                view["why"] = json!(w);
            }
            Ok(view)
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
    let (v, c) = (viewer.clone(), chrome.clone());
    let svc = verb!(svc, book, [node, v, c], "signin", "Ask Tony to pass a captcha or sign in on this seat's window. Files a request on the approvals queue and raises the window in the viewer.",
        json!([seat(), p("site", "string", true, ""), p("reason", "string", true, "")]),
        |b, call, who| {
            let (id, site, reason) = (usage(req(&call, "seat"))?, usage(req(&call, "site"))?, usage(req(&call, "reason"))?);
            c.ensure().await?;
            b.seat_exists(&id, &who.0).await?;
            if reason.trim().is_empty() {
                return Err(BookError::Usage("a request needs a reason Tony can read".into()));
            }
            let args = json!({"kind": "browser_signin", "node": node, "reason": reason, "minutes": SIGNIN_MINUTES, "payload": {"seat": id, "site": site}});
            let filed = call.hub().call("operations", "approval_request", args, None).await.map_err(|e| BookError::Other(e.into()))?;
            b.raise_window(&id, &who.0).await?;
            Ok(json!({"request_id": filed.get("request_id").cloned().unwrap_or(Value::Null), "viewer": v}))
        });
    let c = chrome.clone();
    let svc = verb!(svc, book, [viewer, c], "viewer", "The one viewer that shows every window. Starts the shared Chrome when it is stopped.", json!([]), |_b, _call, _who| {
        c.ensure().await?;
        Ok(json!({"viewer": viewer}))
    });
    service_verbs(svc, book, settings, chrome)
}

/// The productivity service's verbs: its own tab per site in the service window.
fn service_verbs(svc: Service, book: Book, settings: Arc<Settings>, chrome: Arc<Chrome>) -> Service {
    let site = || p("site", "string", true, "A site the browser config names, such as slack, teams or discord.");
    let (s, c) = (settings.clone(), chrome.clone());
    let svc = verb!(svc, book, [s, c], "service_cdp", "Run a page-level DevTools method on the site's tab in the service window, opening the tab first. Only the productivity service may call it.",
        json!([site(), p("method", "string", true, ""), p("params", "object", false, "")]),
        |b, call, _who| {
            productivity_only(&call)?;
            let (site, method) = (usage(req(&call, "site"))?, usage(req(&call, "method"))?);
            let url = s.service_sites.iter().find(|(k, _)| *k == site).map(|(_, u)| u.clone()).ok_or_else(|| BookError::Usage(format!("no service site named {site:?}")))?;
            c.ensure().await?;
            b.service_send(&site, &url, &method, call.args.get("params").cloned().unwrap_or(Value::Null)).await
        });
    verb!(svc, book, "service_close", "Close the site's tab in the service window. Only the productivity service may call it.", json!([site()]), |b, call, _who| {
        productivity_only(&call)?;
        let site = usage(req(&call, "site"))?;
        b.service_close(&site).await.map(|closed| json!({"closed": closed}))
    })
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

/// Reap what no seat owns, and check the recorded seats against Chrome once it answers.
async fn settle(book: Book, boxes: Arc<Containers>, chrome: Arc<Chrome>) {
    let mut delay = Duration::from_secs(5);
    loop {
        let step = async {
            let (ids, names) = book.recorded().await;
            boxes.reap(&ids, &names).await.map_err(BookError::Other)?;
            if !ids.is_empty() {
                chrome.ensure().await?;
                book.verify().await?;
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

/// Stop the shared Chrome once no seat sits on it and nothing has used it for the idle period.
async fn idle_stop(book: Book, chrome: Arc<Chrome>) {
    loop {
        tokio::time::sleep(Duration::from_secs(60)).await;
        chrome.stop_if_idle(book.shared_seats().await).await;
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    let awm_dir = awm_plugin_sdk::config::awm_dir();
    awm_statecheck::cli::state_subcommand("browser", include_str!("../state.toml"), &awm_plugin_sdk::config::service_dir("browser"), &awm_dir)?;
    let svc = Service::from_env()?;
    let dir = svc.context().env().awm_dir.clone();
    let settings = Arc::new(Settings::load(&dir)?);
    let shared = Arc::new(awm_cdp::Browser::new(&settings.cdp));
    let boxes = Arc::new(Containers { image: settings.seat_image.clone(), seats_dir: settings.dir.join("seats"), tony_profile: settings.tony_profile.clone(), docker: Default::default() });
    let book: Book = Arc::new(SeatBook::new(shared, Some(Arc::new(boxes.clone())), Some(dir.join("seats.json"))));
    let loaded = book.load().await?;
    tracing::info!(seats = loaded, "read the recorded seats");
    let chrome = Arc::new(Chrome::new(settings.chrome.clone(), &settings.cdp));
    let ctx = svc.context();
    let grants = Arc::new(Grants::load(Arc::new(ctx.hub().clone()), Some(dir.join("profile-grants.json"))));
    let (b, c) = (book.clone(), chrome.clone());
    service(svc, book, settings, chrome, grants)
        .on_ready(async move {
            let (b2, c2) = (b.clone(), c.clone());
            ctx.spawn("settle", async move {
                settle(b, boxes, c).await;
                Ok(())
            });
            ctx.spawn("idle_stop", async move {
                idle_stop(b2, c2).await;
                Ok(())
            });
        })
        .run()
        .await
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    use awm_plugin_sdk::{Caller, Env};

    fn env() -> Env {
        Env { hub_url: "http://127.0.0.1:1".into(), service: "browser".into(), plugin: "browser".into(), node: "alcor".into(), awm_dir: PathBuf::from("/nonexistent"), token_file: None }
    }

    fn settings() -> Arc<Settings> {
        Arc::new(Settings { dir: "/nonexistent".into(), cdp: String::new(), viewer: "https://viewer".into(), service_sites: vec![("teams".into(), "https://teams.microsoft.com".into())], seat_image: String::new(), tony_profile: None, profile_nodes: vec!["mizar".into()], chrome: None })
    }

    /// The approver as operations answers it, with a test grant key. `approve` signs a grant.
    #[derive(Default)]
    struct FakeApprover {
        requests: std::sync::Mutex<Vec<(String, Value)>>,
        decided: std::sync::Mutex<HashMap<String, (String, Value)>>,
    }

    const SIGNER: [u8; 32] = [7; 32];

    impl FakeApprover {
        fn approve(&self, rid: &str, node: &str, session: &str, minutes: i64) {
            let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs() as i64;
            let body = json!({"grant_id": "g1", "request_id": rid, "kind": grants::KIND, "node": node, "subject": "tony",
                "payload_sha256": awm_trust::grant::payload_digest(&json!({"node": node, "session_id": session})),
                "not_before": now - 60, "not_after": now + minutes * 60, "approved_by_device": "d"});
            let env = awm_trust::grant::sign_envelope(&ed25519_dalek::SigningKey::from_bytes(&SIGNER), "grant", body);
            self.decided.lock().unwrap().insert(rid.into(), ("approved".into(), env));
        }
    }

    impl grants::Approver for FakeApprover {
        fn call<'a>(&'a self, verb: &'a str, args: Value) -> grants::Reply<'a> {
            Box::pin(async move {
                Ok(match verb {
                    "keys" => json!({"grant_pub": awm_trust::grant::b64u(ed25519_dalek::SigningKey::from_bytes(&SIGNER).verifying_key().as_bytes())}),
                    "approval_request" => {
                        let mut r = self.requests.lock().unwrap();
                        let rid = format!("q{}", r.len());
                        r.push((rid.clone(), args));
                        json!({"request_id": rid})
                    }
                    "approval_get" => {
                        let rid = args["request_id"].as_str().unwrap();
                        match self.decided.lock().unwrap().get(rid) {
                            Some((status, grant)) => json!({"status": status, "grant": grant}),
                            None => json!({"status": "pending", "grant": null}),
                        }
                    }
                    _ => anyhow::bail!("no verb {verb}"),
                })
            })
        }
    }

    fn built_with(book: Book, approver: Arc<FakeApprover>) -> Service {
        let s = settings();
        let chrome = Arc::new(Chrome::new(s.chrome.clone(), &s.cdp));
        service(Service::new(env()), book, s, chrome, Arc::new(Grants::load(approver, None)))
    }

    fn built(book: Book) -> Service {
        built_with(book, Arc::default())
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
    async fn tonys_profile_goes_to_trusted_sessions_on_listed_nodes_or_with_tonys_approval() {
        let book = Arc::new(SeatBook::new(Arc::new(seats::tests::FakeChrome::new("shared")), None, None));
        let ap = Arc::new(FakeApprover::default());
        let svc = built_with(book, ap.clone()).into_test().await.unwrap();
        let open = |caller: Caller, profile: Option<&str>| {
            let mut args = json!({"_caller_trust": caller.trust.clone()});
            if let Some(p) = profile {
                args["profile"] = json!(p);
            }
            let svc = &svc;
            async move { svc.call_as("seat_open", args, caller, None).await.unwrap() }
        };
        assert_eq!(open(who(Some("trusted")), None).await["profile"], "tony", "mizar is listed");
        let altair = Caller { node: "altair".into(), ..who(Some("trusted")) };
        let relayed = Caller { origin: Some("altair".into()), session: Some("sess-r".into()), ..who(Some("trusted")) };
        for (name, c) in [("altair", altair.clone()), ("relayed from altair", relayed)] {
            let blank = open(c.clone(), None).await;
            assert_eq!(blank["profile"], "blank", "{name}");
            assert!(blank["why"].as_str().unwrap().contains("not in the browser's profile_nodes"), "{name}: {blank}");
            let asked = open(c, Some("tony")).await;
            assert_eq!(asked["pending"], true, "{name}: {asked}");
            assert!(asked["why"].as_str().unwrap().contains("not in the browser's profile_nodes"), "{name}");
        }
        assert_eq!(ap.requests.lock().unwrap().len(), 2);
        let again = open(altair.clone(), Some("tony")).await;
        assert_eq!((again["pending"].clone(), again["request_id"].clone()), (json!(true), json!("q0")), "a pending request is not filed twice");

        let (rid, args) = ap.requests.lock().unwrap()[0].clone();
        assert_eq!((args["kind"].clone(), args["node"].clone(), args["payload"].clone()), (json!("browser_profile"), json!("altair"), json!({"node": "altair", "session_id": "sess-j"})));
        for (name, node, session, minutes) in [("another session", "altair", "sess-x", 30), ("another node", "lyra", "sess-j", 30), ("expired", "altair", "sess-j", -1)] {
            ap.approve(&rid, node, session, minutes);
            let out = open(altair.clone(), Some("tony")).await;
            assert_eq!(out["pending"], true, "{name}: a grant that does not fit files a new request: {out}");
            assert_ne!(out["request_id"], json!(rid), "{name}");
        }
        let rid = ap.requests.lock().unwrap().last().unwrap().0.clone();
        ap.approve(&rid, "altair", "sess-j", 30);
        let seat = open(altair.clone(), Some("tony")).await;
        assert_eq!((seat["profile"].clone(), seat["level"].clone()), (json!("tony"), json!("trusted")), "{seat}");
        assert_eq!(open(altair.clone(), None).await["profile"], "tony", "the grant holds without asking again");
        let other = Caller { session: Some("sess-y".into()), ..altair };
        assert_eq!(open(other, None).await["profile"], "blank", "the grant is for one session");

        let filed = ap.requests.lock().unwrap().len();
        let coop = Caller { node: "altair".into(), ..who(Some("cooperative")) };
        assert!(open(coop, Some("tony")).await["error"].as_str().unwrap().contains("this session is cooperative"));
        assert_eq!(ap.requests.lock().unwrap().len(), filed, "a cooperative session files no request");
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

    fn bare_call(plugin: Option<&str>, caller: Caller) -> Call {
        Call { id: json!("1"), tool: "browser".into(), verb: "service_cdp".into(), args: json!({}), caller, as_: None, plugin: plugin.map(str::to_string), cancel: awm_plugin_sdk::CancelToken::new(), ctx: Service::new(env()).context() }
    }

    #[test]
    fn only_productivity_on_its_own_account_here_drives_the_service_tabs() {
        let own = Caller { node: "alcor".into(), lane: Lane::Service, trust: Some("trusted".into()), ..Caller::default() };
        assert!(productivity_only(&bare_call(Some("productivity"), own.clone())).is_ok());
        let refused = [
            bare_call(Some("social"), own.clone()),
            bare_call(None, own.clone()),
            bare_call(Some("productivity"), Caller { node: "mizar".into(), ..own.clone() }),
            bare_call(Some("productivity"), Caller { session: Some("cc1".into()), ..own.clone() }),
            bare_call(Some("productivity"), Caller { lane: Lane::Local, ..own }),
        ];
        assert!(refused.iter().all(|c| productivity_only(c).is_err()));
    }

    #[tokio::test]
    async fn a_session_reaches_no_service_verb() {
        let book = Arc::new(SeatBook::new(Arc::new(seats::tests::FakeChrome::new("shared")), None, None));
        let svc = built(book).into_test().await.unwrap();
        for (verb, args) in [("service_cdp", json!({"site": "teams", "method": "Runtime.evaluate"})), ("service_close", json!({"site": "teams"}))] {
            let out = call(&svc, verb, args, Some("trusted")).await;
            assert!(out["error"].as_str().unwrap().contains("only the productivity service"), "{verb}: {out}");
        }
    }
}
