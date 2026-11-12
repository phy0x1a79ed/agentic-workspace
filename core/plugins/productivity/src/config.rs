//! `productivity.toml`: Tony's own accounts, his drives, and the nodes that may call.
//!
//! The file is `$AWM_DIR/productivity.toml`, or `AWM_PRODUCTIVITY_CONFIG`. Ansible writes it on
//! alcor, and productivity never rewrites it. `callers` defaults to no node at all, so a missing
//! file serves nobody.

use std::path::{Path, PathBuf};

use awm_chat::config::{err, opt_bool, opt_str, parse, secret, tables, R};
use toml::{Table, Value};

#[derive(Debug, Clone, Default, PartialEq)]
pub struct AccountConfig {
    pub name: String,
    /// `gmail` or `slack`.
    pub platform: String,
    pub token: String,
    pub cookie: Option<String>,
    pub creds_cmd: Option<String>,
    pub display_name: Option<String>,
    /// The mailbox of a gmail account.
    pub address: Option<String>,
    pub enabled: bool,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct BucketConfig {
    pub name: String,
    pub kind: String,
    pub root: String,
    /// A onedrive bucket's site in the browser config, `onedrive` unless named.
    pub site: String,
    pub client_id: String,
    pub client_secret: String,
    pub refresh_token: String,
}

#[derive(Debug, Clone, Default)]
pub struct Loaded {
    /// The nodes whose sessions may call. Every other node, this one included, is refused.
    pub callers: Vec<String>,
    pub accounts: Vec<AccountConfig>,
    pub buckets: Vec<BucketConfig>,
    /// One line per entry skipped.
    pub notes: Vec<String>,
}

pub fn config_file(awm_dir: &Path) -> PathBuf {
    match std::env::var("AWM_PRODUCTIVITY_CONFIG") {
        Ok(p) if !p.trim().is_empty() => PathBuf::from(p.trim()),
        _ => awm_dir.join("productivity.toml"),
    }
}

/// Read the whole file. Any error fails the load: a half-read config could widen `callers`.
pub fn load(path: &Path) -> R<Loaded> {
    let mut out = Loaded::default();
    let Some(data) = parse(path)? else { return Ok(out) };
    out.callers = match data.get("callers") {
        None => vec![],
        Some(Value::Array(a)) if a.iter().all(Value::is_str) => a.iter().map(|v| v.as_str().unwrap().trim().to_string()).filter(|n| !n.is_empty()).collect(),
        Some(_) => return err(format!("{}: callers must be a list of node names", path.display())),
    };
    (out.accounts, out.notes) = accounts(&data, path)?;
    out.buckets = buckets(&data, path)?;
    Ok(out)
}

fn accounts(data: &Table, cfg: &Path) -> R<(Vec<AccountConfig>, Vec<String>)> {
    let p = cfg.display();
    let (mut out, mut notes) = (vec![], vec![]);
    for (name, s) in tables(data, "account", cfg, "accounts")? {
        let label = format!("[account.{name}]");
        let platform = s.get("platform").and_then(Value::as_str).unwrap_or("");
        match platform {
            "gmail" | "slack" => {}
            "teams" | "discord" => {
                notes.push(format!("{label}: {platform} is read through the browser, not an account here: skipped"));
                continue;
            }
            _ => return err(format!("{p}: {label}.platform must be gmail or slack")),
        }
        let creds_cmd = match s.get("creds_cmd") {
            None => None,
            Some(Value::String(c)) if !c.trim().is_empty() => Some(c.trim().to_string()),
            Some(_) => return err(format!("{p}: {label}.creds_cmd must be a non-empty string")),
        };
        let token = secret(s, "token", cfg, &format!("{label}.token"), creds_cmd.is_none())?.unwrap_or_default();
        if token.starts_with("xoxb-") {
            return err(format!("{p}: {label} is a bot token; bots belong in social"));
        }
        let cookie = secret(s, "cookie", cfg, &format!("{label}.cookie"), false)?;
        if platform == "slack" && creds_cmd.is_none() && token.starts_with("xoxc-") && cookie.is_none() {
            return err(format!("{p}: {label} an xoxc- session token requires a paired cookie (or cookie_file), or use creds_cmd"));
        }
        let address = opt_str(s, "address", cfg, &label)?.filter(|a| !a.is_empty());
        if platform == "gmail" && address.is_none() {
            return err(format!("{p}: {label}.address (the mailbox email) is required for gmail accounts"));
        }
        out.push(AccountConfig {
            name: name.trim().to_string(),
            platform: platform.into(),
            token,
            cookie,
            creds_cmd,
            display_name: opt_str(s, "display_name", cfg, &label)?.filter(|d| !d.is_empty()),
            address,
            enabled: opt_bool(s, "enabled", true, cfg, &label)?,
        });
    }
    Ok((out, notes))
}

fn buckets(data: &Table, cfg: &Path) -> R<Vec<BucketConfig>> {
    let p = cfg.display();
    let mut out = vec![];
    for (name, s) in tables(data, "bucket", cfg, "buckets")? {
        let label = format!("[bucket.{name}]");
        let name = name.trim().to_string();
        let root = opt_str(s, "root", cfg, &label)?.unwrap_or_default();
        match s.get("kind").and_then(Value::as_str).unwrap_or("") {
            "google_drive" => {}
            "onedrive" => {
                let site = opt_str(s, "site", cfg, &label)?.unwrap_or_default();
                out.push(BucketConfig { name, kind: "onedrive".into(), root, site, ..Default::default() });
                continue;
            }
            _ => return err(format!("{p}: {label}.kind must be google_drive or onedrive")),
        }
        let client_id = match s.get("client_id").and_then(Value::as_str).map(str::trim) {
            Some(c) if !c.is_empty() => c.to_string(),
            _ => return err(format!("{p}: {label}.client_id is required for google_drive buckets")),
        };
        out.push(BucketConfig {
            name,
            kind: "google_drive".into(),
            root,
            site: String::new(),
            client_id,
            client_secret: secret(s, "client_secret", cfg, &format!("{label}.client_secret"), true)?.unwrap_or_default(),
            refresh_token: secret(s, "refresh_token", cfg, &format!("{label}.refresh_token"), true)?.unwrap_or_default(),
        });
    }
    Ok(out)
}


#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn callers_accounts_and_buckets_load_and_a_bot_token_is_refused() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join("pw"), " app pw \n").unwrap();
        let p = tmp.path().join("productivity.toml");
        std::fs::write(
            &p,
            r#"
callers = ["mizar", "capella", "lyra"]
[account.gmail]
platform = "gmail"
address = "me@example.com"
token_file = "pw"
[account.slack]
platform = "slack"
token = "xoxc-1"
cookie = "xoxd-1"
[account.teams]
platform = "teams"
[bucket.drive]
kind = "google_drive"
client_id = "id"
client_secret = "s"
refresh_token = "r"
[bucket.one]
kind = "onedrive"
root = "/personal/me/Documents"
"#,
        )
        .unwrap();
        let got = load(&p).unwrap();
        assert_eq!(got.callers, ["mizar", "capella", "lyra"]);
        assert_eq!(got.accounts.iter().map(|a| (a.name.as_str(), a.token.as_str())).collect::<Vec<_>>(), [("gmail", "apppw"), ("slack", "xoxc-1")]);
        assert_eq!(got.buckets.iter().map(|b| (b.kind.as_str(), b.root.as_str())).collect::<Vec<_>>(), [("google_drive", ""), ("onedrive", "/personal/me/Documents")]);
        assert_eq!(got.notes.len(), 1, "{:?}", got.notes);

        std::fs::write(&p, "[account.bot]\nplatform = \"slack\"\ntoken = \"xoxb-1\"\n").unwrap();
        assert!(load(&p).unwrap_err().0.contains("bots belong in social"));
        assert!(load(&tmp.path().join("missing.toml")).unwrap().callers.is_empty(), "no file, no callers");
    }
}
