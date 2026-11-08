//! The gateway side of the Discord connector as pure functions: the guild and channel cache,
//! the normalised [`Inbound`], and the `/approve` registration and invocation.

use std::collections::{HashMap, HashSet};

use serde_json::{json, Value};
use twilight_model::application::interaction::application_command::CommandOptionValue;
use twilight_model::application::interaction::{Interaction, InteractionData, InteractionType};
use twilight_model::channel::message::MessageType;
use twilight_model::channel::{Channel, ChannelType, Message};
use twilight_model::gateway::event::Event;

use crate::connectors::{Attachment, ChatKind, Inbound};

pub const APPROVE_COMMAND: &str = "approve";
/// The channel form naming a user's DM with the bot, such as `dm:4242`.
pub const DM_PREFIX: &str = "dm:";
/// The Duo devices `/approve` offers, as the 2fa service names them.
pub const DEVICE_CHOICES: [&str; 2] = ["cwl", "alliance"];
pub const DEFAULT_DEVICE: &str = "cwl";

const DISCORD_EPOCH_MS: i64 = 1_420_070_400_000;

/// `str(user)` in discord.py: the bare name once Discord dropped the user's discriminator.
pub fn user_str(name: &str, discriminator: u16) -> String {
    if discriminator == 0 {
        name.to_string()
    } else {
        format!("{name}#{discriminator:04}")
    }
}

/// A snowflake's creation time as Python's `datetime.isoformat()` writes it.
pub fn snowflake_iso(id: u64) -> String {
    let ms = (id >> 22) as i64 + DISCORD_EPOCH_MS;
    let Some(t) = chrono::DateTime::from_timestamp_millis(ms) else { return String::new() };
    // isoformat omits the fraction when it is zero.
    if ms % 1000 == 0 {
        t.format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
    } else {
        t.format("%Y-%m-%dT%H:%M:%S%.6f+00:00").to_string()
    }
}

/// Whether a message is a person's plain words. Only a DM filters: a bot post, a system or
/// command message, and text that reads as a failed slash command stay out of the holder's prompt.
pub fn natural(m: &Message, kind: ChatKind) -> bool {
    if kind != ChatKind::Dm {
        return true;
    }
    !m.author.bot && matches!(m.kind, MessageType::Regular | MessageType::Reply) && !m.content.trim_start().starts_with('/')
}

pub fn attachments_of(m: &Message) -> Vec<Attachment> {
    m.attachments
        .iter()
        .enumerate()
        .map(|(i, a)| Attachment {
            idx: i as i64,
            filename: if a.filename.is_empty() { format!("attachment-{i}") } else { a.filename.clone() },
            mime: a.content_type.clone().unwrap_or_default(),
            size: a.size as i64,
            url: a.url.clone(),
            r#ref: json!({"id": a.id.get().to_string()}),
        })
        .collect()
}

/// Where a message sits: its channel's name, its guild's name, and the kind of chat.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Place {
    pub channel_name: String,
    pub server: String,
    pub kind: ChatKind,
}

pub fn inbound(account: &str, m: &Message, place: &Place, to_holder: bool) -> Inbound {
    Inbound {
        account: account.to_string(),
        platform: "discord".into(),
        channel_id: m.channel_id.get().to_string(),
        channel_name: place.channel_name.clone(),
        text: m.content.clone(),
        sender_id: m.author.id.get().to_string(),
        sender_name: user_str(&m.author.name, m.author.discriminator),
        thread_id: String::new(),
        message_id: m.id.get().to_string(),
        ts: snowflake_iso(m.id.get()),
        attachments: attachments_of(m),
        sender_bot: m.author.bot,
        to_holder,
        server: place.server.clone(),
        kind: place.kind,
        outside: false,
        resynced: false,
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Chan {
    pub kind: ChannelType,
    pub name: String,
    pub guild: Option<u64>,
    pub position: i32,
    /// The other person of a DM: id and display name.
    pub recipient: Option<(u64, String)>,
}

/// What the gateway has told this connection: who it is, its guilds, and the channels it saw.
#[derive(Debug, Default)]
pub struct Cache {
    /// Our own user: id and display name.
    pub me: Option<(u64, String)>,
    pub app: Option<u64>,
    /// Guild id and name, in the order the gateway announced them.
    pub guilds: Vec<(u64, String)>,
    pub chans: HashMap<u64, Chan>,
    /// The DM channel with each user.
    pub dm_of: HashMap<u64, u64>,
    /// DMs where someone other than us has written.
    pub spoken: HashSet<u64>,
}

/// What the event loop must do after the cache took an event.
#[derive(Debug, PartialEq)]
pub enum Next {
    Nothing,
    /// The session is up: register the commands for this application.
    Ready(u64),
    Message(Box<Inbound>),
    Interaction(Box<Interaction>),
}

impl Cache {
    pub fn put_channel(&mut self, c: &Channel) {
        let recipient = c.recipients.as_ref().and_then(|r| r.first()).map(|u| (u.id.get(), user_str(&u.name, u.discriminator)));
        if c.kind == ChannelType::Private {
            if let Some((uid, _)) = &recipient {
                self.dm_of.insert(*uid, c.id.get());
            }
        }
        let prev = self.chans.get(&c.id.get());
        let recipient = recipient.or_else(|| prev.and_then(|p| p.recipient.clone()));
        self.chans.insert(
            c.id.get(),
            Chan {
                kind: c.kind,
                name: c.name.clone().unwrap_or_default(),
                guild: c.guild_id.map(|g| g.get()),
                position: c.position.unwrap_or(0),
                recipient,
            },
        );
    }

    pub fn put_guild(&mut self, id: u64, name: &str, channels: &[Channel]) {
        match self.guilds.iter_mut().find(|(g, _)| *g == id) {
            Some(g) => g.1 = name.to_string(),
            None => self.guilds.push((id, name.to_string())),
        }
        for c in channels {
            let mut c = c.clone();
            // A guild's channel list leaves out each channel's guild id.
            c.guild_id = c.guild_id.or(twilight_model::id::Id::new_checked(id));
            self.put_channel(&c);
        }
    }

    pub fn drop_guild(&mut self, id: u64) {
        self.guilds.retain(|(g, _)| *g != id);
        self.chans.retain(|_, c| c.guild != Some(id));
    }

    pub fn guild_name(&self, id: u64) -> String {
        self.guilds.iter().find(|(g, _)| *g == id).map(|(_, n)| n.clone()).unwrap_or_default()
    }

    /// Where a channel sits. A channel the cache never saw outside a guild is a DM.
    pub fn place(&self, channel: u64, guild: Option<u64>) -> Place {
        let c = self.chans.get(&channel);
        let guild = guild.or_else(|| c.and_then(|c| c.guild));
        let kind = match (guild, c.map(|c| c.kind)) {
            (Some(_), _) => ChatKind::Channel,
            (None, Some(ChannelType::Group)) => ChatKind::Group,
            (None, Some(ChannelType::Private) | None) => ChatKind::Dm,
            (None, Some(_)) => ChatKind::Channel,
        };
        let channel_name = match kind {
            ChatKind::Dm => String::new(),
            _ => c.map(|c| c.name.clone()).unwrap_or_default(),
        };
        Place { channel_name, server: guild.map(|g| self.guild_name(g)).unwrap_or_default(), kind }
    }

    pub fn is_me(&self, user: u64) -> bool {
        self.me.as_ref().is_some_and(|(id, _)| *id == user)
    }

    /// Take one message from the live feed: remember its DM and whether a person wrote there.
    /// None for our own message.
    pub fn message(&mut self, account: &str, m: &Message) -> Option<Inbound> {
        let author = m.author.id.get();
        if self.is_me(author) {
            return None;
        }
        let ch = m.channel_id.get();
        let place = self.place(ch, m.guild_id.map(|g| g.get()));
        if place.kind == ChatKind::Dm {
            let entry = self.chans.entry(ch).or_insert(Chan { kind: ChannelType::Private, name: String::new(), guild: None, position: 0, recipient: None });
            if !m.author.bot {
                entry.recipient = Some((author, user_str(&m.author.name, m.author.discriminator)));
                self.dm_of.insert(author, ch);
            }
        }
        if place.kind != ChatKind::Channel && !m.author.bot {
            self.spoken.insert(ch);
        }
        Some(inbound(account, m, &place, natural(m, place.kind)))
    }

    pub fn apply(&mut self, account: &str, e: &Event) -> Next {
        match e {
            Event::Ready(r) => {
                self.me = Some((r.user.id.get(), user_str(&r.user.name, r.user.discriminator)));
                self.app = Some(r.application.id.get());
                return Next::Ready(r.application.id.get());
            }
            Event::GuildCreate(g) => {
                if let twilight_model::gateway::payload::incoming::GuildCreate::Available(g) = &**g {
                    let mut all = g.channels.clone();
                    all.extend(g.threads.iter().cloned());
                    self.put_guild(g.id.get(), &g.name, &all);
                }
            }
            Event::GuildUpdate(g) => self.put_guild(g.0.id.get(), &g.0.name, &[]),
            Event::GuildDelete(g) if g.unavailable != Some(true) => self.drop_guild(g.id.get()),
            Event::ChannelCreate(c) => self.put_channel(&c.0),
            Event::ChannelUpdate(c) => self.put_channel(&c.0),
            Event::ThreadCreate(c) => self.put_channel(&c.0),
            Event::ThreadUpdate(c) => self.put_channel(&c.0),
            Event::ChannelDelete(c) => {
                self.chans.remove(&c.0.id.get());
            }
            Event::ThreadDelete(c) => {
                self.chans.remove(&c.id.get());
            }
            Event::MessageCreate(m) => {
                if let Some(i) = self.message(account, &m.0) {
                    return Next::Message(Box::new(i));
                }
            }
            Event::InteractionCreate(i) => return Next::Interaction(Box::new(i.0.clone())),
            _ => {}
        }
        Next::Nothing
    }

    /// The guild text channels in UI order, then with `include_dms` the DMs this connection saw.
    pub fn channels(&self, include_dms: bool) -> Vec<crate::connectors::Channel> {
        let mut out = vec![];
        for (gid, gname) in &self.guilds {
            let mut rows: Vec<(&u64, &Chan)> = self
                .chans
                .iter()
                .filter(|(_, c)| c.guild == Some(*gid) && matches!(c.kind, ChannelType::GuildText | ChannelType::GuildAnnouncement))
                .collect();
            rows.sort_by_key(|(id, c)| (c.position, **id));
            out.extend(rows.into_iter().map(|(id, c)| crate::connectors::Channel { id: id.to_string(), name: format!("{gname}#{}", c.name), kind: "text".into() }));
        }
        if include_dms {
            let mut dms: Vec<(&u64, &Chan)> = self.chans.iter().filter(|(_, c)| c.kind == ChannelType::Private).collect();
            dms.sort_by_key(|(id, _)| **id);
            out.extend(dms.into_iter().map(|(id, c)| crate::connectors::Channel {
                id: id.to_string(),
                name: c.recipient.as_ref().map(|r| r.1.clone()).unwrap_or_else(|| format!("{DM_PREFIX}{id}")),
                kind: "dm".into(),
            }));
        }
        out
    }
}

/// The `/approve` command exactly as discord.py's `tree.sync()` sends it: offered only in the
/// bot's DM (context 1), on a guild install (integration type 0).
pub fn approve_payload() -> Value {
    json!([{
        "name": APPROVE_COMMAND,
        "description": "Auto-approve Duo logins for 5 minutes (DM the bot).",
        "type": 1,
        "options": [{
            "type": 3,
            "name": "device",
            "description": "Duo device to approve for (default cwl)",
            "required": false,
            "choices": DEVICE_CHOICES.iter().map(|d| json!({"name": d, "value": d})).collect::<Vec<_>>(),
        }],
        "nsfw": false,
        "dm_permission": true,
        "default_member_permissions": null,
        "contexts": [1],
        "integration_types": [0],
    }])
}

/// One `/approve` invocation, read off its interaction.
#[derive(Debug, Clone, PartialEq)]
pub struct Invocation {
    pub user_id: String,
    pub user_name: String,
    pub channel_id: String,
    pub in_guild: bool,
    pub device: String,
}

/// The `/approve` invocation an interaction carries, or None for any other interaction.
pub fn approve_of(i: &Interaction) -> Option<Invocation> {
    if i.kind != InteractionType::ApplicationCommand {
        return None;
    }
    let Some(InteractionData::ApplicationCommand(data)) = &i.data else { return None };
    if data.name != APPROVE_COMMAND {
        return None;
    }
    let device = data
        .options
        .iter()
        .find(|o| o.name == "device")
        .and_then(|o| match &o.value {
            CommandOptionValue::String(s) if !s.is_empty() => Some(s.clone()),
            _ => None,
        })
        .unwrap_or_else(|| DEFAULT_DEVICE.to_string());
    let user = i.author();
    #[allow(deprecated)]
    let channel = i.channel.as_ref().map(|c| c.id).or(i.channel_id);
    Some(Invocation {
        user_id: user.map(|u| u.id.get().to_string()).unwrap_or_default(),
        user_name: user.map(|u| user_str(&u.name, u.discriminator)).unwrap_or_default(),
        channel_id: channel.map(|c| c.get().to_string()).unwrap_or_default(),
        in_guild: i.guild_id.is_some(),
        device,
    })
}

/// The gate's answer to one invocation: the reply, whether only the caller sees it, and the
/// operator when the command goes on to `on_command`.
#[derive(Debug, Clone, PartialEq)]
pub struct Verdict {
    pub reply: String,
    pub ephemeral: bool,
    pub operator: Option<String>,
}

/// A guild invocation is refused before the operator table is read, and a stranger after.
pub fn verdict(inv: &Invocation, operator: Option<String>) -> Verdict {
    if inv.in_guild {
        return Verdict { reply: "DM me to use this command.".into(), ephemeral: true, operator: None };
    }
    match operator {
        None => Verdict { reply: "You are not an operator of this bot.".into(), ephemeral: true, operator: None },
        Some(op) => Verdict { reply: format!("⏳ Arming `{}` Duo approvals for 5 min…", inv.device), ephemeral: false, operator: Some(op) },
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;

    pub fn user_json(id: u64, name: &str, bot: bool) -> Value {
        json!({"id": id.to_string(), "username": name, "discriminator": "0", "avatar": null, "bot": bot, "global_name": null})
    }

    /// A message as the REST API and the gateway write it.
    pub fn message_json(id: u64, channel: u64, author: Value, content: &str, kind: u8) -> Value {
        json!({
            "id": id.to_string(), "channel_id": channel.to_string(), "author": author, "content": content,
            "timestamp": "2024-01-01T00:00:00.000000+00:00", "edited_timestamp": null, "tts": false,
            "mention_everyone": false, "mentions": [], "mention_roles": [], "attachments": [], "embeds": [],
            "pinned": false, "type": kind,
        })
    }

    fn message(author_bot: bool, kind: u8, content: &str, guild: Option<u64>) -> Message {
        let mut v = message_json(1 << 22, 555, user_json(4242, "tester", author_bot), content, kind);
        if let Some(g) = guild {
            v["guild_id"] = json!(g.to_string());
        }
        serde_json::from_value(v).unwrap()
    }

    fn channel(v: Value) -> Channel {
        serde_json::from_value(v).unwrap()
    }

    #[test]
    fn a_dm_seat_hears_only_plain_words() {
        for (bot, kind, text, heard) in [
            (false, 0, "tell the session yes", true),
            (false, 19, "yes", true),
            (true, 0, "approvals armed", false),
            (false, 20, "", false),
            (false, 0, "/new-agen fix the build", false),
            (false, 0, "  /x", false),
        ] {
            assert_eq!(natural(&message(bot, kind, text, None), ChatKind::Dm), heard, "{bot} {kind} {text:?}");
        }
    }

    #[test]
    fn a_guild_message_always_reaches_the_holder() {
        let m = message(true, 20, "/x", Some(9));
        assert!(natural(&m, ChatKind::Channel));
        assert!(natural(&m, ChatKind::Group));
        let mut c = Cache::default();
        let got = c.message("bot", &m).unwrap();
        assert!(got.to_holder);
        assert_eq!(got.kind, ChatKind::Channel);
    }

    #[test]
    fn users_print_as_discord_py_prints_them() {
        assert_eq!(user_str("tester", 0), "tester");
        assert_eq!(user_str("tester", 1), "tester#0001");
    }

    #[test]
    fn a_snowflake_time_matches_python_isoformat() {
        assert_eq!(snowflake_iso(0), "2015-01-01T00:00:00+00:00");
        assert_eq!(snowflake_iso(1234 << 22), "2015-01-01T00:00:01.234000+00:00");
    }

    #[test]
    fn a_dm_message_is_normalised_and_marks_the_dm_spoken() {
        let mut c = Cache { me: Some((1, "bot".into())), ..Default::default() };
        let mut v = message_json(1 << 22, 555, user_json(4242, "tester", false), "hello", 0);
        v["attachments"] = json!([{"id": "77", "filename": "a.txt", "size": 3, "url": "http://cdn.test/a.txt", "proxy_url": "", "content_type": "text/plain"}]);
        let m: Message = serde_json::from_value(v).unwrap();
        let got = c.message("bot", &m).unwrap();
        assert_eq!(
            got,
            Inbound {
                account: "bot".into(),
                platform: "discord".into(),
                channel_id: "555".into(),
                text: "hello".into(),
                sender_id: "4242".into(),
                sender_name: "tester".into(),
                message_id: (1u64 << 22).to_string(),
                ts: "2015-01-01T00:00:00.001000+00:00".into(),
                attachments: vec![Attachment { idx: 0, filename: "a.txt".into(), mime: "text/plain".into(), size: 3, url: "http://cdn.test/a.txt".into(), r#ref: json!({"id": "77"}) }],
                to_holder: true,
                kind: ChatKind::Dm,
                ..Default::default()
            }
        );
        assert!(c.spoken.contains(&555));
        assert_eq!(c.dm_of.get(&4242), Some(&555));
        assert_eq!(c.channels(true), vec![crate::connectors::Channel { id: "555".into(), name: "tester".into(), kind: "dm".into() }]);
    }

    #[test]
    fn our_own_message_is_dropped_and_a_bot_dm_marks_nothing() {
        let mut c = Cache { me: Some((4242, "bot".into())), ..Default::default() };
        assert_eq!(c.message("bot", &message(false, 0, "x", None)), None);
        c.me = Some((1, "bot".into()));
        let got = c.message("bot", &message(true, 0, "x", None)).unwrap();
        assert!(!got.to_holder && got.sender_bot);
        assert!(c.spoken.is_empty());
    }

    #[test]
    fn guild_channels_carry_the_server_and_list_in_ui_order() {
        let mut c = Cache::default();
        let chans = [
            channel(json!({"id": "11", "type": 0, "name": "general", "position": 2})),
            channel(json!({"id": "12", "type": 0, "name": "lab", "position": 1})),
            channel(json!({"id": "13", "type": 2, "name": "voice", "position": 0})),
            channel(json!({"id": "14", "type": 11, "name": "a-thread", "guild_id": "9"})),
        ];
        c.put_guild(9, "Lab Server", &chans);
        c.put_channel(&channel(json!({"id": "20", "type": 3, "name": "group"})));
        assert_eq!(c.place(11, Some(9)), Place { channel_name: "general".into(), server: "Lab Server".into(), kind: ChatKind::Channel });
        assert_eq!(c.place(14, Some(9)).channel_name, "a-thread");
        assert_eq!(c.place(20, None), Place { channel_name: "group".into(), server: String::new(), kind: ChatKind::Group });
        assert_eq!(c.place(999, None).kind, ChatKind::Dm);
        let names: Vec<String> = c.channels(false).into_iter().map(|c| format!("{}={}", c.id, c.name)).collect();
        assert_eq!(names, ["12=Lab Server#lab", "11=Lab Server#general"]);
        c.drop_guild(9);
        assert!(c.channels(false).is_empty());
    }

    #[test]
    fn the_approve_registration_equals_discord_py_s() {
        // Written from discord.py 2.7.1: `[c.to_dict(tree) for c in tree.get_commands()]`.
        let expected: Value = serde_json::from_str(
            r#"[{"name": "approve", "description": "Auto-approve Duo logins for 5 minutes (DM the bot).", "type": 1,
                 "options": [{"type": 3, "name": "device", "description": "Duo device to approve for (default cwl)", "required": false,
                              "choices": [{"name": "cwl", "value": "cwl"}, {"name": "alliance", "value": "alliance"}]}],
                 "nsfw": false, "dm_permission": true, "default_member_permissions": null, "contexts": [1], "integration_types": [0]}]"#,
        )
        .unwrap();
        assert_eq!(approve_payload(), expected);
    }

    pub fn interaction_json(guild: Option<u64>, uid: u64, device: Option<&str>, name: &str) -> Value {
        let options: Vec<Value> = device.into_iter().map(|d| json!({"name": "device", "type": 3, "value": d})).collect();
        let mut v = json!({
            "id": "31337", "application_id": "8", "type": 2, "token": "itoken",
            "authorizing_integration_owners": {}, "entitlements": [],
            "channel": {"id": "999", "type": if guild.is_some() { 0 } else { 1 }},
            "channel_id": "999",
            "data": {"id": "5", "name": name, "type": 1, "options": options},
        });
        match guild {
            Some(g) => {
                v["guild_id"] = json!(g.to_string());
                v["member"] = json!({"user": user_json(uid, "tester", false), "roles": [], "joined_at": null, "deaf": false, "mute": false, "flags": 0});
            }
            None => v["user"] = user_json(uid, "tester", false),
        }
        v
    }

    pub fn interaction(guild: Option<u64>, uid: u64, device: Option<&str>) -> Interaction {
        serde_json::from_value(interaction_json(guild, uid, device, APPROVE_COMMAND)).unwrap()
    }

    #[test]
    fn an_invocation_reads_its_device_user_and_channel() {
        let inv = approve_of(&interaction(None, 4242, None)).unwrap();
        assert_eq!(inv, Invocation { user_id: "4242".into(), user_name: "tester".into(), channel_id: "999".into(), in_guild: false, device: "cwl".into() });
        assert_eq!(approve_of(&interaction(None, 4242, Some("alliance"))).unwrap().device, "alliance");
        assert!(approve_of(&interaction(Some(9), 4242, None)).unwrap().in_guild);
        let other: Interaction = serde_json::from_value(interaction_json(None, 4242, None, "other")).unwrap();
        assert_eq!(approve_of(&other), None);
    }

    #[test]
    fn the_gate_refuses_a_guild_then_a_stranger() {
        let dm = approve_of(&interaction(None, 4242, None)).unwrap();
        let guild = approve_of(&interaction(Some(9), 4242, None)).unwrap();
        let v = verdict(&guild, Some("tony".into()));
        assert_eq!((v.reply.as_str(), v.ephemeral, v.operator), ("DM me to use this command.", true, None));
        let v = verdict(&dm, None);
        assert!(v.reply.contains("not an operator") && v.ephemeral && v.operator.is_none());
        let v = verdict(&dm, Some("tony".into()));
        assert_eq!((v.reply.as_str(), v.ephemeral, v.operator.as_deref()), ("⏳ Arming `cwl` Duo approvals for 5 min…", false, Some("tony")));
    }
}
