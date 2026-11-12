//! Where operations keeps its state and how alcor's approver is set up: the environment,
//! then `AWM_APPROVER_DIR/config.json`, then the defaults Python used.

use std::collections::HashMap;
use std::path::PathBuf;

use serde_json::Value;

#[derive(Debug, Clone)]
pub struct Settings {
    pub approver_dir: PathBuf,
    pub queue_db: PathBuf,
    pub relay_db: PathBuf,
    pub origin: String,
    pub bind: String,
    pub board_origins: Vec<String>,
    pub forwarded_header: String,
    pub allowed_orders: HashMap<String, Vec<String>>,
    pub rate_max: i64,
    pub peers: Vec<String>,
    pub portals: HashMap<String, String>,
    pub card_url: Option<String>,
    pub home: String,
    pub remind_s: f64,
}

fn env(k: &str) -> Option<String> {
    std::env::var(k).ok().filter(|s| !s.is_empty())
}

fn home_dir() -> PathBuf {
    PathBuf::from(env("HOME").unwrap_or_else(|| "/".into()))
}

pub fn node_name() -> String {
    let raw = env("AWM_NODE_NAME").unwrap_or_else(|| {
        let mut buf = [0u8; 256];
        // SAFETY: gethostname writes at most buf.len() bytes into a buffer we own.
        let ok = unsafe { libc::gethostname(buf.as_mut_ptr().cast(), buf.len()) } == 0;
        if !ok {
            return "localhost".into();
        }
        let end = buf.iter().position(|b| *b == 0).unwrap_or(buf.len());
        String::from_utf8_lossy(&buf[..end]).into_owned()
    });
    raw.split('.').next().unwrap_or_default().to_lowercase()
}

pub fn home_node() -> String {
    env("AWM_OPERATIONS_HOME").unwrap_or_else(|| "alcor".into())
}

fn strings(v: Option<&Value>) -> Vec<String> {
    v.and_then(Value::as_array).map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect()).unwrap_or_default()
}

impl Settings {
    pub fn from_env() -> anyhow::Result<Settings> {
        let dir = env("AWM_APPROVER_DIR").map(PathBuf::from).unwrap_or_else(|| home_dir().join(".awm/approver"));
        let cfg_file = env("AWM_APPROVER_CONFIG").map(PathBuf::from).unwrap_or_else(|| dir.join("config.json"));
        let cfg: Value = if cfg_file.exists() { serde_json::from_str(&std::fs::read_to_string(&cfg_file)?)? } else { Value::Null };
        Ok(Settings::from_parts(dir, &cfg))
    }

    pub fn from_parts(dir: PathBuf, cfg: &Value) -> Settings {
        let c = |k: &str| cfg.get(k).and_then(Value::as_str).map(str::to_string);
        let home = home_node();
        let mut allowed: HashMap<String, Vec<String>> = cfg
            .get("allowed_orders")
            .and_then(Value::as_object)
            .map(|m| m.iter().map(|(k, v)| (k.clone(), strings(Some(v)))).collect())
            .unwrap_or_default();
        allowed.entry("agent".into()).or_insert_with(|| vec![home.clone()]);
        allowed.entry("service".into()).or_insert_with(|| vec!["*".into()]);
        let board_origins = match env("AWM_APPROVER_BOARD_ORIGINS") {
            Some(s) => s.split(',').filter(|o| !o.is_empty()).map(str::to_string).collect(),
            None => strings(cfg.get("board_origins")),
        };
        Settings {
            queue_db: env("AWM_QUEUE_DB").map(PathBuf::from).unwrap_or_else(|| home_dir().join(".awm/operations/queue.db")),
            relay_db: env("AWM_OPERATIONS_RELAY_DB").map(PathBuf::from).unwrap_or_else(|| home_dir().join(".awm/operations/relay.db")),
            origin: env("AWM_APPROVER_ORIGIN").or_else(|| c("origin")).unwrap_or_else(|| "https://auth.tony-xy-liu.com".into()),
            bind: env("AWM_APPROVER_BIND").or_else(|| c("bind")).unwrap_or_else(|| "127.0.0.1:8443".into()),
            board_origins,
            forwarded_header: env("AWM_APPROVER_FORWARDED_HEADER").or_else(|| c("forwarded_header")).unwrap_or_else(|| "X-Forwarded-For".into()),
            allowed_orders: allowed,
            rate_max: cfg.get("rate_max").and_then(Value::as_i64).unwrap_or(30),
            peers: strings(cfg.get("peers")),
            portals: cfg.get("portals").and_then(Value::as_object)
                .map(|m| m.iter().filter_map(|(k, v)| v.as_str().map(|n| (k.clone(), n.to_string()))).collect()).unwrap_or_default(),
            card_url: env("AWM_BOARD_CARD_URL").or_else(|| c("card_url")),
            home,
            remind_s: env("AWM_OPERATIONS_REMIND_S").and_then(|s| s.parse().ok()).unwrap_or(3600.0),
            approver_dir: dir,
        }
    }
}
