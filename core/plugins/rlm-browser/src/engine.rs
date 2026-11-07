//! The real-browser engine: raw CDP over a swappable launch seam.
//!
//! A launch backend (host subprocess or docker) starts a Chrome on `127.0.0.1:<port>` for the
//! `headless` and `rendered` modes. `attached` launches nothing and borrows an existing
//! Chrome. The manager keys sessions by id and serves the realm verbs. A session the cache
//! lost to a restart is dialled again from its row.
//!
//! An attached Chrome is Tony's, logged in. Below trusted a session gets a guest window: a
//! fresh browser context in that Chrome, its tabs only, and the page-level allow-list.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use awm_cdp::{CdpError, Connection, Docker};
use awm_plugin_sdk::{Context, Level};
use serde_json::{json, Value};
use tokio::sync::Mutex;

use crate::store::{self, Store};

pub const DEFAULT_MODE: &str = "headless";
const MODES: [&str; 3] = ["headless", "rendered", "attached"];
pub const BROWSER_TAB: &str = "__browser__";

/// Canonicalise a mode. Anything unknown falls back to the default.
pub fn norm_mode(mode: Option<&str>) -> &'static str {
    let m = mode.unwrap_or("").trim().to_lowercase();
    MODES.iter().find(|x| **x == m).copied().unwrap_or(DEFAULT_MODE)
}

#[derive(Debug)]
pub enum EngineError {
    /// A usage error, as Python's ValueError.
    Value(String),
    Runtime(String),
}

impl From<CdpError> for EngineError {
    fn from(e: CdpError) -> Self {
        EngineError::Runtime(e.to_string())
    }
}

impl From<anyhow::Error> for EngineError {
    fn from(e: anyhow::Error) -> Self {
        EngineError::Runtime(e.to_string())
    }
}

type R<T> = Result<T, EngineError>;

/// The process environment the engine reads, as the Python service's env vars.
#[derive(Debug, Clone)]
pub struct Config {
    pub backend: String,
    pub attach_url: String,
    pub image: String,
    pub shm: String,
    pub profiles: PathBuf,
}

impl Config {
    pub fn from_env(profiles: PathBuf) -> Self {
        let env = |k: &str, d: &str| std::env::var(k).ok().map(|v| v.trim().to_string()).filter(|v| !v.is_empty()).unwrap_or_else(|| d.to_string());
        Config {
            backend: env("RLM_BROWSER_BACKEND", "host").to_lowercase(),
            attach_url: env("RLM_ATTACH_CDP_URL", "http://172.25.176.1:19222"),
            image: env("RLM_BROWSER_IMAGE", "rlm-browser:latest"),
            shm: env("RLM_BROWSER_SHM", "2g"),
            profiles,
        }
    }
}

/// A launched Chrome: how to reach it and how to stop it.
pub struct Handle {
    pub port: u16,
    pub profile_dir: String,
    pub container_name: String,
    pub vnc_port: u16,
    child: Option<tokio::process::Child>,
}

impl Handle {
    pub fn base(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }

    async fn stop(&mut self, docker: &Docker) {
        if let Some(child) = self.child.as_mut() {
            awm_plugin_sdk::proc::stop_group(child, Duration::from_secs(10)).await;
        }
        if !self.container_name.is_empty() {
            docker.remove(&self.container_name).await;
        }
    }
}

async fn launch_host(mode: &str, profile_dir: &str) -> R<Handle> {
    let port = awm_cdp::free_port().map_err(anyhow::Error::from)?;
    std::fs::create_dir_all(profile_dir).map_err(anyhow::Error::from)?;
    // A stale SingletonLock from a hard-killed Chrome blocks a relaunch on the same profile.
    let _ = std::fs::remove_file(std::path::Path::new(profile_dir).join("SingletonLock"));
    let chrome = awm_cdp::find_chrome().ok_or_else(|| EngineError::Runtime("no Chrome/Chromium binary found; set RLM_CHROME_BIN or install google-chrome-stable".into()))?;
    let mut cmd = tokio::process::Command::new(chrome);
    cmd.arg(format!("--remote-debugging-port={port}"))
        .arg("--remote-debugging-address=127.0.0.1")
        .arg(format!("--user-data-dir={profile_dir}"))
        .args(["--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--disable-extensions", "--disable-dev-shm-usage", "--disable-gpu"]);
    if mode != "rendered" {
        cmd.arg("--headless=new");
    }
    cmd.arg("about:blank").stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null());
    awm_plugin_sdk::proc::in_group(&mut cmd);
    tracing::info!(port, mode, "launching host chrome");
    let child = cmd.spawn().map_err(anyhow::Error::from)?;
    let mut h = Handle { port, profile_dir: profile_dir.into(), container_name: String::new(), vnc_port: 0, child: Some(child) };
    if let Err(e) = awm_cdp::wait_ready(&h.base(), Duration::from_secs(20)).await {
        h.stop(&Docker::default()).await;
        return Err(e.into());
    }
    Ok(h)
}

async fn launch_docker(cfg: &Config, docker: &Docker, mode: &str, profile_dir: &str) -> R<Handle> {
    let port = awm_cdp::free_port().map_err(anyhow::Error::from)?;
    std::fs::create_dir_all(profile_dir).map_err(anyhow::Error::from)?;
    let name = format!("rlm-browser-{}", &uuid::Uuid::new_v4().simple().to_string()[..12]);
    let vnc = if mode == "rendered" { awm_cdp::free_port().map_err(anyhow::Error::from)? } else { 0 };
    let mut args: Vec<String> = ["run", "-d", "--name", &name, &format!("--shm-size={}", cfg.shm), "-p", &format!("127.0.0.1:{port}:{port}"), "-v", &format!("{profile_dir}:/data"), "-e", &format!("RLM_MODE={mode}"), "-e", &format!("RLM_CDP_PORT={port}")].iter().map(|s| s.to_string()).collect();
    if vnc != 0 {
        args.extend(["-p".into(), format!("127.0.0.1:{vnc}:{vnc}"), "-e".into(), format!("RLM_NOVNC_PORT={vnc}")]);
    }
    args.push(cfg.image.clone());
    tracing::info!(container = %name, mode, port, vnc, "docker run");
    docker.run(&args.iter().map(String::as_str).collect::<Vec<_>>()).await?;
    let mut h = Handle { port, profile_dir: profile_dir.into(), container_name: name.clone(), vnc_port: vnc, child: None };
    if let Err(e) = awm_cdp::wait_ready(&h.base(), Duration::from_secs(40)).await {
        if let Ok(logs) = docker.run(&["logs", "--tail", "30", &name]).await {
            tracing::error!(container = %name, %logs, "container failed to become ready");
        }
        h.stop(docker).await;
        return Err(e.into());
    }
    Ok(h)
}

struct Sess {
    id: String,
    base: String,
    borrowed: bool,
    /// The browser context a guest is confined to, in a borrowed Chrome.
    guest: Option<String>,
    container: String,
    handle: Option<Handle>,
    browser: Option<Arc<Connection>>,
    tabs: HashMap<String, Arc<Connection>>,
}

/// Every live session, and the realm verbs over real CDP.
pub struct Manager {
    cfg: Config,
    docker: Docker,
    store: Store,
    ctx: Option<Context>,
    sessions: Mutex<HashMap<String, Arc<Mutex<Sess>>>>,
    lock: Mutex<()>,
    /// Attached sessions this node forwarded to the peer that owns the real Chrome. Local
    /// routing memory only: the peer's own row is the record.
    forwards: std::sync::Mutex<HashMap<String, String>>,
}

impl Manager {
    pub fn new(cfg: Config, store: Store, ctx: Option<Context>) -> Self {
        Manager { cfg, docker: Docker::default(), store, ctx, sessions: Mutex::default(), lock: Mutex::new(()), forwards: Default::default() }
    }

    pub fn note_forwarded(&self, id: &str, peer: &str) {
        self.forwards.lock().unwrap().insert(id.into(), peer.into());
    }

    pub fn forwarded_peer(&self, id: &str) -> Option<String> {
        self.forwards.lock().unwrap().get(id).cloned()
    }

    pub fn forget_forwarded(&self, id: &str) {
        self.forwards.lock().unwrap().remove(id);
    }

    fn backend_name(&self, mode: &str) -> &'static str {
        match (mode, self.cfg.backend.as_str()) {
            ("attached", _) => "attached",
            (_, "docker") => "docker",
            _ => "host",
        }
    }

    async fn launch(&self, mode: &str, profile_dir: &str) -> R<Handle> {
        if self.backend_name(mode) == "docker" {
            launch_docker(&self.cfg, &self.docker, mode, profile_dir).await
        } else {
            launch_host(mode, profile_dir).await
        }
    }

    /// The live session, dialled again from its row when the cache lost it to a restart.
    async fn resolve(&self, id: &str) -> R<Arc<Mutex<Sess>>> {
        if let Some(s) = self.sessions.lock().await.get(id) {
            return Ok(s.clone());
        }
        let row = self.store.get(id).await?.ok_or_else(|| EngineError::Value(format!("unknown session_id: {id:?}")))?;
        let borrowed = row["backend"] == "attached";
        let base = if borrowed {
            self.cfg.attach_url.trim_end_matches('/').to_string()
        } else {
            let port = row["cdp_port"].as_i64().unwrap_or(0);
            if port == 0 {
                return Err(EngineError::Value(format!("session {id:?} has no CDP port; re-acquire it")));
            }
            format!("http://127.0.0.1:{port}")
        };
        awm_cdp::wait_ready(&base, Duration::from_secs(5)).await?;
        let guest = row["_guest_context"].as_str().filter(|g| !g.is_empty()).map(str::to_string);
        let sess = Sess { id: id.into(), base: base.clone(), borrowed, guest, container: row["container_name"].as_str().unwrap_or("").into(), handle: None, browser: None, tabs: HashMap::new() };
        tracing::info!(session = id, %base, "re-attached session");
        let arc = Arc::new(Mutex::new(sess));
        self.sessions.lock().await.insert(id.into(), arc.clone());
        Ok(arc)
    }

    async fn browser(s: &mut Sess) -> R<Arc<Connection>> {
        if let Some(c) = s.browser.as_ref().filter(|c| !c.is_closed()) {
            return Ok(c.clone());
        }
        let c = Arc::new(Connection::connect(&awm_cdp::browser_ws(&s.base).await?).await?);
        s.browser = Some(c.clone());
        Ok(c)
    }

    async fn tab(&self, s: &mut Sess, tab: &str) -> R<Arc<Connection>> {
        if let Some(c) = s.tabs.get(tab).filter(|c| !c.is_closed()) {
            return Ok(c.clone());
        }
        let (conn, mut events) = Connection::connect_with_events(&awm_cdp::page_ws(&s.base, tab)).await?;
        if let Some(ctx) = self.ctx.clone() {
            let (sid, tid) = (s.id.clone(), tab.to_string());
            tokio::spawn(async move {
                while let Some(msg) = events.recv().await {
                    let kind = match msg["method"].as_str().unwrap_or("") {
                        "Page.loadEventFired" => "page_loaded",
                        "Page.javascriptDialogOpening" => "dialog",
                        "Inspector.targetCrashed" => "crashed",
                        _ => continue,
                    };
                    ctx.emit(crate::manifest::EMIT_TOPIC, json!({"session_id": sid, "tab_id": tid, "kind": kind, "data": msg.get("params").cloned().unwrap_or(json!({}))}));
                }
            });
        }
        let conn = Arc::new(conn);
        s.tabs.insert(tab.into(), conn.clone());
        Ok(conn)
    }

    /// The page targets the session may see: all of them, or a guest's own.
    async fn pages(s: &mut Sess) -> R<Vec<Value>> {
        let browser = Self::browser(s).await?;
        let infos = browser.send("Target.getTargets", json!({}), None).await?;
        Ok(infos["targetInfos"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|t| t["type"] == "page")
            .filter(|t| s.guest.as_deref().is_none_or(|g| t["browserContextId"].as_str() == Some(g)))
            .cloned()
            .collect())
    }

    async fn own_tab(s: &mut Sess, tab: &str) -> R<()> {
        if s.guest.is_none() {
            return Ok(());
        }
        if Self::pages(s).await?.iter().any(|t| t["targetId"] == tab) {
            Ok(())
        } else {
            Err(EngineError::Value(format!("tab {tab:?} is not in this session's guest window")))
        }
    }

    async fn new_tab(s: &mut Sess, url: &str) -> R<String> {
        let browser = Self::browser(s).await?;
        let mut params = json!({"url": url});
        if let Some(g) = &s.guest {
            params["browserContextId"] = json!(g);
        }
        Ok(browser.send("Target.createTarget", params, None).await?["targetId"].as_str().unwrap_or_default().to_string())
    }

    // -- lifecycle ----------------------------------------------------------

    pub async fn acquire(&self, game: &str, mode: Option<&str>, opts: &Value, level: Level) -> R<Value> {
        let mode = norm_mode(mode);
        let backend = self.backend_name(mode);
        let egress = opts.get("egress").cloned();
        let profile = self.cfg.profiles.join(if game.trim().is_empty() { "default" } else { game.trim() }).to_string_lossy().to_string();
        let _g = self.lock.lock().await;
        let (handle, base, guest) = if mode == "attached" {
            let actives: Vec<Value> = self.store.list().await?.into_iter().filter(|s| s["backend"] == "attached" && s["status"] == "active").collect();
            if let Some(a) = actives.first() {
                return Err(EngineError::Value(format!(
                    "an attached session is already active ({}); release it first — attached mode is single-window",
                    a["session_id"].as_str().unwrap_or("")
                )));
            }
            let url = opts.get("attach_url").and_then(Value::as_str).unwrap_or(&self.cfg.attach_url).trim().trim_end_matches('/').to_string();
            awm_cdp::wait_ready(&url, Duration::from_secs(8)).await?;
            tracing::info!(%url, "attaching to existing chrome");
            let guest = if level == Level::Trusted {
                None
            } else {
                let conn = Connection::connect(&awm_cdp::browser_ws(&url).await?).await?;
                let ctx = conn.send("Target.createBrowserContext", json!({"disposeOnDetach": false}), None).await?;
                ctx["browserContextId"].as_str().map(str::to_string)
            };
            (None, url, guest)
        } else {
            let h = self.launch(mode, &profile).await?;
            let base = h.base();
            (Some(h), base, None)
        };
        let port = handle.as_ref().map(|h| h.port).unwrap_or_else(|| base.rsplit(':').next().and_then(|p| p.parse().ok()).unwrap_or(0));
        let (udd, cname, vnc) = handle.as_ref().map(|h| (h.profile_dir.clone(), h.container_name.clone(), h.vnc_port)).unwrap_or_default();
        let row = self.store.create(game, mode, backend, port, &udd, &cname, vnc, egress, guest.as_deref().unwrap_or("")).await?;
        let id = row["session_id"].as_str().unwrap_or_default().to_string();
        let mut sess = Sess { id: id.clone(), base, borrowed: mode == "attached", guest: guest.clone(), container: cname, handle, browser: None, tabs: HashMap::new() };
        if guest.is_some() {
            Self::new_tab(&mut sess, "about:blank").await?;
        }
        self.sessions.lock().await.insert(id.clone(), Arc::new(Mutex::new(sess)));
        Ok(json!({"session_id": id, "mode": mode, "backend": backend}))
    }

    pub async fn release(&self, id: &str) -> R<Value> {
        let sess = self.resolve(id).await.ok();
        let _g = self.lock.lock().await;
        self.sessions.lock().await.remove(id);
        if let Some(s) = sess {
            self.teardown(&mut *s.lock().await).await;
        } else if let Some(row) = self.store.get(id).await? {
            // The Chrome is gone, but a container may still hold the name.
            if let Some(name) = row["container_name"].as_str().filter(|n| !n.is_empty()) {
                self.docker.remove(name).await;
            }
        }
        Ok(json!({"released": self.store.delete(id).await?}))
    }

    /// Close an owned Chrome through CDP, which works after a restart lost the launch handle.
    /// A borrowed Chrome is never closed: a guest's context is disposed instead.
    async fn teardown(&self, s: &mut Sess) {
        if let Ok(b) = Self::browser(s).await {
            if !s.borrowed {
                let _ = b.send_timeout("Browser.close", json!({}), None, Duration::from_secs(5)).await;
            } else if let Some(g) = s.guest.clone() {
                let _ = b.send("Target.disposeBrowserContext", json!({"browserContextId": g}), None).await;
            }
        }
        s.tabs.clear();
        s.browser = None;
        if let Some(h) = s.handle.as_mut() {
            h.stop(&self.docker).await;
        } else if !s.container.is_empty() {
            self.docker.remove(&s.container).await;
        }
    }

    /// Close every tab the session may close, keep the browser and the slot, open one blank tab.
    pub async fn reset(&self, id: &str) -> R<Value> {
        let arc = self.resolve(id).await?;
        let mut s = arc.lock().await;
        let browser = Self::browser(&mut s).await?;
        if !s.borrowed || s.guest.is_some() {
            for t in Self::pages(&mut s).await? {
                let _ = browser.send("Target.closeTarget", json!({"targetId": t["targetId"]}), None).await;
            }
        }
        s.tabs.clear();
        Self::new_tab(&mut s, "about:blank").await?;
        self.store.set_status(id, "active").await?;
        Ok(json!({"session_id": id, "status": "active"}))
    }

    pub async fn status(&self, id: Option<&str>) -> R<Value> {
        let rows = match id {
            Some(i) => self.store.get(i).await?.into_iter().collect(),
            None => self.store.list().await?,
        };
        let mut out = vec![];
        for row in rows {
            let sid = row["session_id"].as_str().unwrap_or_default().to_string();
            let mut entry = store::public(row);
            if let Ok(t) = self.tab_list(&sid).await {
                entry["tabs"] = t["tabs"].clone();
            }
            out.push(entry);
        }
        Ok(json!({"sessions": out}))
    }

    // -- tabs ---------------------------------------------------------------

    pub async fn tab_open(&self, id: &str, url: Option<&str>) -> R<Value> {
        let arc = self.resolve(id).await?;
        let mut s = arc.lock().await;
        Ok(json!({"tab_id": Self::new_tab(&mut s, url.unwrap_or("about:blank")).await?}))
    }

    pub async fn tab_close(&self, id: &str, tab: &str) -> R<Value> {
        let arc = self.resolve(id).await?;
        let mut s = arc.lock().await;
        Self::own_tab(&mut s, tab).await?;
        Self::browser(&mut s).await?.send("Target.closeTarget", json!({"targetId": tab}), None).await?;
        s.tabs.remove(tab);
        Ok(json!({"closed": true, "tab_id": tab}))
    }

    pub async fn tab_list(&self, id: &str) -> R<Value> {
        let arc = self.resolve(id).await?;
        let mut s = arc.lock().await;
        let tabs: Vec<Value> = Self::pages(&mut s).await?.iter().map(|t| json!({"tab_id": t["targetId"], "url": t.get("url"), "title": t.get("title"), "attached": t.get("attached")})).collect();
        Ok(json!({"tabs": tabs}))
    }

    pub async fn tab_activate(&self, id: &str, tab: &str) -> R<Value> {
        let arc = self.resolve(id).await?;
        let mut s = arc.lock().await;
        Self::own_tab(&mut s, tab).await?;
        Self::browser(&mut s).await?.send("Target.activateTarget", json!({"targetId": tab}), None).await?;
        Ok(json!({"activated": true, "tab_id": tab}))
    }

    async fn pick_tab(s: &mut Sess, tab: Option<&str>) -> R<String> {
        if let Some(t) = tab.filter(|t| !t.is_empty()) {
            Self::own_tab(s, t).await?;
            return Ok(t.into());
        }
        if let Some(t) = Self::pages(s).await?.first().and_then(|t| t["targetId"].as_str()) {
            return Ok(t.into());
        }
        Self::new_tab(s, "about:blank").await
    }

    // -- perceive -----------------------------------------------------------

    pub async fn observe(&self, id: &str, tab: Option<&str>) -> R<Value> {
        let arc = self.resolve(id).await?;
        let mut s = arc.lock().await;
        let tid = Self::pick_tab(&mut s, tab).await?;
        let conn = self.tab(&mut s, &tid).await?;
        drop(s);
        conn.send("Runtime.enable", json!({}), None).await?;
        let eval = |e: &'static str| {
            let conn = conn.clone();
            async move { conn.send("Runtime.evaluate", json!({"expression": e, "returnByValue": true}), None).await.map(|r| r["result"]["value"].clone()) }
        };
        let (html, url, title) = (eval("document.documentElement.outerHTML").await?, eval("location.href").await?, eval("document.title").await?);
        Ok(json!({"tab_id": tid, "html": html, "url": url, "title": title}))
    }

    pub async fn screenshot(&self, id: &str, tab: Option<&str>, full_page: bool) -> R<Value> {
        let arc = self.resolve(id).await?;
        let mut s = arc.lock().await;
        let tid = Self::pick_tab(&mut s, tab).await?;
        let conn = self.tab(&mut s, &tid).await?;
        drop(s);
        conn.send("Page.enable", json!({}), None).await?;
        let mut params = json!({"format": "png"});
        if full_page {
            let m = conn.send("Page.getLayoutMetrics", json!({}), None).await?;
            let css = m.get("cssContentSize").or_else(|| m.get("contentSize")).cloned().unwrap_or(json!({}));
            if css.as_object().is_some_and(|o| !o.is_empty()) {
                params["clip"] = json!({"x": 0, "y": 0, "width": css.get("width").cloned().unwrap_or(json!(0)), "height": css.get("height").cloned().unwrap_or(json!(0)), "scale": 1});
            }
            params["captureBeyondViewport"] = json!(true);
        }
        let shot = conn.send("Page.captureScreenshot", params, None).await?;
        Ok(json!({"tab_id": tid, "image": shot.get("data"), "format": "png"}))
    }

    // -- engine relay and discovery -----------------------------------------

    /// Relay any CDP method, to a tab's connection or, with `__browser__`, the browser's.
    /// A guest runs only page-level methods, on its own tabs.
    pub async fn cdp(&self, id: &str, method: &str, tab: Option<&str>, params: Value) -> R<Value> {
        let arc = self.resolve(id).await?;
        let mut s = arc.lock().await;
        if s.guest.is_some() && (tab == Some(BROWSER_TAB) || !awm_cdp::page_method_allowed(method)) {
            return Err(EngineError::Value(format!("{method} is not open to a guest of the attached browser")));
        }
        let conn = if tab == Some(BROWSER_TAB) {
            Self::browser(&mut s).await?
        } else {
            let tid = Self::pick_tab(&mut s, tab).await?;
            self.tab(&mut s, &tid).await?
        };
        drop(s);
        let params = if params.is_null() { json!({}) } else { params };
        Ok(json!({"result": conn.send(method, params, None).await?}))
    }

    /// The running Chrome's own `/json/protocol`. With no session, a throwaway headless probe.
    pub async fn commands(&self, id: Option<&str>) -> R<Value> {
        if let Some(id) = id {
            let base = self.resolve(id).await?.lock().await.base.clone();
            return Ok(json!({"protocol": awm_cdp::protocol(&base).await?}));
        }
        let probe = self.cfg.profiles.join("__probe__").to_string_lossy().to_string();
        let mut h = self.launch("headless", &probe).await?;
        let out = awm_cdp::protocol(&h.base()).await;
        h.stop(&self.docker).await;
        Ok(json!({"protocol": out?}))
    }
}
