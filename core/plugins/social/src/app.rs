//! The plugin's state and every verb (`hub_adapter.py`).
//!
//! The connectors feed [`Social::on_message`], which emits the message and routes it: a seat
//! takes it through [`Seats::route`], else a hand mount's holder gets it pushed, else nobody.
//! A caller below trusted reaches only the chats it holds, plus the chats a forward let it read.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::Path;
use std::sync::{Arc, Mutex, OnceLock, RwLock};
use std::time::Duration;

use awm_plugin_sdk::holders::{self, Holder};
use awm_plugin_sdk::push::{Pusher, Returned};
use awm_plugin_sdk::{Call, CallOpts, Context, Hub, Level, VerbError};
use serde_json::{json, Value};

use crate::config::AccountConfig;
use awm_chat::{chan, msg};
use crate::connectors::{self, ChatKind, Channel, Command, Connector, Inbound, OutFile};
use crate::db::{now, Db, Mount};
use crate::seats::line::{self, Place};
use crate::seats::store::Seat;
use crate::seats::{Routed, Seats};

const SEEN_MAX: usize = 4096;
pub const PROBE_COMMAND: &str = "__awm_probe__";

pub type Conns = Arc<RwLock<BTreeMap<String, Arc<dyn Connector>>>>;

pub struct Social {
    pub here: String,
    pub hub: Hub,
    pub db: Db,
    pub accounts: Vec<AccountConfig>,
    pub conns: Conns,
    pub seats: Arc<Seats>,
    pub ctx: OnceLock<Context>,
    pusher: OnceLock<Pusher>,
    watch: OnceLock<Arc<awm_plugin_sdk::sessions::SessionWatch>>,
    seen: Mutex<HashSet<(String, String, String)>>,
}

fn s(v: &Value, key: &str) -> String {
    match v.get(key) {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(x)) => x.clone(),
        Some(Value::Bool(false)) => String::new(),
        Some(other) => other.to_string(),
    }
}

/// Whether a call may flip a seat's Remote Control: operations on this node, and nobody else.
pub(crate) fn switch_caller(plugin: Option<&str>, from_peer: bool) -> bool {
    plugin == Some("operations") && !from_peer
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::String(x)) => !x.is_empty() && x != "false" && x != "0",
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(_) => true,
    }
}

fn int(v: Option<&Value>, default: i64) -> i64 {
    match v {
        Some(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)).unwrap_or(default),
        Some(Value::String(x)) if !x.is_empty() => x.parse().unwrap_or(default),
        _ => default,
    }
}

fn log_freed(freed: &[Mount]) {
    for m in freed {
        tracing::info!("released seat {}/{}: holder {} is gone", m.account, m.channel_id, holder_label(m));
    }
}

fn holder_label(m: &Mount) -> String {
    let name = if m.session_name.is_empty() { String::new() } else { format!(" ({})", m.session_name) };
    format!("{}@{}{name}", m.session_id, m.node)
}

fn level_of(call: &Call) -> Level {
    let level = call.level();
    let claimed = call.caller_claim().and_then(|c| c.get("trust")).and_then(Value::as_str).and_then(|t| t.parse::<Level>().ok());
    match claimed {
        Some(c) => level.min(c),
        None => level,
    }
}

impl Social {
    pub fn new(here: String, hub: Hub, db: Db, accounts: Vec<AccountConfig>, conns: Conns, seats: Arc<Seats>) -> Arc<Self> {
        let me = Arc::new(Self {
            here,
            hub: hub.clone(),
            db,
            accounts,
            conns,
            seats,
            ctx: OnceLock::new(),
            pusher: OnceLock::new(),
            watch: OnceLock::new(),
            seen: Mutex::new(HashSet::new()),
        });
        let weak = Arc::downgrade(&me);
        let on_return = Arc::new(move |(node, session): (String, String), lines: Vec<String>, why: Returned| {
            let Some(me) = weak.upgrade() else { return };
            tracing::info!("{} line(s) for hand mount {session}@{node} came back ({why:?})", lines.len());
            if why == Returned::Gone {
                tokio::spawn(async move {
                    log_freed(&me.db.release_holder(&node, &session).await);
                });
            }
        });
        let _ = me.pusher.set(Pusher::reflection(hub, on_return));
        me
    }

    fn pusher(&self) -> &Pusher {
        self.pusher.get().expect("the pusher is set in new")
    }

    pub fn account(&self, name: &str) -> Option<&AccountConfig> {
        self.accounts.iter().find(|a| a.name == name)
    }

    pub fn conn(&self, account: &str) -> Result<Arc<dyn Connector>, VerbError> {
        self.conns.read().unwrap().get(account).cloned().ok_or_else(|| VerbError::runtime(format!("account {account:?} is not configured or not connected")))
    }

    fn emit(&self, topic: &str, payload: Value) {
        if let Some(ctx) = self.ctx.get() {
            ctx.emit(topic, payload);
        }
    }

    // -- inbound ------------------------------------------------------------------

    /// One inbound message from a connector.
    pub async fn on_message(self: &Arc<Self>, m: Inbound) {
        if !m.message_id.is_empty() {
            let key = (m.platform.clone(), m.account.clone(), m.message_id.clone());
            let mut seen = self.seen.lock().unwrap();
            if seen.contains(&key) {
                return;
            }
            if seen.len() >= SEEN_MAX {
                seen.clear();
            }
            seen.insert(key);
        }
        if !m.resynced {
            let user = self.db.lookup(&m.platform, &m.sender_id).await.unwrap_or_default();
            self.emit("message", msg(&m, &user));
        }
        if !m.to_holder {
            return;
        }
        let thread = if m.thread_id.is_empty() { &m.message_id } else { &m.thread_id };
        if self.db.is_quiet(&m.account, &m.channel_id, thread).await {
            return;
        }
        if self.seats.route(&m).await != Routed::NotSeat || m.resynced {
            return;
        }
        if let Some(held) = self.db.holder(&m.account, &m.channel_id).await {
            let level = self.account(&m.account).map(|a| a.level_of(&m.channel_id)).unwrap_or(Level::Untrusted);
            let text = line::push_line(&m, level.as_str());
            if self.seats.is_host(&held.node) {
                self.seats.hand(&held.node, &held.session_id, &text).await;
            } else {
                self.pusher().push(&held.node, &held.session_id, &text);
            }
        }
    }

    pub async fn on_command(&self, c: Command) {
        let mut payload = json!({
            "command": c.name, "account": c.account, "platform": c.platform, "options": c.options,
            "device": c.options.get("device").cloned().unwrap_or(Value::Null), "user_id": c.user_id, "user_name": c.user_name,
            "channel_id": c.channel_id, "is_dm": c.is_dm, "operator": c.operator,
        });
        for (k, v) in &c.options {
            payload[k] = v.clone();
        }
        self.emit("command", payload);
    }

    // -- who calls ------------------------------------------------------------------

    /// The calling session as a holder.
    async fn holder(&self, call: &Call) -> Result<Holder, holders::HolderError> {
        holders::caller(call, "a seat").await
    }

    /// The chats of `account` the caller may read and answer, or None for no bound.
    async fn chat_bound(&self, account: &str, who: Option<&Holder>, level: Level) -> Option<HashSet<String>> {
        let Some(cfg) = self.account(account) else { return if level == Level::Trusted { None } else { Some(HashSet::new()) } };
        let held: Vec<Mount> = match who {
            Some(h) => self.db.mounts(Some(account)).await.into_iter().filter(|m| m.node == h.node && m.session_id == h.session).collect(),
            None => vec![],
        };
        let mut chats: HashSet<String> = held.iter().map(|m| m.channel_id.clone()).collect();
        if level == Level::Trusted && (chats.is_empty() || chats.iter().any(|c| self.level_at(cfg, c) == Level::Trusted)) {
            return None;
        }
        if let Some(h) = who {
            let mine: Vec<(String, String)> = self.db.mounts(None).await.into_iter().filter(|m| m.node == h.node && m.session_id == h.session).map(|m| (m.account, m.channel_id)).collect();
            chats.extend(self.seats.store.reads(mine, account, now()).await);
        }
        Some(chats)
    }

    /// A chat's level: its declared seat's, else the account's.
    fn level_at(&self, cfg: &AccountConfig, chat: &str) -> Level {
        self.seats.cfg.chats.iter().find(|c| c.account == cfg.name && c.channel == chat).map(|c| c.level).unwrap_or_else(|| cfg.level_of(chat))
    }

    /// Fill `account`, `channel`, `thread` and `message_id` from the caller's latest line, and
    /// resolve a chat label such as `DM` or `#general` among the chats it holds.
    async fn defaults(&self, call: &Call, who: Option<&Holder>, want_message: bool) -> Result<(String, String, Option<String>, Option<String>), VerbError> {
        let mut account = s(&call.args, "account");
        let mut channel = s(&call.args, "channel");
        let mut thread = Some(s(&call.args, "thread")).filter(|t| !t.is_empty());
        let mut message = Some(s(&call.args, "message_id")).filter(|t| !t.is_empty());
        let line = match who {
            Some(h) => self.seats.store.line(&h.node, &h.session).await,
            None => None,
        };
        if channel.is_empty() {
            let Some((a, c, t, id)) = line.clone() else {
                return Err(VerbError::value(format!("{}: missing required argument(s): {}. No pushed line names a chat for this session.", call.verb, if account.is_empty() { "account, channel" } else { "channel" })));
            };
            if !account.is_empty() && account != a {
                return Err(VerbError::value(format!("{}: channel is required with another account than your latest line's", call.verb)));
            }
            account = a;
            channel = c;
            if thread.is_none() && !t.is_empty() {
                thread = Some(t);
            }
            if want_message && message.is_none() {
                message = Some(id).filter(|m| !m.is_empty());
            }
        } else if let Some(h) = who {
            let held: Vec<Mount> = self.db.mounts(None).await.into_iter().filter(|m| m.node == h.node && m.session_id == h.session && (account.is_empty() || m.account == account)).collect();
            let want = channel.trim_start_matches(['#', '@']).to_lowercase();
            let mut hits: Vec<&Mount> = held.iter().filter(|m| m.channel_id == channel).collect();
            if hits.is_empty() {
                for m in &held {
                    let seat = self.seats.store.get(&m.account, &m.channel_id).await;
                    let is_dm = seat.as_ref().is_some_and(|s| s.kind == "dm");
                    if (channel == "DM" && is_dm) || (!m.channel_name.is_empty() && m.channel_name.trim_start_matches('#').to_lowercase() == want) {
                        hits.push(m);
                    }
                }
            }
            if let [m] = hits.as_slice() {
                account = m.account.clone();
                channel = m.channel_id.clone();
            }
        }
        if account.is_empty() {
            account = line.map(|l| l.0).unwrap_or_default();
        }
        if account.is_empty() {
            return Err(VerbError::value(format!("{}: missing required argument(s): account.", call.verb)));
        }
        if want_message && message.is_none() {
            return Err(VerbError::value(format!("{}: missing required argument(s): message_id.", call.verb)));
        }
        Ok((account, channel, thread, message))
    }

    /// Refuse a caller below trusted on a chat it does not hold.
    async fn held_only(&self, call: &Call, who: &Result<Holder, holders::HolderError>, account: &str, channel: &str) -> Result<(), VerbError> {
        let level = level_of(call);
        if level == Level::Trusted {
            return Ok(());
        }
        let h = match who {
            Ok(h) => h,
            Err(e) => return Err(VerbError::permission(format!("a {} session answers only the chats it holds: {e}", level.as_str()))),
        };
        let held = self.chat_bound(account, Some(h), level).await.unwrap_or_default();
        if !held.contains(channel) {
            return Err(VerbError::permission(format!("a {} session answers only the chats it holds, and it does not hold {channel:?}", level.as_str())));
        }
        Ok(())
    }

    // -- the chat verbs -------------------------------------------------------------

    pub async fn send(&self, call: Call) -> Result<Value, VerbError> {
        let who = self.holder(&call).await;
        let (account, channel, thread, _) = self.defaults(&call, who.as_ref().ok(), false).await?;
        self.held_only(&call, &who, &account, &channel).await?;
        let conn = self.conn(&account)?;
        let mut files: Vec<OutFile> = match call.args.get("files") {
            Some(Value::Array(a)) => a.iter().map(OutFile::from_value).collect::<Result<_, _>>()?,
            _ => vec![],
        };
        let path = s(&call.args, "file");
        if !path.is_empty() {
            let p = Path::new(&path);
            if p.is_dir() {
                return Err(VerbError::value(format!("send attaches one file, and {path:?} is a folder")));
            }
            if !p.is_file() {
                return Err(VerbError::new("FileNotFoundError", format!("send: no local file {path:?}")));
            }
            files.push(OutFile { filename: p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default(), body: connectors::FileBody::Path(p.into()) });
        }
        let text = s(&call.args, "text");
        if text.is_empty() && files.is_empty() {
            return Err(VerbError::value("send needs text, a file, or both"));
        }
        check_files(&*conn, &account, &files)?;
        let sent = conn.send(&channel, &text, thread.as_deref(), &files).await?;
        if truthy(call.args.get("quiet")) {
            let ts = [s(&Value::Object(sent.clone()), "ts"), s(&Value::Object(sent.clone()), "message_id")].into_iter().find(|t| !t.is_empty());
            if let Some(ts) = ts {
                let ch = Some(s(&Value::Object(sent.clone()), "channel_id")).filter(|c| !c.is_empty()).unwrap_or_else(|| channel.clone());
                self.db.quiet_thread(&account, &ch, &ts).await;
            }
        }
        let mut out = serde_json::Map::from_iter([("ok".to_string(), json!(true))]);
        out.extend(sent);
        Ok(Value::Object(out))
    }

    pub async fn react(&self, call: Call) -> Result<Value, VerbError> {
        let who = self.holder(&call).await;
        let (account, channel, _, message) = self.defaults(&call, who.as_ref().ok(), true).await?;
        self.held_only(&call, &who, &account, &channel).await?;
        self.react_to(&account, &channel, &message.unwrap_or_default(), &call.args).await
    }

    async fn react_to(&self, account: &str, channel: &str, message: &str, args: &Value) -> Result<Value, VerbError> {
        let done = self.conn(account)?.react(channel, message, &s(args, "emoji"), truthy(args.get("remove"))).await?;
        let mut out = serde_json::Map::from_iter([("ok".to_string(), json!(true))]);
        if let Value::Object(o) = done {
            out.extend(o);
        }
        Ok(Value::Object(out))
    }

    pub async fn fetch(&self, call: Call) -> Result<Value, VerbError> {
        let who = self.holder(&call).await;
        let (account, channel) = (s(&call.args, "account"), s(&call.args, "channel"));
        self.held_only(&call, &who, &account, &channel).await?;
        let conn = self.conn(&account)?;
        let before = Some(s(&call.args, "before")).filter(|b| !b.is_empty());
        let msgs = conn.fetch(&channel, int(call.args.get("limit"), 50) as usize, before.as_deref()).await?;
        self.rows(msgs).await
    }

    async fn rows(&self, msgs: Vec<Inbound>) -> Result<Value, VerbError> {
        let mut rows = vec![];
        for m in &msgs {
            rows.push(msg(m, &self.db.lookup(&m.platform, &m.sender_id).await.unwrap_or_default()));
        }
        Ok(json!({"count": rows.len(), "messages": rows}))
    }

    pub async fn history(&self, call: Call) -> Result<Value, VerbError> {
        let account = s(&call.args, "account");
        let conn = self.conn(&account)?;
        if !conn.keeps_history() {
            return Err(VerbError::runtime(format!("account {account:?} keeps no history; use fetch")));
        }
        let who = self.holder(&call).await.ok();
        let allowed = self.chat_bound(&account, who.as_ref(), level_of(&call)).await;
        let channel = Some(s(&call.args, "channel")).filter(|c| !c.is_empty());
        if let (Some(a), Some(c)) = (&allowed, &channel) {
            if !a.contains(c) {
                return Err(VerbError::permission(format!("chat {c:?} is not a seat this session holds")));
            }
        }
        let chats: Vec<Option<String>> = match (&channel, &allowed) {
            (Some(c), _) => vec![Some(c.clone())],
            (None, Some(a)) => {
                let mut v: Vec<_> = a.iter().cloned().map(Some).collect();
                v.sort();
                v
            }
            (None, None) => vec![None],
        };
        let since = Some(s(&call.args, "since")).filter(|x| !x.is_empty());
        let limit = int(call.args.get("limit"), 100);
        let mut msgs = vec![];
        for c in &chats {
            msgs.extend(self.db.history(&account, c.as_deref(), since.as_deref(), limit, truthy(call.args.get("include_deleted"))).await.map_err(|e| VerbError::runtime(e.to_string()))?);
        }
        if chats.len() > 1 {
            let ts = |m: &Value| m["ts"].as_str().and_then(|t| t.parse::<f64>().ok()).unwrap_or(0.0);
            msgs.sort_by(|a, b| ts(a).total_cmp(&ts(b)));
            let n = limit.max(0) as usize;
            if since.is_some() {
                msgs.truncate(n);
            } else if msgs.len() > n {
                msgs = msgs.split_off(msgs.len() - n);
            }
        }
        let mut synced = self.db.synced_channels(&account).await.map_err(|e| VerbError::runtime(e.to_string()))?;
        if let Some(a) = &allowed {
            synced.retain(|c| a.contains(c["channel_id"].as_str().unwrap_or("")));
        }
        Ok(json!({"messages": msgs, "count": msgs.len(), "chats": synced}))
    }

    pub async fn download_attachments(&self, call: Call) -> Result<Value, VerbError> {
        let who = self.holder(&call).await;
        let (account, channel) = (s(&call.args, "account"), s(&call.args, "channel"));
        self.held_only(&call, &who, &account, &channel).await?;
        self.inline_files(&account, &channel, &s(&call.args, "message_id"), idx(&call.args)).await
    }

    /// A message's attachments as `b64` files, which the caller's gateway writes to disk.
    async fn inline_files(&self, account: &str, channel: &str, message: &str, idx: Option<i64>) -> Result<Value, VerbError> {
        awm_chat::inline_files(&self.conn(account)?.download_attachments(channel, message, idx).await?, &self.here)
    }

    pub async fn accounts(&self, _call: Call) -> Result<Value, VerbError> {
        let live = self.conns.read().unwrap().iter().map(|(n, c)| (n.clone(), c.live())).collect::<HashMap<_, _>>();
        let out: Vec<Value> = self
            .accounts
            .iter()
            .map(|a| {
                json!({"name": a.name, "platform": a.platform, "kind": "bot", "display_name": a.display_name.clone().unwrap_or_default(),
                    "enabled": a.enabled, "live": live.get(&a.name).copied().unwrap_or(false), "singleton": a.singleton, "owner": "local"})
            })
            .collect();
        Ok(json!({"accounts": out}))
    }

    pub async fn channels(&self, call: Call) -> Result<Value, VerbError> {
        let c = self.conn(&s(&call.args, "account"))?.list_channels(truthy(call.args.get("include_dms"))).await?;
        Ok(json!({"channels": c.iter().map(chan).collect::<Vec<_>>()}))
    }

    pub async fn open_dm(&self, call: Call) -> Result<Value, VerbError> {
        let c = self.conn(&s(&call.args, "account"))?.open_dm(&s(&call.args, "user")).await?;
        Ok(json!({"channel": chan(&c)}))
    }

    // -- seats and mounts -----------------------------------------------------------

    async fn find_seat(&self, conn: &Arc<dyn Connector>, channel: &str) -> Result<Channel, String> {
        let r = channel.trim();
        let name = r.trim_start_matches(['#', '@']).to_lowercase();
        let chats = conn.list_seats().await.map_err(|e| e.message)?;
        let mut hits: Vec<&Channel> = chats.iter().filter(|c| c.id == r).collect();
        if hits.is_empty() {
            hits = chats.iter().filter(|c| !c.name.is_empty() && (c.name.to_lowercase() == r.to_lowercase() || c.name.to_lowercase() == name)).collect();
        }
        match hits.len() {
            1 => return Ok(hits[0].clone()),
            0 => {}
            n => return Err(format!("{r:?} names {n} seats: {}. Pass the id.", hits.iter().map(|c| format!("{} ({})", c.name, c.id)).collect::<Vec<_>>().join(", "))),
        }
        let dms = conn.list_channels(true).await.unwrap_or_default();
        if r.starts_with("dm:") || dms.iter().any(|c| c.kind == "dm" && (c.id == r || c.name.to_lowercase() == name)) {
            return Err(format!("{} DMs are reserved for notifications and cannot be mounted, except an operator's DM, which `seats` lists", conn.platform()));
        }
        Err(format!("account {:?} is not in {r:?}. `seats` lists the chats it is in; invite the bot to add one", conn.account().name))
    }

    pub fn place(&self, account: &str, c: &Channel) -> Place {
        let kind = match c.kind.as_str() {
            "dm" | "im" => ChatKind::Dm,
            "group" | "mpim" => ChatKind::Group,
            _ => ChatKind::Channel,
        };
        let platform = self.account(account).map(|a| a.platform.clone()).unwrap_or_default();
        if kind == ChatKind::Dm {
            return Place { platform, kind, person: c.name.clone(), ..Default::default() };
        }
        // A Discord guild channel is listed as `<guild>#<channel>`.
        let (server, chat) = c.name.split_once('#').unwrap_or(("", &c.name));
        Place { platform, server: server.into(), kind, chat_name: chat.into(), person: String::new() }
    }

    pub async fn seats_verb(&self, call: Call) -> Result<Value, VerbError> {
        let account = s(&call.args, "account");
        let conn = self.conn(&account)?;
        let held: HashMap<String, Mount> = self.db.mounts(Some(&account)).await.into_iter().map(|m| (m.channel_id.clone(), m)).collect();
        let mut stored: BTreeMap<String, Seat> = self.seats.store.all().await.into_iter().filter(|st| st.account == account).map(|st| (st.channel_id.clone(), st)).collect();
        let mut out = vec![];
        let mut add = |c: &Channel, seat: Option<Seat>| {
            let mut row = chan(c);
            row["holder"] = held.get(&c.id).map_or(Value::Null, Mount::holder);
            row["seat"] = seat.as_ref().map_or(Value::Null, Seat::public);
            if let Some(st) = seat {
                row["label"] = json!(self.seats.name(&st));
            }
            out.push(row);
        };
        for c in conn.list_seats().await? {
            let seat = stored.remove(&c.id);
            add(&c, seat);
        }
        // A declared seat whose chat the platform has not listed yet, such as a guild channel before the gateway is ready.
        for st in stored.into_values() {
            add(&Channel { id: st.channel_id.clone(), name: st.channel_name.clone(), kind: st.kind.clone() }, Some(st));
        }
        Ok(json!({"seats": out}))
    }

    /// Why a caller below trusted may not mount this seat, or None.
    async fn mount_refusal(&self, level: Level, account: &str, channel: &str, who: &Holder, evicted: Option<&Mount>) -> Option<String> {
        if level == Level::Trusted {
            return None;
        }
        let cfg = self.account(account)?;
        if self.level_at(cfg, channel) == Level::Trusted {
            return Some(format!("a {} session cannot hold a trusted chat", level.as_str()));
        }
        let evicted = evicted?;
        let parent = match self.parent_session(&who.node, &who.session).await {
            Ok(p) => p,
            Err(e) => return Some(format!("cannot read this session's parent to take the seat over: {e}")),
        };
        if (evicted.node.as_str(), evicted.session_id.as_str()) != (who.node.as_str(), parent.as_deref().unwrap_or("")) {
            return Some(format!("a {} session takes a held seat only from the session that started it, not from {}", level.as_str(), holder_label(evicted)));
        }
        None
    }

    async fn parent_session(&self, node: &str, session: &str) -> Result<Option<String>, String> {
        let peer = (node != self.here).then(|| node.to_string());
        let opts = CallOpts { peer, timeout: Duration::from_secs(30), ..CallOpts::default() };
        let out = self.hub.call_with("reflection", "list_other", json!({}), &opts).await.map_err(|e| e.to_string())?;
        for row in out.get("sessions").and_then(Value::as_array).into_iter().flatten() {
            if s(row, "session_id") == session {
                return Ok(Some(s(row, "parent")).filter(|p| !p.is_empty()));
            }
        }
        Err(format!("reflection on {node} does not list session {session}"))
    }

    pub async fn mount(&self, call: Call) -> Result<Value, VerbError> {
        let account = s(&call.args, "account");
        let conn = self.conn(&account)?;
        let who = match self.holder(&call).await {
            Ok(w) => w,
            Err(e) => return Ok(json!({"ok": false, "error": e.0})),
        };
        let seat = match self.find_seat(&conn, &s(&call.args, "channel")).await {
            Ok(c) => c,
            Err(e) => return Ok(json!({"ok": false, "error": e})),
        };
        let label = if seat.name.is_empty() { seat.id.clone() } else { seat.name.clone() };
        let place = self.place(&account, &seat);
        if let Some(st) = self.seats.declared(&account, &seat.id, &place).await {
            return Ok(json!({"ok": false, "error": format!("{label} is a seat on {}: run `seats adopt account={account} channel={} session_id=<session id>` there", st.node, seat.id)}));
        }
        let prev = self.db.holder(&account, &seat.id).await;
        let evicted = prev.filter(|p| (p.node.as_str(), p.session_id.as_str()) != (who.node.as_str(), who.session.as_str()));
        if let Some(e) = &evicted {
            if !truthy(call.args.get("force")) {
                return Ok(json!({"ok": false, "error": format!("{label} is held by {} since {}. Pass force=true to take it.", holder_label(e), e.mounted_at), "holder": e.holder()}));
            }
        }
        if let Some(why) = self.mount_refusal(level_of(&call), &account, &seat.id, &who, evicted.as_ref()).await {
            return Ok(json!({"ok": false, "error": format!("{label}: {why}")}));
        }
        let row = self.db.mount(Mount { account: account.clone(), channel_id: seat.id.clone(), channel_name: seat.name.clone(), node: who.node.clone(), session_id: who.session.clone(), session_name: who.name.clone(), mounted_at: String::new() }).await.map_err(|e| VerbError::runtime(e.to_string()))?;
        self.watch_node(&who.node);
        if let Some(e) = &evicted {
            self.pusher().push(&e.node, &e.session_id, &format!("{} seat lost: {label} is now held by {}. Nothing more from it reaches you.", conn.platform(), holder_label(&row)));
        }
        let reply = format!(
            "social send text=<text> answers the latest line in its thread; name this chat with channel={} when you hold more than one. Before work that takes a while, react \u{1f440} with `social react emoji=\u{1f440}`, then post the answer with `social send`. Share a long document, such as a plan, with file=<path> instead of pasting it",
            seat.id
        );
        Ok(json!({
            "ok": true,
            "seat": {"account": account, "id": seat.id, "name": seat.name, "kind": seat.kind, "holder": row.holder()},
            "evicted": evicted.as_ref().map(Mount::holder),
            "rename": format!("when this is the only seat this session holds, rename the session to {:?} with reflection rename", place.session_name()),
            "push": "each new message here is typed into your prompt as '[<server>/<chat>/<sender>, <level>] \u{21b3}<thread> <text>'",
            "reply": reply,
            "backlog": if conn.keeps_history() { format!("social history account={account} channel={}", seat.id) } else { format!("social fetch account={account} channel={}", seat.id) },
            "lasts": "until unmount, a forced mount by another session, or this session ending",
        }))
    }

    pub async fn unmount(&self, call: Call) -> Result<Value, VerbError> {
        let account = s(&call.args, "account");
        self.conn(&account)?;
        let who = match self.holder(&call).await {
            Ok(w) => w,
            Err(e) => return Ok(json!({"ok": false, "error": e.0})),
        };
        let mut rows = self.db.mounts(Some(&account)).await;
        let r = s(&call.args, "channel").trim().to_string();
        if !r.is_empty() {
            let name = r.trim_start_matches(['#', '@']).to_lowercase();
            rows.retain(|m| m.channel_id == r || [r.to_lowercase(), name.clone()].contains(&m.channel_name.to_lowercase()));
            if rows.is_empty() {
                return Ok(json!({"ok": false, "error": format!("{r:?} is not mounted")}));
            }
            if let Some(o) = rows.iter().find(|m| (m.node.as_str(), m.session_id.as_str()) != (who.node.as_str(), who.session.as_str())) {
                return Ok(json!({"ok": false, "error": format!("{r:?} is held by {}, not by you", holder_label(o))}));
            }
        } else {
            rows.retain(|m| m.node == who.node && m.session_id == who.session);
        }
        for m in &rows {
            self.db.unmount(&account, &m.channel_id).await;
            if let Some(st) = self.seats.store.get(&account, &m.channel_id).await {
                self.seats.released(&st, &m.session_id).await;
            }
        }
        Ok(json!({"ok": true, "released": rows.iter().map(|m| json!({"id": m.channel_id, "name": m.channel_name})).collect::<Vec<_>>()}))
    }

    pub async fn forward(&self, call: Call) -> Result<Value, VerbError> {
        let who = self.holder(&call).await.map_err(VerbError::from)?;
        let Some((src_account, src_channel, _, last_id)) = self.seats.store.line(&who.node, &who.session).await else {
            return Err(VerbError::value("forward: no pushed line names a chat for this session"));
        };
        let message_id = Some(s(&call.args, "message_id")).filter(|m| !m.is_empty()).unwrap_or(last_id);
        let level = level_of(&call);
        self.held_only(&call, &Ok(who.clone()), &src_account, &src_channel).await?;
        let account = Some(s(&call.args, "account")).filter(|a| !a.is_empty()).unwrap_or_else(|| src_account.clone());
        let target_ref = s(&call.args, "channel");
        let conn = self.conn(&account)?;
        let chat = self.find_seat(&conn, &target_ref).await.map_err(VerbError::value)?;
        let target = match self.seats.store.get(&account, &chat.id).await {
            Some(t) => t,
            None => {
                let place = self.place(&account, &chat);
                if place.kind == ChatKind::Dm {
                    return Err(VerbError::value(format!("{target_ref:?} is a DM nobody has opened: a forward starts no first contact")));
                }
                self.seats.declared(&account, &chat.id, &place).await.ok_or_else(|| VerbError::value(format!("{target_ref:?} is no seat: [seats] in social.toml declares none there")))?
            }
        };
        if level < Level::Trusted && target.level == "trusted" {
            return Err(VerbError::permission(format!("a {} session cannot forward into a trusted chat", level.as_str())));
        }
        let original = self.seats.store.line_of(&src_account, &src_channel, &message_id).await.unwrap_or_else(|| format!("message {message_id}"));
        let note = s(&call.args, "note");
        let line = format!("[forwarded by the agent of {src_channel}] {original}{}", if note.is_empty() { String::new() } else { format!(" \u{2014} {note}") });
        self.seats.forward(&target, (&src_account, &src_channel), &line).await;
        Ok(json!({"ok": true, "to": {"account": account, "id": chat.id, "name": chat.name, "state": target.state}, "reads": {"account": src_account, "channel": src_channel, "hours": 24}}))
    }

    /// The seat an Ops switch names, for operations on this node only.
    async fn ops_seat(&self, call: &Call) -> Result<crate::seats::store::Seat, VerbError> {
        if !switch_caller(call.plugin.as_deref(), call.from_peer()) {
            return Err(VerbError::permission(format!("{}: only operations on this node runs this", call.verb)));
        }
        let seat = s(&call.args, "seat");
        let (account, channel) = seat.split_once('/').filter(|(a, c)| !a.is_empty() && !c.is_empty()).ok_or_else(|| VerbError::value(format!("seat {seat:?} is not <account>/<channel_id>")))?;
        self.seats.store.get(account, channel).await.ok_or_else(|| VerbError::value(format!("{seat} is no seat social has seen; `seats` lists them")))
    }

    /// The Remote Control switch on Ops: operations on this node is the one caller.
    pub async fn seat_remote_control(&self, call: Call) -> Result<Value, VerbError> {
        let st = self.ops_seat(&call).await?;
        if call.args.get("on").is_none_or(Value::is_null) {
            return Err(VerbError::value("seat_remote_control: missing required argument(s): on."));
        }
        let on = truthy(call.args.get("on"));
        self.seats.set_remote_control(&st, on).await;
        Ok(json!({"ok": true, "seat": st.id(), "remote_control": on, "live": "pending"}))
    }

    /// The Refresh button on Ops: operations on this node is the one caller.
    pub async fn seat_refresh(&self, call: Call) -> Result<Value, VerbError> {
        let st = self.ops_seat(&call).await?;
        let (model, effort) = (s(&call.args, "model"), s(&call.args, "effort"));
        if model.is_empty() {
            return Err(VerbError::value("seat_refresh: missing required argument(s): model."));
        }
        awm_trust::trust::check_effort(&effort).map_err(|e| VerbError::value(format!("seat_refresh: {e}")))?;
        self.seats.set_refresh(&st, &model, &effort).await;
        Ok(json!({"ok": true, "seat": st.id(), "model": model, "effort": effort, "live": "pending"}))
    }

    // -- the seat hosts ---------------------------------------------------------------

    /// The node a seat host's call speaks for: the door's stamp, and no session's word.
    fn seat_host(&self, call: &Call) -> Result<String, VerbError> {
        if call.caller_claim().is_some() || call.caller_session_id().is_some() {
            return Err(VerbError::permission(format!("{}: only a node's seats plugin calls this", call.verb)));
        }
        Ok(call.peer_node().unwrap_or(&self.here).to_string())
    }

    pub async fn seat_pull(&self, call: Call) -> Result<Value, VerbError> {
        let node = self.seat_host(&call)?;
        Ok(self.seats.pull(&node, int(call.args.get("ack_through"), 0), call.args.get("wait_s").and_then(Value::as_f64).unwrap_or(0.0)).await)
    }

    /// One row of a host's outbox, applied once however often it is sent.
    pub async fn seat_report(&self, call: Call) -> Result<Value, VerbError> {
        let node = self.seat_host(&call)?;
        let (epoch, id, kind) = (s(&call.args, "epoch"), int(call.args.get("id"), 0), s(&call.args, "kind"));
        if !self.seats.store.fresh(&node, &epoch, id).await {
            return Ok(json!({"ok": true, "duplicate": true}));
        }
        let body = call.args.get("body").cloned().unwrap_or(Value::Null);
        let out = self.apply_report(&node, &kind, &body).await;
        if out.get("retry") != Some(&json!(true)) {
            self.seats.store.applied(&node, &epoch, id).await;
        }
        Ok(out)
    }

    /// A seat agent's reaction or download, answered directly because the agent waits on it.
    pub async fn seat_act(&self, call: Call) -> Result<Value, VerbError> {
        let node = self.seat_host(&call)?;
        let (account, channel, message) = (s(&call.args, "account"), s(&call.args, "channel"), s(&call.args, "message_id"));
        if !self.answers(&node, &account, &channel).await {
            return Err(VerbError::permission(format!("{node} does not answer {account}/{channel}")));
        }
        match s(&call.args, "action").as_str() {
            "react" => self.react_to(&account, &channel, &message, &call.args).await,
            "download" => self.inline_files(&account, &channel, &message, idx(&call.args)).await,
            other => Err(VerbError::value(format!("seat_act: action is react or download, not {other:?}"))),
        }
    }

    /// Whether `node` hosts this chat's seat or holds it by hand.
    async fn answers(&self, node: &str, account: &str, channel: &str) -> bool {
        self.seats.store.get(account, channel).await.is_some_and(|st| st.node == node) || self.db.holder(account, channel).await.is_some_and(|m| m.node == node)
    }

    async fn apply_report(&self, node: &str, kind: &str, b: &Value) -> Value {
        let ok = json!({"ok": true});
        let (account, channel) = (s(b, "account"), Some(s(b, "channel")).filter(|c| !c.is_empty()).unwrap_or_else(|| s(b, "channel_id")));
        if matches!(kind, "seat" | "send" | "react" | "mount" | "unmount" | "line") && !self.answers(node, &account, &channel).await {
            return json!({"ok": false, "error": format!("{node} does not answer {account}/{channel}")});
        }
        match kind {
            "seat" => {
                // An answer to a flip or a refresh goes to Ops even when the seat did not change.
                let changed = self.seats.store.mirror(&account, &channel, &s(b, "state"), &s(b, "session_id"), truthy(b.get("remote_control")), &s(b, "rc_url"), b["public"].clone()).await;
                if changed || b["event"].get("rc_live").is_some() || b["event"].get("refresh_live").is_some() {
                    self.emit("seats", b["event"].clone());
                }
                ok
            }
            "send" => {
                self.seat_send(&account, &channel, b).await
            }
            "react" => {
                if let Ok(conn) = self.conn(&account) {
                    if let Err(e) = conn.react(&channel, &s(b, "message_id"), &s(b, "emoji"), false).await {
                        tracing::info!("seat react on {account}/{channel} failed: {}", e.message);
                    }
                }
                ok
            }
            "notice" => {
                match self.seats.cfg.notice_for(&self.accounts) {
                    Some(n) => match self.conn(&n.account) {
                        Ok(conn) => {
                            if let Err(e) = conn.send(&n.channel, &format!("{}{}", crate::seats::NOTICE, s(b, "text")), None, &[]).await {
                                tracing::warn!("a seat notice did not go out: {}", e.message);
                            }
                        }
                        Err(e) => tracing::warn!("a seat notice did not go out: {}", e.message),
                    },
                    None => tracing::warn!("no notice chat for: {}", s(b, "text")),
                }
                ok
            }
            "ask" => {
                let ask_kind = s(b, "kind");
                let args = if ask_kind == "review" { json!({"kind": ask_kind, "summary": s(b, "text")}) } else { json!({"kind": ask_kind, "reason": s(b, "text")}) };
                if let Err(e) = self.hub.call("operations", "request_human", args, None).await {
                    tracing::warn!("request_human ({ask_kind}) failed: {e}");
                }
                ok
            }
            "tag" => {
                let _ = self.hub.call("journal", "tag_add", json!({"name": s(b, "name"), "description": s(b, "description")}), None).await;
                ok
            }
            "mount" => {
                let m = Mount { account, channel_id: channel, channel_name: s(b, "channel_name"), node: node.to_string(), session_id: s(b, "session_id"), session_name: s(b, "session_name"), mounted_at: String::new() };
                if let Err(e) = self.db.mount(m).await {
                    tracing::warn!("seat mount failed: {e}");
                }
                ok
            }
            "unmount" => {
                let session = s(b, "session_id");
                if self.db.holder(&account, &channel).await.is_some_and(|m| m.node == node && m.session_id == session) {
                    self.db.unmount(&account, &channel).await;
                }
                ok
            }
            "line" => {
                self.seats.store.set_line(node, &s(b, "session_id"), &account, &channel, &s(b, "thread_id"), &s(b, "message_id"), now()).await;
                ok
            }
            "live" => {
                let sessions: HashSet<&str> = b["session_ids"].as_array().into_iter().flatten().filter_map(Value::as_str).collect();
                let held: BTreeSet<String> = self.db.mounts(None).await.into_iter().filter(|m| m.node == node && !sessions.contains(m.session_id.as_str())).map(|m| m.session_id).collect();
                for session in held {
                    log_freed(&self.db.release_holder(node, &session).await);
                }
                ok
            }
            other => {
                tracing::warn!("{node} reported a row of unknown kind {other:?}");
                ok
            }
        }
    }

    /// A seat agent's reply. Only a refusal that a resend cannot change drops it.
    async fn seat_send(&self, account: &str, channel: &str, b: &Value) -> Value {
        let conn = match self.conn(account) {
            Ok(c) => c,
            Err(e) => return json!({"ok": false, "error": e.message, "retry": true}),
        };
        let files: Vec<OutFile> = match b.get("files") {
            Some(Value::Array(a)) => match a.iter().map(OutFile::from_value).collect() {
                Ok(f) => f,
                Err(e) => return json!({"ok": false, "error": VerbError::from(e).message}),
            },
            _ => vec![],
        };
        if let Err(e) = check_files(&*conn, account, &files) {
            return json!({"ok": false, "error": e.message, "error_class": e.error_class, "retry": false});
        }
        let thread = Some(s(b, "thread")).filter(|t| !t.is_empty());
        match conn.send(channel, &s(b, "text"), thread.as_deref(), &files).await {
            Ok(sent) => {
                let mut out = serde_json::Map::from_iter([("ok".to_string(), json!(true))]);
                out.extend(sent);
                Value::Object(out)
            }
            Err(e) => {
                // A platform's 4xx other than a rate limit refuses the message itself: a resend gets the same answer.
                let client_error = e.error_class == "HTTPException" && e.message.starts_with('4') && !e.message.starts_with("429");
                let final_refusal = client_error || ["ValueError", "PermissionError", "FirstContactRefused", "FileNotFoundError", "NotImplementedError", "Forbidden", "NotFound"].contains(&e.error_class.as_str());
                json!({"ok": false, "error": e.message, "error_class": e.error_class, "retry": !final_refusal})
            }
        }
    }

    // -- operators -------------------------------------------------------------------

    pub async fn list_operators(&self, call: Call) -> Result<Value, VerbError> {
        let p = Some(s(&call.args, "platform")).filter(|p| !p.is_empty());
        Ok(json!({"operators": self.db.list_operators(p).await.map_err(|e| VerbError::runtime(e.to_string()))?}))
    }

    pub async fn add_operator(&self, call: Call) -> Result<Value, VerbError> {
        let (p, u, a) = (s(&call.args, "platform"), s(&call.args, "platform_user_id"), s(&call.args, "awm_user"));
        for (k, v) in [("platform", &p), ("platform_user_id", &u), ("awm_user", &a)] {
            if v.trim().is_empty() {
                return Err(VerbError::value(format!("{k} is required")));
            }
        }
        self.db.add_operator(&p, &u, &a).await.map_err(|e| VerbError::runtime(e.to_string()))
    }

    pub async fn remove_operator(&self, call: Call) -> Result<Value, VerbError> {
        Ok(json!({"removed": self.db.remove_operator(&s(&call.args, "platform"), &s(&call.args, "platform_user_id")).await.map_err(|e| VerbError::runtime(e.to_string()))?}))
    }

    pub async fn lookup(&self, call: Call) -> Result<Value, VerbError> {
        Ok(json!({"awm_user": self.db.lookup(&s(&call.args, "platform"), &s(&call.args, "platform_user_id")).await}))
    }

    pub async fn emit_probe(&self, call: Call) -> Result<Value, VerbError> {
        if self.ctx.get().is_none() {
            return Ok(json!({"ok": false, "error": "social adapter not started"}));
        }
        let nonce = s(&call.args, "nonce");
        if nonce.is_empty() {
            return Ok(json!({"ok": false, "error": "nonce is required"}));
        }
        self.emit("command", json!({"command": PROBE_COMMAND, "nonce": nonce}));
        Ok(json!({"ok": true, "nonce": nonce}))
    }

    // -- background ------------------------------------------------------------------

    /// Release every hand mount whose holder's session is gone. A seat host reports its own.
    pub async fn prune(&self) -> Vec<Mount> {
        let mut by_node: BTreeMap<String, HashSet<String>> = BTreeMap::new();
        for m in self.db.mounts(None).await {
            if self.seats.is_host(&m.node) {
                continue;
            }
            by_node.entry(m.node).or_default().insert(m.session_id);
        }
        let mut freed = vec![];
        for (node, sessions) in by_node {
            let Some(live) = awm_plugin_sdk::push::live_rows(&self.hub, &node).await.map(|rows| rows.into_keys().collect::<HashSet<_>>()) else { continue };
            for session in sessions.difference(&live) {
                freed.extend(self.db.release_holder(&node, session).await);
            }
        }
        freed
    }

    /// Watch `node`'s sessions, so a hand mount there is pruned. A seat host reports its own.
    pub fn watch_node(&self, node: &str) {
        if let (Some(w), false) = (self.watch.get(), self.seats.is_host(node)) {
            w.ensure(node);
        }
    }

    /// Start watching reflection on this node, and on every node a hand mount lives on.
    pub async fn watch_sessions(self: &Arc<Self>) {
        let me = Arc::downgrade(self);
        let watch = awm_plugin_sdk::sessions::SessionWatch::new(self.hub.clone(), "social", move |_node| {
            let me = me.clone();
            async move {
                let Some(me) = me.upgrade() else { return };
                log_freed(&me.prune().await);
            }
        });
        let _ = self.watch.set(watch);
        let mut nodes: BTreeSet<String> = BTreeSet::from([self.here.clone()]);
        nodes.extend(self.db.mounts(None).await.into_iter().map(|m| m.node));
        for n in nodes {
            self.watch_node(&n);
        }
    }
}

/// The optional attachment index of a download.
/// Refuses files the account cannot take in one message.
fn check_files(conn: &dyn Connector, account: &str, files: &[OutFile]) -> Result<(), VerbError> {
    if files.is_empty() {
        return Ok(());
    }
    if !conn.sends_files() {
        return Err(VerbError::new("NotImplementedError", format!("{} account {account:?} does not take files through social", conn.platform())));
    }
    let size: u64 = files.iter().map(OutFile::size).sum();
    if size > conn.max_file_bytes() {
        return Err(VerbError::value(format!("the files come to {size} bytes, over the {} that {account:?} takes per message", conn.max_file_bytes())));
    }
    Ok(())
}

fn idx(args: &Value) -> Option<i64> {
    match args.get("idx") {
        None | Some(Value::Null) => None,
        Some(Value::String(x)) if x.is_empty() => None,
        v => Some(int(v, 0)),
    }
}
