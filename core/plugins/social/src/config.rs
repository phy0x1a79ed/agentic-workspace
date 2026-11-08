//! `social.toml`: accounts, buckets and seats (`config.py`).
//!
//! The file is `$AWM_DIR/social.toml`, or `AWM_SOCIAL_CONFIG`. Ansible writes it on alcor, and
//! social never rewrites it. A secret is inline or a `<key>_file`, relative to the file's folder,
//! with all whitespace stripped. Entries this build does not drive (the mira source, Teams,
//! OneDrive, and the keeper's `[seat.*]` and `[keeper]` tables) are skipped with a log line and
//! stay in the file.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use awm_trust::Level;
use toml::{Table, Value};

pub const KNOWN_PLATFORMS: [&str; 4] = ["discord", "slack", "gmail", "teams"];
pub const KNOWN_BUCKET_KINDS: [&str; 2] = ["google_drive", "onedrive"];
pub const LEVELS: [&str; 3] = ["untrusted", "cooperative", "trusted"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigError(pub String);

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ConfigError {}

type R<T> = Result<T, ConfigError>;

fn err<T>(msg: impl Into<String>) -> R<T> {
    Err(ConfigError(msg.into()))
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct AccountConfig {
    pub name: String,
    pub platform: String,
    pub token: String,
    pub app_token: Option<String>,
    pub cookie: Option<String>,
    pub creds_cmd: Option<String>,
    pub display_name: Option<String>,
    pub address: Option<String>,
    pub enabled: bool,
    pub singleton: bool,
    pub trust: String,
    pub trusted_chats: Vec<String>,
}

impl AccountConfig {
    /// "bot" or "user", from the platform and the token: a display hint, never authz.
    pub fn kind(&self) -> &'static str {
        if self.platform == "gmail" || self.token.starts_with("xoxp-") || self.token.starts_with("xoxc-") {
            return "user";
        }
        if self.platform == "slack" && self.creds_cmd.is_some() {
            return "user";
        }
        "bot"
    }

    /// The level of one chat: trusted when it is in `trusted_chats`, else the account's.
    pub fn level_of(&self, chat: &str) -> Level {
        if self.trusted_chats.iter().any(|c| c == chat) {
            return Level::Trusted;
        }
        self.trust.parse::<Level>().ok().unwrap_or(Level::Untrusted)
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct BucketConfig {
    pub name: String,
    pub kind: String,
    pub root: String,
    pub client_id: String,
    pub client_secret: String,
    pub refresh_token: String,
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
}

/// One named chat that is a seat, overriding any rule.
#[derive(Debug, Clone, PartialEq)]
pub struct SeatChat {
    pub account: String,
    pub channel: String,
    pub node: String,
    pub level: Level,
    pub scope: String,
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

impl SeatsConfig {
    /// The seat a chat is, if any: a named chat first, else the first rule of its kind.
    pub fn seat_for(&self, account: &str, channel: &str, kind: &str, sender: &str) -> Option<(String, Level, String)> {
        if let Some(c) = self.chats.iter().find(|c| c.account == account && c.channel == channel) {
            return Some((c.node.clone(), c.level, c.scope.clone()));
        }
        self.rules
            .iter()
            .find(|r| r.account == account && r.kind == kind && !r.except.iter().any(|e| e == channel || e == sender))
            .map(|r| (r.node.clone(), r.level, r.scope.clone()))
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
/// accounts up, as a bad `[bucket]` table leaves them up.
#[derive(Debug, Clone, Default)]
pub struct Loaded {
    pub accounts: Vec<AccountConfig>,
    pub buckets: Vec<BucketConfig>,
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

fn parse(path: &Path) -> R<Option<Table>> {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return err(format!("could not parse {}: {e}", path.display())),
    };
    text.parse::<Table>().map(Some).map_err(|e| ConfigError(format!("could not parse {}: {e}", path.display())))
}

/// Read the whole file. An account error fails the load. A bucket or seats error is a note.
pub fn load(path: &Path) -> R<Loaded> {
    let mut out = Loaded::default();
    let Some(data) = parse(path)? else { return Ok(out) };
    let (accounts, notes) = accounts(&data, path)?;
    out.accounts = accounts;
    out.notes = notes;
    match buckets(&data, path) {
        Ok((b, notes)) => {
            out.buckets = b;
            out.notes.extend(notes);
        }
        Err(e) => out.notes.push(format!("buckets refused: {e}")),
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

/// A secret from an inline value or a `<label>_file`.
fn secret(section: &Table, key: &str, cfg: &Path, label: &str, required: bool) -> R<Option<String>> {
    let p = cfg.display();
    if let Some(inline) = section.get(key) {
        return match inline.as_str().map(str::trim) {
            Some(s) if !s.is_empty() => Ok(Some(s.to_string())),
            _ => err(format!("{p}: {label} must be a non-empty string")),
        };
    }
    if let Some(file) = section.get(&format!("{key}_file")) {
        let Some(f) = file.as_str().map(str::trim).filter(|s| !s.is_empty()) else {
            return err(format!("{p}: {label}_file must be a path string"));
        };
        let mut file = PathBuf::from(f);
        if !file.is_absolute() {
            file = cfg.parent().unwrap_or(Path::new(".")).join(file);
        }
        let raw = std::fs::read_to_string(&file).map_err(|e| ConfigError(format!("{p}: {label}_file {} could not be read: {e}", file.display())))?;
        let s: String = raw.split_whitespace().collect();
        if s.is_empty() {
            return err(format!("{p}: {label}_file {} is empty", file.display()));
        }
        return Ok(Some(s));
    }
    if required {
        return err(format!("{p}: {label} (or {label}_file) is required"));
    }
    Ok(None)
}

fn opt_str(section: &Table, key: &str, cfg: &Path, label: &str) -> R<Option<String>> {
    match section.get(key) {
        None => Ok(None),
        Some(Value::String(s)) => Ok(Some(s.trim().to_string())),
        Some(_) => err(format!("{}: {label}.{key} must be a string when present", cfg.display())),
    }
}

fn opt_bool(section: &Table, key: &str, default: bool, cfg: &Path, label: &str) -> R<bool> {
    match section.get(key) {
        None => Ok(default),
        Some(Value::Boolean(b)) => Ok(*b),
        Some(_) => err(format!("{}: {label}.{key} must be a boolean", cfg.display())),
    }
}

fn tables<'a>(data: &'a Table, key: &str, cfg: &Path, what: &str) -> R<Vec<(&'a String, &'a Table)>> {
    let Some(v) = data.get(key) else { return Ok(vec![]) };
    let Some(t) = v.as_table() else {
        return err(format!("{}: [{key}] must be a table of named {what}", cfg.display()));
    };
    t.iter()
        .map(|(name, s)| s.as_table().map(|s| (name, s)).ok_or_else(|| ConfigError(format!("{}: [{key}.{name}] must be a table", cfg.display()))))
        .collect()
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
        let creds_cmd = match s.get("creds_cmd") {
            None => None,
            Some(Value::String(c)) if !c.trim().is_empty() => Some(c.trim().to_string()),
            Some(_) => return err(format!("{p}: {label}.creds_cmd must be a non-empty string")),
        };
        let token = secret(s, "token", cfg, &format!("{label}.token"), creds_cmd.is_none())?.unwrap_or_default();
        let app_token = secret(s, "app_token", cfg, &format!("{label}.app_token"), false)?;
        let cookie = secret(s, "cookie", cfg, &format!("{label}.cookie"), false)?;
        if platform == "slack" && creds_cmd.is_none() && token.starts_with("xoxc-") && cookie.is_none() {
            return err(format!("{p}: {label} an xoxc- session token requires a paired cookie (or cookie_file), or use creds_cmd"));
        }
        let display_name = opt_str(s, "display_name", cfg, &label)?.filter(|d| !d.is_empty());
        let address = opt_str(s, "address", cfg, &label)?.filter(|a| !a.is_empty());
        if platform == "gmail" && address.is_none() {
            return err(format!("{p}: {label}.address (the mailbox email) is required for gmail accounts"));
        }
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
            cookie,
            creds_cmd,
            display_name,
            address,
            enabled,
            singleton,
            trust,
            trusted_chats,
        });
    }
    Ok((out, notes))
}

fn buckets(data: &Table, cfg: &Path) -> R<(Vec<BucketConfig>, Vec<String>)> {
    let p = cfg.display();
    let (mut out, mut notes) = (vec![], vec![]);
    for (name, s) in tables(data, "bucket", cfg, "buckets")? {
        let label = format!("[bucket.{name}]");
        let kind = s.get("kind").and_then(Value::as_str).unwrap_or("");
        if !KNOWN_BUCKET_KINDS.contains(&kind) {
            return err(format!("{p}: {label}.kind must be one of {}", KNOWN_BUCKET_KINDS.join(", ")));
        }
        if kind == "onedrive" || s.get("source").is_some() {
            notes.push(format!("{label} is a mira bucket: skipped, this build does not drive mira"));
            continue;
        }
        let root = opt_str(s, "root", cfg, &label)?.unwrap_or_default();
        let client_id = match s.get("client_id").and_then(Value::as_str).map(str::trim) {
            Some(c) if !c.is_empty() => c.to_string(),
            _ => return err(format!("{p}: {label}.client_id is required for google_drive buckets")),
        };
        let client_secret = secret(s, "client_secret", cfg, &format!("{label}.client_secret"), true)?.unwrap_or_default();
        let refresh_token = secret(s, "refresh_token", cfg, &format!("{label}.refresh_token"), true)?.unwrap_or_default();
        out.push(BucketConfig { name: name.trim().to_string(), kind: kind.into(), root, client_id, client_secret, refresh_token });
    }
    Ok((out, notes))
}

const SEATS_KEYS: [&str; 8] = ["project", "model", "effort", "paused", "notice", "docs", "rule", "chat"];
const RULE_KEYS: [&str; 6] = ["account", "kind", "node", "level", "scope", "except"];
const CHAT_KEYS: [&str; 5] = ["account", "channel", "node", "level", "scope"];

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
    if matches!(out.effort.as_str(), "xhigh" | "max") {
        return err("[seats].effort: high is the upper limit");
    }
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
        out.rules.push(SeatRule { account: need(r, "account", &label)?, kind, node: need(r, "node", &label)?, level: level(r, &label)?, scope: need(r, "scope", &label)?, except });
    }
    let mut seen = BTreeMap::new();
    for (i, c) in list("chat")?.iter().enumerate() {
        let label = format!("[[seats.chat]] {}", i + 1);
        keys(c, &CHAT_KEYS, &label)?;
        let chat = SeatChat { account: need(c, "account", &label)?, channel: need(c, "channel", &label)?, node: need(c, "node", &label)?, level: level(c, &label)?, scope: need(c, "scope", &label)? };
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
    fn accounts_resolve_secret_files_and_skip_mira() {
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
[seat.old]
account = "opal"
"#,
        );
        let got = load(&p).unwrap();
        assert_eq!(got.accounts.iter().map(|a| a.name.as_str()).collect::<Vec<_>>(), ["discord-bot", "opal"]);
        let opal = got.accounts.iter().find(|a| a.name == "opal").unwrap();
        assert_eq!((opal.token.as_str(), opal.kind(), opal.trust.as_str()), ("xoxb-1", "bot", "cooperative"));
        let bot = got.accounts.iter().find(|a| a.name == "discord-bot").unwrap();
        assert_eq!(bot.trust, "untrusted");
        assert_eq!((bot.level_of("42"), bot.level_of("7")), (Level::Trusted, Level::Untrusted));
        assert_eq!(got.notes.len(), 3, "{:?}", got.notes);
    }

    #[test]
    fn account_errors_keep_the_python_text() {
        let tmp = tempfile::tempdir().unwrap();
        let p = write(tmp.path(), "[account.x]\nplatform = \"irc\"\n");
        assert!(load(&p).unwrap_err().0.ends_with("[account.x].platform must be one of discord, slack, gmail, teams"));
        let p = write(tmp.path(), "[account.x]\nplatform = \"gmail\"\ntoken = \"t\"\n");
        assert!(load(&p).unwrap_err().0.ends_with("[account.x].address (the mailbox email) is required for gmail accounts"));
        let p = write(tmp.path(), "[account.x]\nplatform = \"slack\"\ntoken = \"xoxc-1\"\n");
        assert!(load(&p).unwrap_err().0.contains("requires a paired cookie"));
        let p = write(tmp.path(), "[account.x]\nplatform = \"slack\"\ntoken_file = \"missing\"\n");
        assert!(load(&p).unwrap_err().0.contains("could not be read"));
    }

    #[test]
    fn a_bad_seats_table_keeps_the_accounts() {
        let tmp = tempfile::tempdir().unwrap();
        let p = write(tmp.path(), "[account.x]\nplatform = \"discord\"\ntoken = \"t\"\n[seats]\neffort = \"max\"\n");
        let got = load(&p).unwrap();
        assert_eq!(got.accounts.len(), 1);
        assert!(got.seats.rules.is_empty() && got.seats.paused);
        assert!(got.notes.iter().any(|n| n.contains("high is the upper limit")));
    }

    #[test]
    fn seats_match_a_named_chat_before_a_rule() {
        let tmp = tempfile::tempdir().unwrap();
        let p = write(
            tmp.path(),
            r#"
[account.opal]
platform = "slack"
token = "xoxb-1"
[account.discord-bot]
platform = "discord"
token = "d"
[seats]
paused = false
[[seats.rule]]
account = "opal"
kind = "dm"
node = "altair"
level = "cooperative"
scope = "seats/{slug}"
except = ["USLACKBOT"]
[[seats.chat]]
account = "opal"
channel = "D1"
node = "altair"
level = "trusted"
scope = "seats/tony-slack"
[[seats.chat]]
account = "discord-bot"
channel = "99"
node = "mizar"
level = "trusted"
scope = "seats/tony-discord"
"#,
        );
        let got = load(&p).unwrap();
        let s = &got.seats;
        assert!(!s.paused);
        assert_eq!(s.seat_for("opal", "D1", "dm", "U1"), Some(("altair".into(), Level::Trusted, "seats/tony-slack".into())));
        assert_eq!(s.seat_for("opal", "D2", "dm", "U2").unwrap().1, Level::Cooperative);
        assert_eq!(s.seat_for("opal", "D3", "dm", "USLACKBOT"), None);
        assert_eq!(s.seat_for("opal", "C1", "channel", "U2"), None);
        assert_eq!(s.notice_for(&got.accounts), Some(Notice { account: "discord-bot".into(), channel: "99".into() }));
    }
}
