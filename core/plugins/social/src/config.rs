//! `social.toml`: the bot accounts and seats (`config.py`).
//!
//! The file is `$AWM_DIR/social.toml`, or `AWM_SOCIAL_CONFIG`. Ansible writes it on alcor, and
//! social never rewrites it. Entries social does not drive are skipped with a log line and stay
//! in the file: the mira source, Teams, the keeper's `[seat.*]` and `[keeper]` tables, and Tony's
//! own accounts and `[bucket.*]` drives, which `productivity` serves.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use awm_trust::trust::check_effort;
use awm_trust::Level;
use awm_chat::config::{err, opt_bool, opt_str, parse, secret, tables, ConfigError, R};
use toml::{Table, Value};

pub const KNOWN_PLATFORMS: [&str; 4] = ["discord", "slack", "gmail", "teams"];
pub const LEVELS: [&str; 3] = ["untrusted", "cooperative", "trusted"];

#[derive(Debug, Clone, Default, PartialEq)]
pub struct AccountConfig {
    pub name: String,
    pub platform: String,
    pub token: String,
    pub app_token: Option<String>,
    pub display_name: Option<String>,
    pub enabled: bool,
    pub singleton: bool,
    pub trust: String,
    pub trusted_chats: Vec<String>,
}

impl AccountConfig {
    /// The level of one chat: trusted when it is in `trusted_chats`, else the account's.
    pub fn level_of(&self, chat: &str) -> Level {
        if self.trusted_chats.iter().any(|c| c == chat) {
            return Level::Trusted;
        }
        self.trust.parse::<Level>().ok().unwrap_or(Level::Untrusted)
    }
}

/// Where a down notice goes: one chat on one account.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Notice {
    pub account: String,
    pub channel: String,
}

/// One kind of chat that is a seat: every chat of `kind` on `account`, but those in `except`.
#[derive(Debug, Clone, PartialEq)]
pub struct SeatRule {
    pub account: String,
    /// `dm`, `group` or `channel`.
    pub kind: String,
    pub node: String,
    pub level: Level,
    /// The scope under the seats project. `{slug}` is the chat's slug.
    pub scope: String,
    /// Chat ids and sender ids that are not seats, such as Slackbot.
    pub except: Vec<String>,
    /// The agent's model and effort, instead of `[seats]`.
    pub agent: Option<(String, String)>,
}

/// One named chat that is a seat, overriding any rule.
#[derive(Debug, Clone, PartialEq)]
pub struct SeatChat {
    pub account: String,
    pub channel: String,
    pub node: String,
    pub level: Level,
    pub scope: String,
    /// The agent's model and effort, instead of `[seats]`.
    pub agent: Option<(String, String)>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct SeatsConfig {
    pub project: String,
    pub model: String,
    pub effort: String,
    pub paused: bool,
    pub notice: Option<Notice>,
    /// The seat docs on the agents' nodes: `seat-sop.md` and `briefs/`.
    pub docs: String,
    pub rules: Vec<SeatRule>,
    pub chats: Vec<SeatChat>,
}

impl Default for SeatsConfig {
    fn default() -> Self {
        Self { project: "agentic-commons".into(), model: "opus".into(), effort: "high".into(), paused: true, notice: None, docs: "/home/tony/agentic_workspace/projects/agentic-commons/main/docs".into(), rules: vec![], chats: vec![] }
    }
}

enum Place<'a> {
    Chat(&'a SeatChat),
    Rule(&'a SeatRule),
}

impl SeatsConfig {
    /// The seat a chat is, if any: a named chat first, else the first rule of its kind.
    pub fn seat_for(&self, account: &str, channel: &str, kind: &str, sender: &str) -> Option<(String, Level, String)> {
        match self.place(account, channel, kind, sender)? {
            Place::Chat(c) => Some((c.node.clone(), c.level, c.scope.clone())),
            Place::Rule(r) => Some((r.node.clone(), r.level, r.scope.clone())),
        }
    }

    /// A seat's own model and effort, read live so a config edit reaches an existing seat.
    pub fn agent_for(&self, account: &str, channel: &str, kind: &str) -> Option<(String, String)> {
        match self.place(account, channel, kind, "")? {
            Place::Chat(c) => c.agent.clone(),
            Place::Rule(r) => r.agent.clone(),
        }
    }

    fn place(&self, account: &str, channel: &str, kind: &str, sender: &str) -> Option<Place<'_>> {
        if let Some(c) = self.chats.iter().find(|c| c.account == account && c.channel == channel) {
            return Some(Place::Chat(c));
        }
        self.rules.iter().find(|r| r.account == account && r.kind == kind && !r.except.iter().any(|e| e == channel || e == sender)).map(Place::Rule)
    }

    /// The notice chat: the `notice` table, else the trusted Discord DM a `[[seats.chat]]` names.
    pub fn notice_for(&self, accounts: &[AccountConfig]) -> Option<Notice> {
        if self.notice.is_some() {
            return self.notice.clone();
        }
        self.chats
            .iter()
            .filter(|c| c.level == Level::Trusted)
            .find(|c| accounts.iter().any(|a| a.name == c.account && a.platform == "discord"))
            .map(|c| Notice { account: c.account.clone(), channel: c.channel.clone() })
    }
}

/// Everything social reads from the file. A bad `[seats]` table keeps no seats and leaves the
/// accounts up.
#[derive(Debug, Clone, Default)]
pub struct Loaded {
    pub accounts: Vec<AccountConfig>,
    pub seats: SeatsConfig,
    /// One line per entry skipped or table refused.
    pub notes: Vec<String>,
}

pub fn config_file(awm_dir: &Path) -> PathBuf {
    match std::env::var("AWM_SOCIAL_CONFIG") {
        Ok(p) if !p.trim().is_empty() => PathBuf::from(p.trim()),
        _ => awm_dir.join("social.toml"),
    }
}

/// Read the whole file. An account error fails the load. A seats error is a note.
pub fn load(path: &Path) -> R<Loaded> {
    let mut out = Loaded::default();
    let Some(data) = parse(path)? else { return Ok(out) };
    let (accounts, notes) = accounts(&data, path)?;
    out.accounts = accounts;
    out.notes = notes;
    if data.contains_key("bucket") {
        out.notes.push("[bucket.*] is productivity's: skipped".into());
    }
    match seats(&data, path) {
        Ok(s) => out.seats = s,
        Err(e) => out.notes.push(format!("seats refused, none kept: {e}")),
    }
    for (key, what) in [("seat", "[seat.*]"), ("keeper", "[keeper]")] {
        if data.contains_key(key) {
            out.notes.push(format!("{what} is the retired keeper's: skipped, [seats] declares seats now"));
        }
    }
    Ok(out)
}

fn accounts(data: &Table, cfg: &Path) -> R<(Vec<AccountConfig>, Vec<String>)> {
    let p = cfg.display();
    let (mut out, mut notes) = (vec![], vec![]);
    for (name, s) in tables(data, "account", cfg, "accounts")? {
        let label = format!("[account.{name}]");
        let platform = s.get("platform").and_then(Value::as_str).unwrap_or("");
        if !KNOWN_PLATFORMS.contains(&platform) {
            return err(format!("{p}: {label}.platform must be one of {}", KNOWN_PLATFORMS.join(", ")));
        }
        let source = s.get("source").and_then(Value::as_str);
        if source.is_some() && source != Some("mira") {
            return err(format!("{p}: {label}.source must be \"mira\" when set"));
        }
        if source == Some("mira") || platform == "teams" {
            notes.push(format!("{label} is a mira account: skipped, this build does not drive mira"));
            continue;
        }
        let token = secret(s, "token", cfg, &format!("{label}.token"), false)?.unwrap_or_default();
        if platform == "gmail" || (platform == "slack" && !token.starts_with("xoxb-")) {
            notes.push(format!("{label} is Tony's own account: skipped, productivity serves it"));
            continue;
        }
        if token.is_empty() {
            return err(format!("{p}: {label}.token (or token_file) is required"));
        }
        let app_token = secret(s, "app_token", cfg, &format!("{label}.app_token"), false)?;
        let display_name = opt_str(s, "display_name", cfg, &label)?.filter(|d| !d.is_empty());
        let enabled = opt_bool(s, "enabled", true, cfg, &label)?;
        let singleton = opt_bool(s, "singleton", false, cfg, &label)?;
        let trust = match s.get("trust") {
            None => if platform == "discord" { "untrusted" } else { "cooperative" }.to_string(),
            Some(Value::String(t)) if LEVELS.contains(&t.as_str()) => t.clone(),
            Some(_) => return err(format!("{p}: {label}.trust must be one of ('untrusted', 'cooperative', 'trusted')")),
        };
        let trusted_chats = match s.get("trusted_chats") {
            None => vec![],
            Some(Value::Array(a)) if a.iter().all(Value::is_str) => a.iter().map(|v| v.as_str().unwrap().to_string()).collect(),
            Some(_) => return err(format!("{p}: {label}.trusted_chats must be a list of chat ids")),
        };
        out.push(AccountConfig {
            name: name.trim().to_string(),
            platform: platform.to_string(),
            token,
            app_token,
            display_name,
            enabled,
            singleton,
            trust,
            trusted_chats,
        });
    }
    Ok((out, notes))
}

const SEATS_KEYS: [&str; 8] = ["project", "model", "effort", "paused", "notice", "docs", "rule", "chat"];
const RULE_KEYS: [&str; 8] = ["account", "kind", "node", "level", "scope", "except", "model", "effort"];
const CHAT_KEYS: [&str; 7] = ["account", "channel", "node", "level", "scope", "model", "effort"];

fn keys(t: &Table, allowed: &[&str], label: &str) -> R<()> {
    let mut bad: Vec<&String> = t.keys().filter(|k| !allowed.contains(&k.as_str())).collect();
    bad.sort();
    if bad.is_empty() {
        Ok(())
    } else {
        err(format!("{label} has unknown keys: {}", bad.iter().map(|k| k.as_str()).collect::<Vec<_>>().join(", ")))
    }
}

fn need(t: &Table, key: &str, label: &str) -> R<String> {
    match t.get(key).and_then(Value::as_str).map(str::trim) {
        Some(s) if !s.is_empty() => Ok(s.to_string()),
        _ => err(format!("{label}.{key} is required")),
    }
}

fn level(t: &Table, label: &str) -> R<Level> {
    let raw = need(t, "level", label)?;
    raw.parse::<Level>().ok().ok_or_else(|| ConfigError(format!("{label}.level must be one of {}", LEVELS.join(", "))))
}

/// A seat's `model` and `effort`, as `start_other` takes them: both, or neither.
fn agent(t: &Table, label: &str) -> R<Option<(String, String)>> {
    match (t.contains_key("model"), t.contains_key("effort")) {
        (false, false) => return Ok(None),
        (true, true) => {}
        _ => return err(format!("{label}: name model and effort together")),
    }
    let effort = need(t, "effort", label)?;
    check_effort(&effort).map_err(|e| ConfigError(format!("{label}.effort: {e}")))?;
    Ok(Some((need(t, "model", label)?, effort)))
}

fn seats(data: &Table, cfg: &Path) -> R<SeatsConfig> {
    let mut out = SeatsConfig::default();
    let Some(v) = data.get("seats") else { return Ok(out) };
    let p = cfg.display();
    let Some(t) = v.as_table() else { return err(format!("{p}: [seats] must be a table")) };
    keys(t, &SEATS_KEYS, "[seats]")?;
    for (key, slot) in [("project", &mut out.project), ("model", &mut out.model), ("effort", &mut out.effort), ("docs", &mut out.docs)] {
        if t.contains_key(key) {
            *slot = need(t, key, "[seats]")?;
        }
    }
    check_effort(&out.effort).map_err(|e| ConfigError(format!("[seats].effort: {e}")))?;
    out.paused = opt_bool(t, "paused", true, cfg, "[seats]")?;
    if let Some(n) = t.get("notice") {
        let n = n.as_table().ok_or_else(|| ConfigError("[seats].notice must be a table {account, channel}".into()))?;
        out.notice = Some(Notice { account: need(n, "account", "[seats].notice")?, channel: need(n, "channel", "[seats].notice")? });
    }
    let list = |key: &str| -> R<Vec<Table>> {
        match t.get(key) {
            None => Ok(vec![]),
            Some(Value::Array(a)) => a.iter().map(|v| v.as_table().cloned().ok_or_else(|| ConfigError(format!("[[seats.{key}]] must be tables")))).collect(),
            Some(_) => err(format!("[[seats.{key}]] must be an array of tables")),
        }
    };
    for (i, r) in list("rule")?.iter().enumerate() {
        let label = format!("[[seats.rule]] {}", i + 1);
        keys(r, &RULE_KEYS, &label)?;
        let kind = need(r, "kind", &label)?;
        if !["dm", "group", "channel"].contains(&kind.as_str()) {
            return err(format!("{label}.kind must be one of dm, group, channel"));
        }
        let except = match r.get("except") {
            None => vec![],
            Some(Value::Array(a)) if a.iter().all(Value::is_str) => a.iter().map(|v| v.as_str().unwrap().to_string()).collect(),
            Some(_) => return err(format!("{label}.except must be a list of ids")),
        };
        out.rules.push(SeatRule { account: need(r, "account", &label)?, kind, node: need(r, "node", &label)?, level: level(r, &label)?, scope: need(r, "scope", &label)?, except, agent: agent(r, &label)? });
    }
    let mut seen = BTreeMap::new();
    for (i, c) in list("chat")?.iter().enumerate() {
        let label = format!("[[seats.chat]] {}", i + 1);
        keys(c, &CHAT_KEYS, &label)?;
        let chat = SeatChat { account: need(c, "account", &label)?, channel: need(c, "channel", &label)?, node: need(c, "node", &label)?, level: level(c, &label)?, scope: need(c, "scope", &label)?, agent: agent(c, &label)? };
        if let Some(j) = seen.insert((chat.account.clone(), chat.channel.clone()), i + 1) {
            return err(format!("{label} names the chat [[seats.chat]] {j} names"));
        }
        out.chats.push(chat);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(dir: &Path, text: &str) -> PathBuf {
        let p = dir.join("social.toml");
        std::fs::write(&p, text).unwrap();
        p
    }

    #[test]
    fn a_seat_names_model_and_effort_together_under_the_ceiling() {
        let tmp = tempfile::tempdir().unwrap();
        let load = |extra: &str| {
            let p = write(tmp.path(), &format!("[[seats.chat]]\naccount = \"a\"\nchannel = \"C1\"\nnode = \"n\"\nlevel = \"trusted\"\nscope = \"s\"\n{extra}\n"));
            let data: Table = std::fs::read_to_string(&p).unwrap().parse().unwrap();
            seats(&data, &p)
        };
        assert_eq!(load("").unwrap().agent_for("a", "C1", "dm"), None);
        let both = load("model = \"sonnet\"\neffort = \"medium\"").unwrap();
        assert_eq!(both.agent_for("a", "C1", "dm"), Some(("sonnet".into(), "medium".into())));
        assert!(load("model = \"sonnet\"").unwrap_err().0.contains("together"));
        assert!(load("effort = \"low\"").unwrap_err().0.contains("together"));
        assert!(load("model = \"opus\"\neffort = \"xhigh\"").unwrap_err().0.contains("above the limit"));
    }

    #[test]
    fn accounts_resolve_secret_files_and_skip_mira_and_tonys_own() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join("bot"), " xoxb-1 \n").unwrap();
        let p = write(
            tmp.path(),
            r#"
[mira]
url = "https://mira.test"
token = "t"
[account.opal]
platform = "slack"
token_file = "bot"
app_token = "xapp-1"
[account.tony-slack]
platform = "slack"
source = "mira"
[account.teams]
platform = "teams"
source = "mira"
[account.discord-bot]
platform = "discord"
token = "d"
trusted_chats = ["42"]
[account.tony-gmail]
platform = "gmail"
address = "me@example.com"
token = "app-password"
[account.tony-own]
platform = "slack"
token = "xoxc-1"
[bucket.drive]
kind = "google_drive"
[seat.old]
account = "opal"
"#,
        );
        let got = load(&p).unwrap();
        assert_eq!(got.accounts.iter().map(|a| a.name.as_str()).collect::<Vec<_>>(), ["discord-bot", "opal"]);
        let opal = got.accounts.iter().find(|a| a.name == "opal").unwrap();
        assert_eq!((opal.token.as_str(), opal.trust.as_str()), ("xoxb-1", "cooperative"));
        let bot = got.accounts.iter().find(|a| a.name == "discord-bot").unwrap();
        assert_eq!(bot.trust, "untrusted");
        assert_eq!((bot.level_of("42"), bot.level_of("7")), (Level::Trusted, Level::Untrusted));
        assert_eq!(got.notes.len(), 6, "{:?}", got.notes);
    }

}
