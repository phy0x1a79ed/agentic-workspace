//! Browser seats (fleet.md R102): each attached session drives its own window.
//!
//! A seat opens, switches and closes the tabs of its own window and nothing else. No seat
//! reaches the raw debug port: `cdp` runs a page-level method on a tab the seat owns, from an
//! allow-list. The trust level decides the profile (item 6).

use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::Arc;

use awm_cdp::CdpError;
use awm_plugin_sdk::Level;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::Mutex;

pub const PROFILES: [&str; 2] = ["tony", "blank"];

pub use awm_cdp::page_method_allowed as seat_may_run;

/// A refusal the verb answers as `{"error": ...}`, as the Python plugin did.
#[derive(Debug)]
pub enum BookError {
    Seat(String),
    Usage(String),
    Cdp(CdpError),
    Other(anyhow::Error),
}

impl From<CdpError> for BookError {
    fn from(e: CdpError) -> Self {
        BookError::Cdp(e)
    }
}

impl From<anyhow::Error> for BookError {
    fn from(e: anyhow::Error) -> Self {
        BookError::Other(e)
    }
}

fn seat_err(msg: &str) -> BookError {
    BookError::Seat(msg.to_string())
}

pub type CdpFuture<'a> = Pin<Box<dyn Future<Output = Result<Value, CdpError>> + Send + 'a>>;

/// A DevTools endpoint: the shared Chrome or a seat's own container.
pub trait Cdp: Send + Sync {
    fn send<'a>(&'a self, method: &'a str, params: Value, session: Option<&'a str>) -> CdpFuture<'a>;
    /// Changes each time the endpoint is dialled again: flat session ids die with it.
    fn generation(&self) -> u64 {
        0
    }
}

impl Cdp for awm_cdp::Browser {
    fn send<'a>(&'a self, method: &'a str, params: Value, session: Option<&'a str>) -> CdpFuture<'a> {
        Box::pin(awm_cdp::Browser::send(self, method, params, session))
    }
    fn generation(&self) -> u64 {
        awm_cdp::Browser::generation(self)
    }
}

pub type LaunchFuture = Pin<Box<dyn Future<Output = anyhow::Result<(String, u16)>> + Send>>;

/// Runs a seat in its own container.
pub trait Launcher: Send + Sync {
    /// Start a container for `seat_id` with `profile`: its name and its loopback debug port.
    fn launch(&self, seat_id: &str, profile: &str) -> LaunchFuture;
    fn stop(&self, name: &str) -> Pin<Box<dyn Future<Output = ()> + Send>>;
    /// The endpoint of a container's debug port.
    fn connect(&self, port: u16) -> Arc<dyn Cdp>;
    /// Delete what a seat left on disk.
    fn forget(&self, _seat_id: &str) {}
}

#[derive(Clone, Serialize, Deserialize)]
pub struct Seat {
    pub seat: String,
    pub session: String,
    pub level: String,
    pub profile: String,
    /// A blank browser context. None is Tony's default profile.
    pub context: Option<String>,
    pub container: Option<String>,
    #[serde(default)]
    pub port: Option<u16>,
    pub window: Option<i64>,
    /// Target ids in opening order.
    pub tabs: Vec<String>,
    #[serde(skip)]
    flat: Vec<(String, String)>,
    #[serde(skip)]
    generation: Option<u64>,
    #[serde(skip)]
    cdp: Option<Arc<dyn Cdp>>,
}

impl Seat {
    pub fn view(&self) -> Value {
        let mut tabs = self.tabs.clone();
        tabs.sort();
        json!({"seat": self.seat, "level": self.level, "profile": self.profile, "container": self.container, "window": self.window, "tabs": tabs})
    }

    fn cdp(&self) -> Arc<dyn Cdp> {
        self.cdp.clone().expect("a seat in the book has its endpoint")
    }

    fn flat(&self, target: &str) -> Option<&str> {
        self.flat.iter().find(|(t, _)| t == target).map(|(_, s)| s.as_str())
    }
}

#[derive(Default, Serialize, Deserialize)]
struct Saved {
    seats: Vec<Seat>,
}

#[derive(Default)]
struct State {
    seats: Vec<Seat>,
    service_window: Option<i64>,
    /// Site, target and flat session, valid for `service_generation`'s connection.
    service_tabs: Vec<(String, String, String)>,
    service_generation: Option<u64>,
}

/// Every seat on the shared Chrome and in the seats' own containers.
pub struct SeatBook {
    shared: Arc<dyn Cdp>,
    launcher: Option<Arc<dyn Launcher>>,
    store: Option<PathBuf>,
    state: Mutex<State>,
}

fn new_seat_id() -> String {
    use base64::Engine;
    let bytes = uuid::Uuid::new_v4().into_bytes();
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(&bytes[..12])
}

fn origin(url: &str) -> &str {
    let rest = url.split_once("://").map(|(_, r)| r).unwrap_or(url);
    let end = rest.find('/').map(|i| i + url.len() - rest.len()).unwrap_or(url.len());
    &url[..end]
}

impl SeatBook {
    pub fn new(shared: Arc<dyn Cdp>, launcher: Option<Arc<dyn Launcher>>, store: Option<PathBuf>) -> Self {
        SeatBook { shared, launcher, store, state: Mutex::new(State::default()) }
    }

    /// Read the seats a previous run recorded. Each is checked against Chrome on first use.
    pub async fn load(&self) -> anyhow::Result<usize> {
        let Some(path) = &self.store else { return Ok(0) };
        let saved: Saved = match std::fs::read(path) {
            Ok(b) => serde_json::from_slice(&b)?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(0),
            Err(e) => return Err(e.into()),
        };
        let mut st = self.state.lock().await;
        for mut seat in saved.seats {
            seat.cdp = Some(match (seat.port, &self.launcher) {
                (Some(port), Some(l)) => l.connect(port),
                _ => self.shared.clone(),
            });
            seat.generation = None;
            st.seats.push(seat);
        }
        Ok(st.seats.len())
    }

    fn save(&self, st: &State) {
        let Some(path) = &self.store else { return };
        let body = serde_json::to_vec_pretty(&json!({"seats": st.seats})).expect("seats serialise");
        let tmp = path.with_extension("json.tmp");
        let write = || -> std::io::Result<()> {
            use std::os::unix::fs::OpenOptionsExt;
            let mut f = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(&tmp)?;
            std::io::Write::write_all(&mut f, &body)?;
            std::fs::rename(&tmp, path)
        };
        if let Err(e) = write() {
            tracing::warn!(error = %e, path = %path.display(), "could not record the seats");
        }
    }

    /// The seat ids and container names the book holds.
    pub async fn recorded(&self) -> (Vec<String>, Vec<String>) {
        let st = self.state.lock().await;
        (st.seats.iter().map(|s| s.seat.clone()).collect(), st.seats.iter().filter_map(|s| s.container.clone()).collect())
    }

    // -- opening and closing ------------------------------------------------

    pub async fn open(&self, session: &str, level: &str, profile: Option<&str>, own_container: bool) -> Result<Seat, BookError> {
        let lvl: Level = level.parse().map_err(|_| BookError::Usage(format!("unknown trust level {level:?}")))?;
        if lvl == Level::Untrusted {
            return Err(seat_err("an untrusted session gets no browser seat"));
        }
        let profile = profile.filter(|p| !p.is_empty()).unwrap_or(if lvl == Level::Trusted { "tony" } else { "blank" });
        if !PROFILES.contains(&profile) {
            return Err(BookError::Usage("profile is one of ('tony', 'blank')".into()));
        }
        if profile == "tony" && lvl != Level::Trusted {
            return Err(seat_err("only a trusted session gets Tony's profile (fleet.md R102 item 6)"));
        }
        if session.is_empty() {
            return Err(seat_err("a seat belongs to a session: no calling session reached the browser"));
        }
        let seat_id = new_seat_id();
        let (cdp, container, port, context) = if own_container {
            let launcher = self.launcher.clone().ok_or_else(|| seat_err("this browser runs no per-seat containers"))?;
            let (name, port) = launcher.launch(&seat_id, profile).await?;
            (launcher.connect(port), Some(name), Some(port), None)
        } else if profile == "tony" {
            (self.shared.clone(), None, None, None)
        } else {
            let ctx = self.shared.send("Target.createBrowserContext", json!({"disposeOnDetach": false}), None).await?;
            (self.shared.clone(), None, None, ctx["browserContextId"].as_str().map(str::to_string))
        };
        let generation = Some(cdp.generation());
        let seat = Seat {
            seat: seat_id.clone(),
            session: session.into(),
            level: lvl.as_str().into(),
            profile: profile.into(),
            context,
            container,
            port,
            window: None,
            tabs: vec![],
            flat: vec![],
            generation,
            cdp: Some(cdp),
        };
        {
            let mut st = self.state.lock().await;
            st.seats.push(seat);
            self.save(&st);
        }
        self.tab_open_in(&seat_id, session, "about:blank", true).await?;
        let st = self.state.lock().await;
        Ok(st.seats.iter().find(|s| s.seat == seat_id).cloned().expect("the seat just opened"))
    }

    pub async fn close(&self, seat_id: &str, session: &str) -> Result<(), BookError> {
        let seat = {
            let mut st = self.state.lock().await;
            self.fresh(&mut st, seat_id, session).await?;
            let i = st.seats.iter().position(|s| s.seat == seat_id).expect("fresh found it");
            st.seats.remove(i)
        };
        let cdp = seat.cdp();
        for target in &seat.tabs {
            cdp.send("Target.closeTarget", json!({"targetId": target}), None).await?;
        }
        if let Some(ctx) = &seat.context {
            cdp.send("Target.disposeBrowserContext", json!({"browserContextId": ctx}), None).await?;
        }
        if let (Some(name), Some(l)) = (&seat.container, &self.launcher) {
            l.stop(name).await;
            l.forget(&seat.seat);
        }
        self.save(&*self.state.lock().await);
        Ok(())
    }

    /// The seat, checked against its owner, with its tabs attached on the current connection.
    async fn fresh<'s>(&self, st: &'s mut State, seat_id: &str, session: &str) -> Result<&'s mut Seat, BookError> {
        let i = st.seats.iter().position(|s| s.seat == seat_id && s.session == session).ok_or_else(|| seat_err("no such seat for this session"))?;
        let seat = &mut st.seats[i];
        let cdp = seat.cdp();
        if seat.generation == Some(cdp.generation()) {
            return Ok(&mut st.seats[i]);
        }
        let infos = cdp.send("Target.getTargets", json!({}), None).await?;
        let alive: Vec<String> = infos["targetInfos"].as_array().into_iter().flatten().filter_map(|t| t["targetId"].as_str().map(str::to_string)).collect();
        seat.tabs.retain(|t| alive.contains(t));
        seat.flat.clear();
        for t in seat.tabs.clone() {
            let flat = cdp.send("Target.attachToTarget", json!({"targetId": t, "flatten": true}), None).await?;
            seat.flat.push((t, flat["sessionId"].as_str().unwrap_or_default().to_string()));
        }
        seat.generation = Some(cdp.generation());
        if seat.tabs.is_empty() {
            let gone = st.seats.remove(i);
            tracing::info!(seat = %gone.seat, "a recorded seat has no tabs left: forgetting it");
            if let (Some(name), Some(l)) = (&gone.container, &self.launcher) {
                l.stop(name).await;
                l.forget(&gone.seat);
            }
            self.save(st);
            return Err(seat_err("no such seat for this session"));
        }
        self.save(st);
        Ok(&mut st.seats[i])
    }

    /// Check every recorded seat against Chrome, dropping those whose window is gone.
    pub async fn verify(&self) -> Result<(), BookError> {
        let ids: Vec<(String, String)> = self.state.lock().await.seats.iter().map(|s| (s.seat.clone(), s.session.clone())).collect();
        for (id, session) in ids {
            let mut st = self.state.lock().await;
            match self.fresh(&mut st, &id, &session).await {
                Ok(_) | Err(BookError::Seat(_)) => {}
                Err(e) => return Err(e),
            }
        }
        Ok(())
    }

    // -- tabs ---------------------------------------------------------------

    pub async fn tab_open(&self, seat_id: &str, session: &str, url: &str) -> Result<String, BookError> {
        self.tab_open_in(seat_id, session, url, false).await
    }

    async fn tab_open_in(&self, seat_id: &str, session: &str, url: &str, new_window: bool) -> Result<String, BookError> {
        let mut st = self.state.lock().await;
        let seat = self.fresh_or_new(&mut st, seat_id, session).await?;
        let cdp = seat.cdp();
        let mut params = json!({"url": url});
        if let Some(ctx) = &seat.context {
            params["browserContextId"] = json!(ctx);
        }
        if new_window || seat.tabs.is_empty() {
            params["newWindow"] = json!(true);
        } else {
            // A new tab lands in the focused window, so focus this seat's first.
            cdp.send("Target.activateTarget", json!({"targetId": seat.tabs[0]}), None).await?;
        }
        let target = cdp.send("Target.createTarget", params, None).await?["targetId"].as_str().unwrap_or_default().to_string();
        let window = cdp.send("Browser.getWindowForTarget", json!({"targetId": target}), None).await?["windowId"].as_i64();
        if seat.window.is_some() && window != seat.window {
            cdp.send("Target.closeTarget", json!({"targetId": target}), None).await?;
            return Err(seat_err("the tab opened outside this seat's window; try again"));
        }
        seat.window = window;
        let flat = cdp.send("Target.attachToTarget", json!({"targetId": target, "flatten": true}), None).await?["sessionId"].as_str().unwrap_or_default().to_string();
        seat.tabs.push(target.clone());
        seat.flat.push((target.clone(), flat));
        self.save(&st);
        Ok(target)
    }

    /// A seat that just opened has no tabs yet, so it skips the stale-tab check.
    async fn fresh_or_new<'s>(&self, st: &'s mut State, seat_id: &str, session: &str) -> Result<&'s mut Seat, BookError> {
        let i = st.seats.iter().position(|s| s.seat == seat_id && s.session == session).ok_or_else(|| seat_err("no such seat for this session"))?;
        if st.seats[i].tabs.is_empty() {
            return Ok(&mut st.seats[i]);
        }
        self.fresh(st, seat_id, session).await
    }

    pub async fn tab_close(&self, seat_id: &str, session: &str, target: &str) -> Result<(), BookError> {
        let mut st = self.state.lock().await;
        let seat = self.fresh(&mut st, seat_id, session).await?;
        if !seat.tabs.iter().any(|t| t == target) {
            return Err(seat_err("that tab is not in this seat's window"));
        }
        seat.cdp().send("Target.closeTarget", json!({"targetId": target}), None).await?;
        seat.tabs.retain(|t| t != target);
        seat.flat.retain(|(t, _)| t != target);
        self.save(&st);
        Ok(())
    }

    pub async fn tab_activate(&self, seat_id: &str, session: &str, target: &str) -> Result<(), BookError> {
        let mut st = self.state.lock().await;
        let seat = self.fresh(&mut st, seat_id, session).await?;
        if !seat.tabs.iter().any(|t| t == target) {
            return Err(seat_err("that tab is not in this seat's window"));
        }
        seat.cdp().send("Target.activateTarget", json!({"targetId": target}), None).await?;
        Ok(())
    }

    pub async fn tab_list(&self, seat_id: &str, session: &str) -> Result<Value, BookError> {
        let (cdp, tabs) = {
            let mut st = self.state.lock().await;
            let seat = self.fresh(&mut st, seat_id, session).await?;
            (seat.cdp(), seat.tabs.clone())
        };
        let infos = cdp.send("Target.getTargets", json!({}), None).await?;
        let rows: Vec<Value> = infos["targetInfos"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|t| t["targetId"].as_str().is_some_and(|id| tabs.iter().any(|x| x == id)))
            .map(|t| json!({"tab": t["targetId"], "url": t.get("url"), "title": t.get("title")}))
            .collect();
        Ok(Value::Array(rows))
    }

    pub async fn cdp(&self, seat_id: &str, session: &str, target: &str, method: &str, params: Value) -> Result<Value, BookError> {
        let (cdp, flat) = {
            let mut st = self.state.lock().await;
            let seat = self.fresh(&mut st, seat_id, session).await?;
            let flat = seat.flat(target).ok_or_else(|| seat_err("that tab is not in this seat's window"))?.to_string();
            (seat.cdp(), flat)
        };
        if !seat_may_run(method) {
            return Err(BookError::Seat(format!("{method} is not open to a seat")));
        }
        let params = if params.is_null() { json!({}) } else { params };
        Ok(cdp.send(method, params, Some(&flat)).await?)
    }

    pub async fn navigate(&self, seat_id: &str, session: &str, target: &str, url: &str) -> Result<Value, BookError> {
        self.cdp(seat_id, session, target, "Page.navigate", json!({"url": url})).await
    }

    /// Bring a seat's window to the front of the one viewer (item 3).
    pub async fn raise_window(&self, seat_id: &str, session: &str) -> Result<(), BookError> {
        let mut st = self.state.lock().await;
        let seat = self.fresh(&mut st, seat_id, session).await?;
        let cdp = seat.cdp();
        if let Some(w) = seat.window {
            cdp.send("Browser.setWindowBounds", json!({"windowId": w, "bounds": {"windowState": "normal"}}), None).await?;
        }
        if let Some(t) = seat.tabs.first() {
            cdp.send("Target.activateTarget", json!({"targetId": t}), None).await?;
        }
        Ok(())
    }

    pub async fn seat_exists(&self, seat_id: &str, session: &str) -> Result<(), BookError> {
        let mut st = self.state.lock().await;
        self.fresh(&mut st, seat_id, session).await.map(|_| ())
    }

    // -- the service window (item 4) ----------------------------------------

    /// The site's tab in the service window, driven only by the singleton, with its flat
    /// session. A tab already open on the site's origin is adopted, so a restart opens no
    /// second one. Chrome saves the window's downloads to `download_dir`.
    async fn service_tab(&self, site: &str, url: &str, download_dir: &str) -> Result<(String, String), BookError> {
        let mut st = self.state.lock().await;
        let cdp = self.shared.clone();
        if st.service_generation != Some(cdp.generation()) {
            st.service_tabs.clear();
            st.service_window = None;
        }
        if let Some((_, t, f)) = st.service_tabs.iter().find(|(s, _, _)| s == site) {
            return Ok((t.clone(), f.clone()));
        }
        let infos = cdp.send("Target.getTargets", json!({}), None).await?;
        let seat_tabs: Vec<&String> = st.seats.iter().flat_map(|s| s.tabs.iter()).collect();
        let found = infos["targetInfos"].as_array().into_iter().flatten().find(|t| {
            t["type"].as_str().unwrap_or("page") == "page"
                && t["url"].as_str().is_some_and(|u| origin(u) == origin(url))
                && t["targetId"].as_str().is_some_and(|id| !seat_tabs.iter().any(|x| *x == id))
        });
        let target = match found.and_then(|t| t["targetId"].as_str()) {
            Some(t) => t.to_string(),
            None => {
                let params = json!({"url": url, "newWindow": st.service_window.is_none()});
                if st.service_window.is_some() {
                    if let Some((_, first, _)) = st.service_tabs.first() {
                        cdp.send("Target.activateTarget", json!({"targetId": first}), None).await?;
                    }
                }
                cdp.send("Target.createTarget", params, None).await?["targetId"].as_str().unwrap_or_default().to_string()
            }
        };
        if st.service_tabs.is_empty() {
            cdp.send("Browser.setDownloadBehavior", json!({"behavior": "allow", "downloadPath": download_dir}), None).await?;
        }
        st.service_window = cdp.send("Browser.getWindowForTarget", json!({"targetId": target}), None).await?["windowId"].as_i64();
        let flat = cdp.send("Target.attachToTarget", json!({"targetId": target, "flatten": true}), None).await?["sessionId"].as_str().unwrap_or_default().to_string();
        st.service_tabs.push((site.into(), target.clone(), flat.clone()));
        st.service_generation = Some(cdp.generation());
        Ok((target, flat))
    }

    /// Run a page-level method on the site's service tab, opening the tab first.
    pub async fn service_send(&self, site: &str, url: &str, download_dir: &str, method: &str, params: Value) -> Result<Value, BookError> {
        if !seat_may_run(method) {
            return Err(BookError::Seat(format!("{method} is not open on a service tab")));
        }
        let (_, flat) = self.service_tab(site, url, download_dir).await?;
        let params = if params.is_null() { json!({}) } else { params };
        Ok(self.shared.send(method, params, Some(&flat)).await?)
    }

    /// Close the site's service tab, if open.
    pub async fn service_close(&self, site: &str) -> Result<bool, BookError> {
        let mut st = self.state.lock().await;
        let Some(i) = st.service_tabs.iter().position(|(s, _, _)| s == site) else { return Ok(false) };
        let (_, target, _) = st.service_tabs.remove(i);
        if st.service_generation == Some(self.shared.generation()) {
            self.shared.send("Target.closeTarget", json!({"targetId": target}), None).await?;
        }
        Ok(true)
    }

    /// How many seats live on the shared Chrome rather than in their own containers.
    pub async fn shared_seats(&self) -> usize {
        self.state.lock().await.seats.iter().filter(|s| s.container.is_none()).count()
    }

    pub async fn list(&self, session: Option<&str>) -> Vec<Value> {
        let st = self.state.lock().await;
        st.seats.iter().filter(|s| session.is_none_or(|x| s.session == x)).map(Seat::view).collect()
    }
}

#[cfg(test)]
pub(crate) mod tests;
