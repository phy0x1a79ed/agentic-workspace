//! The seats plugin: the chat seats this node hosts.

mod brief;
mod engine;
mod link;
mod store;
#[cfg(test)]
mod tests;
mod world;

use std::sync::Arc;

use awm_plugin_sdk::holders;
use awm_plugin_sdk::push::Pusher;
use awm_plugin_sdk::{Call, Level, Service, VerbError};
use serde_json::{json, Value};

use engine::{Seats, Settings};
use link::Link;
use store::Seat;

/// Discord's cap on one message's content, counted in chars as twilight counts them.
const DISCORD_MAX: usize = 2000;

pub(crate) fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

pub(crate) fn now_iso() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false)
}

fn s(v: &Value, key: &str) -> String {
    v.get(key).and_then(Value::as_str).unwrap_or("").to_string()
}

struct App {
    seats: Arc<Seats>,
    link: Arc<Link>,
    _watch: Arc<awm_plugin_sdk::sessions::SessionWatch>,
}

fn param(name: &str, kind: &str, required: bool) -> Value {
    json!({"name": name, "type": kind, "required": required})
}

/// The chat `session` answers: the one `channel` names among its seats, else its latest line's.
fn pick<'a>(seats: &'a [Seat], channel: &str, latest: Option<&store::Latest>) -> Result<&'a Seat, VerbError> {
    if channel.is_empty() {
        if let Some(l) = latest {
            if let Some(hit) = seats.iter().find(|s| s.account() == l.account && s.channel() == l.channel) {
                return Ok(hit);
            }
        }
        return match seats {
            [only] => Ok(only),
            _ => Err(VerbError::value("seats: this session holds several seats and no line names one. Pass channel, an id or a label such as `DM` or `#general`.")),
        };
    }
    let want = channel.trim_start_matches(['#', '@']).to_lowercase();
    let mut hits = seats.iter().filter(|s| s.channel() == channel || (channel == "DM" && s.decl.kind == "dm") || (!s.decl.channel_name.is_empty() && s.decl.channel_name.trim_start_matches('#').to_lowercase() == want));
    match (hits.next(), hits.next()) {
        (Some(hit), None) => Ok(hit),
        (None, _) => Err(VerbError::value(format!("seats: this session holds no seat {channel:?}. Use `social send` for any other chat."))),
        _ => Err(VerbError::value(format!("seats: {channel:?} names several seats. Pass the id."))),
    }
}

impl App {
    async fn agent_seats(&self, call: &Call) -> Result<(String, Vec<Seat>), VerbError> {
        let who = holders::caller(call, "a seat").await?;
        let seats = self.seats.store.of_session(&who.session).await;
        if seats.is_empty() {
            return Err(VerbError::new("HolderError", "this session is no seat's agent on this node"));
        }
        Ok((who.session, seats))
    }

    async fn send(&self, call: Call) -> Result<Value, VerbError> {
        let (session, seats) = self.agent_seats(&call).await?;
        let latest = self.seats.store.line(&session).await;
        let seat = pick(&seats, &s(&call.args, "channel"), latest.as_ref())?;
        let text = s(&call.args, "text");
        let files = call.args.get("files").filter(|f| f.as_array().is_some_and(|a| !a.is_empty())).cloned();
        if text.is_empty() && files.is_none() {
            return Err(VerbError::value("seats send needs text, a file, or both"));
        }
        let len = text.chars().count();
        if seat.decl.platform == "discord" && len > DISCORD_MAX {
            return Err(VerbError::value(format!("seats send: Discord takes at most {DISCORD_MAX} characters per message and this text has {len}. Send it as several messages, or attach it as a file.")));
        }
        let thread = Some(s(&call.args, "thread")).filter(|t| !t.is_empty()).or_else(|| latest.filter(|l| l.account == seat.account() && l.channel == seat.channel()).map(|l| l.thread).filter(|t| !t.is_empty()));
        Ok(self.link.send(&json!({"account": seat.account(), "channel": seat.channel(), "thread": thread, "text": text, "files": files})).await)
    }

    /// React to, or download from, a message in the agent's seat: the latest line's by default.
    async fn act(&self, call: &Call, action: &str, extra: Value) -> Result<Value, VerbError> {
        let (session, seats) = self.agent_seats(call).await?;
        let latest = self.seats.store.line(&session).await;
        let seat = pick(&seats, &s(&call.args, "channel"), latest.as_ref())?;
        let message = Some(s(&call.args, "message_id"))
            .filter(|m| !m.is_empty())
            .or_else(|| latest.filter(|l| l.account == seat.account() && l.channel == seat.channel()).map(|l| l.message_id).filter(|m| !m.is_empty()))
            .ok_or_else(|| VerbError::value(format!("seats {}: no line of this seat names a message. Pass message_id.", call.verb)))?;
        let mut args = json!({"action": action, "account": seat.account(), "channel": seat.channel(), "message_id": message});
        if let (Value::Object(a), Value::Object(e)) = (&mut args, extra) {
            a.extend(e.into_iter().filter(|(_, v)| !v.is_null()));
        }
        self.link.act(args).await
    }

    async fn react(&self, call: Call) -> Result<Value, VerbError> {
        let emoji = s(&call.args, "emoji");
        if emoji.is_empty() {
            return Err(VerbError::value("seats react needs an emoji"));
        }
        self.act(&call, "react", json!({"emoji": emoji, "remove": call.args.get("remove")})).await
    }

    async fn download(&self, call: Call) -> Result<Value, VerbError> {
        self.act(&call, "download", json!({"idx": call.args.get("idx")})).await
    }

    async fn public(&self, seat: &Seat) -> Value {
        self.seats.public(seat).await
    }

    async fn list(&self, _call: Call) -> Result<Value, VerbError> {
        let mut out = vec![];
        for seat in self.seats.store.all().await {
            out.push(self.public(&seat).await);
        }
        Ok(json!({"seats": out, "paused": self.seats.settings().paused, "link": self.link.status().await}))
    }

    fn named(call: &Call) -> Result<(String, String), VerbError> {
        if !call.trusted() {
            return Err(VerbError::permission(format!("{}: only a trusted session runs this", call.verb)));
        }
        Ok((s(&call.args, "account"), s(&call.args, "channel")))
    }

    fn no_seat(account: &str, channel: &str) -> VerbError {
        VerbError::value(format!("{account}/{channel} is no seat on this node; `seats list` shows them"))
    }

    async fn reset(&self, call: Call) -> Result<Value, VerbError> {
        let (account, channel) = Self::named(&call)?;
        let seat = self.seats.reset(&account, &channel).await.ok_or_else(|| Self::no_seat(&account, &channel))?;
        Ok(json!({"ok": true, "seat": self.public(&seat).await}))
    }

    async fn admit(&self, call: Call) -> Result<Value, VerbError> {
        let (account, channel) = Self::named(&call)?;
        let seat = self.seats.admit(&account, &channel).await.ok_or_else(|| Self::no_seat(&account, &channel))?;
        Ok(json!({"ok": true, "seat": self.public(&seat).await}))
    }

    /// A session's level from reflection's row, or None when it does not say.
    async fn session_level(call: &Call, session: &str) -> Option<Level> {
        holders::session_rows(call.hub(), false).await?.iter().find(|r| s(r, "session_id") == session).and_then(|r| ["trust", "level"].iter().find_map(|k| r.get(*k).and_then(Value::as_str)).and_then(|t| t.parse().ok()))
    }

    async fn adopt(&self, call: Call) -> Result<Value, VerbError> {
        let (account, channel) = Self::named(&call)?;
        let session = s(&call.args, "session_id");
        let seat = self.seats.store.get(&account, &channel).await.ok_or_else(|| Self::no_seat(&account, &channel))?;
        let wanted: Level = seat.decl.level.parse().unwrap_or(Level::Untrusted);
        match Self::session_level(&call, &session).await {
            Some(l) if l < wanted => return Err(VerbError::permission(format!("session {session} runs at {}, below the seat's {}", l.as_str(), wanted.as_str()))),
            None if wanted == Level::Trusted => return Err(VerbError::permission(format!("session {session}'s level cannot be read, and the seat is trusted"))),
            _ => {}
        }
        let seat = self.seats.adopt(&seat.decl, &session).await;
        Ok(json!({"ok": true, "seat": self.public(&seat).await}))
    }
}

fn service(svc: Service, app: Arc<App>) -> Service {
    macro_rules! verb {
        ($svc:expr, $name:literal, $desc:literal, $params:expr, $method:ident) => {{
            let a = app.clone();
            $svc.verb("seats", $name, $desc, $params, move |call| {
                let a = a.clone();
                async move { a.$method(call).await }
            })
        }};
    }
    let account_channel = || json!([param("account", "string", true), param("channel", "string", true)]);
    let svc = verb!(
        svc,
        "send",
        "Answer your seat's chat. With no `channel` it answers your latest pushed line, in its thread. If the link to the chat is down, the reply waits on this node and goes out once. `file` uploads a file from this node (an absolute path); `files` takes several such paths, up to 24 MiB in all. Use `social send` for a chat that is not your seat.",
        json!([param("text", "string", false), param("channel", "string", false), param("thread", "string", false), param("file", "string", false), param("files", "array", false)]),
        send
    );
    let svc = verb!(
        svc,
        "react",
        "React to a message in your seat's chat with an emoji (a unicode emoji, or a custom one as `<:name:id>` on Discord, `name` on Slack). With no `message_id` it reacts to your latest line's message. `remove` takes the reaction off. Fails on a platform without reactions.",
        json!([param("emoji", "string", true), param("remove", "boolean", false), param("message_id", "string", false), param("channel", "string", false)]),
        react
    );
    let svc = verb!(
        svc,
        "download",
        "Save the files posted with a message in your seat's chat to a fresh temp dir on this node, and return each file's `path`. With no `message_id` it takes your latest line's message. `idx` picks one attachment. Files together may not pass 24 MiB. Fails on a platform without attachments.",
        json!([param("message_id", "string", false), param("idx", "number", false), param("channel", "string", false)]),
        download
    );
    let svc = verb!(svc, "list", "This node's seats, whether they are paused, and the state of the link to social.", json!([]), list);
    let svc = verb!(svc, "reset", "Clear a seat's start breaker and its missing-scope hold, then look at it again. Trusted only.", account_channel(), reset);
    let svc = verb!(svc, "admit", "Let an outside sender's messages through to a seat. Trusted only.", account_channel(), admit);
    verb!(svc, "adopt", "Make a running session this seat's agent, keeping its conversation. Trusted only.", json!([param("account", "string", true), param("channel", "string", true), param("session_id", "string", true)]), adopt)
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    let svc = Service::from_env()?;
    let env = svc.context().env().clone();
    let hub = svc.context().hub().clone();
    let store = store::Store::open(&awm_plugin_sdk::config::service_dir("seats").join("seats.db"))?;
    let epoch = match store.meta("epoch").await {
        Some(e) => e,
        None => {
            let e = format!("{}-{:x}", env.node, (now() * 1e6) as u64);
            store.set_meta("epoch", &e).await;
            e
        }
    };
    let settings: Settings = store.meta("settings").await.and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default();
    let link = Link::new(Arc::new(link::ThroughDoor(hub.clone())), store.clone(), epoch);
    let on_return = Arc::new(|(node, session): (String, String), lines: Vec<String>, why| tracing::info!("{} line(s) for hand mount {session}@{node} came back ({why:?})", lines.len()));
    let pusher = Pusher::reflection(hub.clone(), on_return);
    let world = Arc::new(world::Live { hub: hub.clone(), here: env.node.clone(), link: link.clone(), pusher });
    let seats = Seats::new(store, settings, world);

    let watched = seats.clone();
    let watch = awm_plugin_sdk::sessions::SessionWatch::new(hub, "seats", move |_node| {
        let seats = watched.clone();
        async move { seats.wake() }
    });
    let here = env.node.clone();
    let app = Arc::new(App { seats: seats.clone(), link: link.clone(), _watch: watch.clone() });
    service(svc, app)
        .on_ready(async move {
            watch.ensure(&here);
            seats.report_all().await;
            tokio::spawn(seats.clone().tick_forever());
            tokio::spawn(link.clone().flush_forever(seats.clone()));
            tokio::spawn(link.pull_forever(seats));
        })
        .run()
        .await
}
