//! auth.db, read in place at schema 7: the edge's signing secret, the device tokens, and the
//! session-lifetime settings in `awm_settings`.
//!
//! The signing secret is minted once, when the database itself is created, and never again:
//! a fresh secret would end every live session on the node, and a missing database or secret
//! where one existed is damage to stop on, not to paper over (ports/README.md, state safety 6).
//! A device token's secret is never stored, only its SHA-256. A revoked row stays, with
//! `revoked_at` set, so a listing still names it.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use awm_store::rusqlite::{self, OptionalExtension, Row};
use awm_store::settings::{Contract, ContractError};
use awm_store::{InitOutcome, Migration, Store, StoreError};
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use rand::RngCore;
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{json, Value};

pub const DB_NAME: &str = "auth.db";
pub const SCHEMA_VERSION: i64 = 7;
/// Set to `1` on a new node, so that the first start creates auth.db and its secret.
pub const INIT_ENV: &str = "AWM_AUTH_INIT";

const SCHEMA: &str = "
CREATE TABLE awm_secret (
    id     INTEGER PRIMARY KEY CHECK (id = 1),
    secret TEXT NOT NULL
);
CREATE TABLE awm_device_tokens (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    label         TEXT NOT NULL,
    secret_sha256 TEXT NOT NULL,
    created_at    REAL NOT NULL,
    last_used_at  REAL,
    revoked_at    REAL,
    scope         TEXT
);
";

const DEVICE_TOKENS_TABLE: &str = "\
CREATE TABLE IF NOT EXISTS awm_device_tokens (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    label         TEXT NOT NULL,
    secret_sha256 TEXT NOT NULL,
    created_at    REAL NOT NULL,
    last_used_at  REAL,
    revoked_at    REAL
);
";

/// Python's chain, kept so that an old copy restored by hand still opens.
const MIGRATIONS: [Migration; 6] = [
    Migration {
        from: 1,
        sql: "\
CREATE TABLE IF NOT EXISTS awm_users (
    username   TEXT PRIMARY KEY,
    pw_hash    TEXT NOT NULL,
    pw_salt    TEXT NOT NULL,
    pw_params  TEXT NOT NULL,
    created_at REAL NOT NULL,
    disabled   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS awm_login_fail (
    key          TEXT PRIMARY KEY,
    fails        INTEGER NOT NULL DEFAULT 0,
    locked_until REAL NOT NULL DEFAULT 0,
    last_at      REAL NOT NULL DEFAULT 0
);
",
    },
    // v3 added the retired Penpot credential table. The step stays so the chain holds.
    Migration { from: 2, sql: "" },
    Migration { from: 3, sql: DEVICE_TOKENS_TABLE },
    Migration { from: 4, sql: "ALTER TABLE awm_device_tokens ADD COLUMN scope TEXT;" },
    // A token was live only while its owner existed and was enabled, so the rest are revoked
    // before the owners vanish: dropping the table must not bring a disabled user's token back.
    Migration {
        from: 5,
        sql: "\
UPDATE awm_device_tokens
   SET revoked_at = (julianday('now') - 2440587.5) * 86400.0
 WHERE revoked_at IS NULL
   AND (scope IS NULL
        OR username NOT IN (SELECT username FROM awm_users WHERE disabled = 0));
DROP TABLE IF EXISTS awm_users;
DROP TABLE IF EXISTS awm_penpot;
",
    },
    Migration {
        from: 6,
        sql: "\
DROP TABLE IF EXISTS awm_credentials;
DROP TABLE IF EXISTS awm_login_fail;
",
    },
];

#[derive(Debug, thiserror::Error)]
pub enum AuthStoreError {
    #[error("{} is missing: refusing to start with no signing secret (set {INIT_ENV}=1 on a new node)", .0.display())]
    Missing(PathBuf),
    #[error("{} holds no signing secret: refusing to mint a fresh one, which would end every session", .0.display())]
    NoSecret(PathBuf),
    #[error(transparent)]
    Store(#[from] StoreError),
    #[error(transparent)]
    Settings(#[from] ContractError),
}

impl From<rusqlite::Error> for AuthStoreError {
    fn from(e: rusqlite::Error) -> Self {
        AuthStoreError::Store(StoreError::from(e))
    }
}

// -- settings ---------------------------------------------------------------------------------

pub const SETTINGS_TITLE: &str = "Authentication";

/// The settings page's knobs, as pydantic validated them: a number, a numeric string or a bool.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct AuthSettings {
    #[serde(default = "default_ttl_hours", deserialize_with = "lax_float")]
    pub session_ttl_hours: f64,
    #[serde(default = "default_max_days", deserialize_with = "lax_float")]
    pub max_session_days: f64,
}

fn default_ttl_hours() -> f64 {
    24.0
}

fn default_max_days() -> f64 {
    30.0
}

impl Default for AuthSettings {
    fn default() -> Self {
        AuthSettings { session_ttl_hours: default_ttl_hours(), max_session_days: default_max_days() }
    }
}

fn lax_float<'de, D: Deserializer<'de>>(d: D) -> Result<f64, D::Error> {
    use serde::de::Error;
    match Value::deserialize(d)? {
        Value::Number(n) => n.as_f64().ok_or_else(|| D::Error::custom("Input should be a valid number")),
        Value::Bool(b) => Ok(f64::from(u8::from(b))),
        Value::String(s) => awm_pycompat::pystr::strip(&s)
            .parse::<f64>()
            .map_err(|_| D::Error::custom("Input should be a valid number, unable to parse string as a number")),
        _ => Err(D::Error::custom("Input should be a valid number")),
    }
}

/// The JSON Schema pydantic published for the settings, byte for byte.
pub fn settings_schema() -> Value {
    json!({
        "description": "User-settable auth knobs.",
        "properties": {
            "session_ttl_hours": {
                "default": 24.0,
                "description": "Sliding session lifetime: a browser cookie is refreshed to this many hours ahead on each authenticated request.",
                "title": "Session Ttl Hours",
                "type": "number"
            },
            "max_session_days": {
                "default": 30.0,
                "description": "Hard ceiling on total session age; sliding refresh stops past this and re-login is required.",
                "title": "Max Session Days",
                "type": "number"
            }
        },
        "title": "AuthSettings",
        "type": "object"
    })
}

// -- device tokens ----------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub struct DeviceRow {
    pub id: String,
    pub username: String,
    pub label: String,
    pub secret_sha256: String,
    pub created_at: f64,
    pub last_used_at: Option<f64>,
    pub revoked_at: Option<f64>,
    pub scope: Option<String>,
}

const DEVICE_COLUMNS: &str = "id, username, label, secret_sha256, created_at, last_used_at, revoked_at, scope";

fn device_row(r: &Row<'_>) -> rusqlite::Result<DeviceRow> {
    Ok(DeviceRow {
        id: r.get(0)?,
        username: r.get(1)?,
        label: r.get(2)?,
        secret_sha256: r.get(3)?,
        created_at: r.get(4)?,
        last_used_at: r.get(5)?,
        revoked_at: r.get(6)?,
        scope: r.get(7)?,
    })
}

/// The open auth.db.
#[derive(Clone)]
pub struct AuthStore {
    store: Store,
    contract: std::sync::Arc<Contract<AuthSettings>>,
}

impl std::fmt::Debug for AuthStore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AuthStore").field("path", &self.path()).finish()
    }
}

impl AuthStore {
    /// `<state dir>/auth.db`.
    pub fn path_in(state_dir: &Path) -> PathBuf {
        state_dir.join(DB_NAME)
    }

    /// Open auth.db at `path`, migrating an old copy in place. A missing file is refused
    /// unless `create`, and only a file created here gets a fresh signing secret.
    pub fn open(path: impl AsRef<Path>, create: bool) -> Result<AuthStore, AuthStoreError> {
        let path = path.as_ref().to_path_buf();
        let existed = path.exists();
        if !existed && !create {
            return Err(AuthStoreError::Missing(path));
        }
        let store = Store::open(&path)?;
        let outcome = store.init(SCHEMA, SCHEMA_VERSION, &MIGRATIONS)?;
        let contract = std::sync::Arc::new(Contract::new(store.clone(), SETTINGS_TITLE, settings_schema()));
        let auth = AuthStore { store, contract };
        if !existed && matches!(outcome, InitOutcome::Created { .. }) {
            auth.mint_secret()?;
        }
        Ok(auth)
    }

    /// [`AuthStore::open`], then [`AuthStore::secret`]: the start refuses a database with no
    /// secret.
    pub fn open_for_start(path: impl AsRef<Path>, create: bool) -> Result<AuthStore, AuthStoreError> {
        let auth = AuthStore::open(path, create)?;
        auth.secret()?;
        Ok(auth)
    }

    pub fn path(&self) -> &Path {
        self.store.path()
    }

    /// The underlying pooled database.
    pub fn db(&self) -> &Store {
        &self.store
    }

    fn mint_secret(&self) -> Result<(), AuthStoreError> {
        let mut raw = [0u8; 48];
        rand::rngs::OsRng.fill_bytes(&mut raw);
        let secret = URL_SAFE_NO_PAD.encode(raw);
        self.store.blocking_write(|tx| {
            tx.execute("INSERT OR IGNORE INTO awm_secret (id, secret) VALUES (1, ?1)", [&secret]).map_err(StoreError::from)
        })?;
        tracing::warn!("auth: minted the signing secret of a new {}", self.path().display());
        Ok(())
    }

    /// The HMAC signing secret. Never minted here: a database without one fails closed.
    pub fn secret(&self) -> Result<String, AuthStoreError> {
        let got: Option<String> = self.store.blocking_read(|c| {
            c.query_row("SELECT secret FROM awm_secret WHERE id = 1", [], |r| r.get(0)).optional().map_err(StoreError::from)
        })?;
        got.filter(|s| !s.is_empty()).ok_or_else(|| AuthStoreError::NoSecret(self.path().into()))
    }

    pub fn device_insert(
        &self,
        id: &str,
        username: &str,
        label: &str,
        secret_sha256: &str,
        scope: Option<&str>,
        now: f64,
    ) -> Result<DeviceRow, AuthStoreError> {
        self.store.blocking_write(|tx| {
            tx.execute(
                "INSERT INTO awm_device_tokens (id, username, label, secret_sha256, created_at, scope) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                rusqlite::params![id, username, label, secret_sha256, now, scope],
            )
            .map_err(StoreError::from)
        })?;
        self.device_get(id)?.ok_or_else(|| AuthStoreError::Store(StoreError::Sqlite(rusqlite::Error::QueryReturnedNoRows)))
    }

    pub fn device_get(&self, id: &str) -> Result<Option<DeviceRow>, AuthStoreError> {
        Ok(self.store.blocking_read(|c| {
            c.query_row(&format!("SELECT {DEVICE_COLUMNS} FROM awm_device_tokens WHERE id = ?1"), [id], device_row)
                .optional()
                .map_err(StoreError::from)
        })?)
    }

    fn devices(&self, filter: &str) -> Result<Vec<DeviceRow>, AuthStoreError> {
        let sql = format!("SELECT {DEVICE_COLUMNS} FROM awm_device_tokens {filter} ORDER BY created_at, id");
        Ok(self.store.blocking_read(|c| {
            let mut stmt = c.prepare(&sql).map_err(StoreError::from)?;
            let rows = stmt.query_map([], device_row).map_err(StoreError::from)?;
            rows.collect::<rusqlite::Result<Vec<_>>>().map_err(StoreError::from)
        })?)
    }

    /// Every token, oldest first, revoked ones included.
    pub fn device_list(&self) -> Result<Vec<DeviceRow>, AuthStoreError> {
        self.devices("")
    }

    /// Every unrevoked token, oldest first.
    pub fn device_live(&self) -> Result<Vec<DeviceRow>, AuthStoreError> {
        self.devices("WHERE revoked_at IS NULL")
    }

    /// Mark a token revoked, keeping the first `revoked_at`. Returns whether the token exists.
    pub fn device_revoke(&self, id: &str, now: f64) -> Result<bool, AuthStoreError> {
        self.store.blocking_write(|tx| {
            tx.execute(
                "UPDATE awm_device_tokens SET revoked_at = ?1 WHERE id = ?2 AND revoked_at IS NULL",
                rusqlite::params![now, id],
            )
            .map_err(StoreError::from)
        })?;
        Ok(self.device_get(id)?.is_some())
    }

    /// Move `last_used_at` forward to each reported time, never back. Returns the rows changed.
    pub fn device_touch(&self, used: &BTreeMap<String, f64>) -> Result<usize, AuthStoreError> {
        if used.is_empty() {
            return Ok(0);
        }
        Ok(self.store.blocking_write(|tx| {
            let mut changed = 0;
            for (id, at) in used {
                changed += tx
                    .execute(
                        "UPDATE awm_device_tokens SET last_used_at = ?1 WHERE id = ?2 AND (last_used_at IS NULL OR last_used_at < ?1)",
                        rusqlite::params![at, id],
                    )
                    .map_err(StoreError::from)?;
            }
            Ok::<usize, StoreError>(changed)
        })?)
    }

    /// The session-lifetime settings: defaults merged with the stored row, read fresh.
    pub fn settings(&self) -> Result<AuthSettings, AuthStoreError> {
        Ok(self.contract.load()?)
    }

    /// The settings contract, for `config_get` and `config_set`.
    pub fn contract(&self) -> &Contract<AuthSettings> {
        &self.contract
    }

    /// `VACUUM` the database, so that its free pages drop old plaintext.
    pub fn vacuum(&self) -> Result<(), AuthStoreError> {
        let conn = self.store.connection()?;
        conn.execute_batch("VACUUM")?;
        Ok(())
    }
}
