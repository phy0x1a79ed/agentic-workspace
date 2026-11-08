//! The `awm_settings` table and the opt-in config contract a plugin publishes through it.
//!
//! One row per key holds a JSON object, written with `json.dumps` bytes so the Python reads it
//! unchanged. The table is created on first use and owes nothing to the schema version.

use crate::store::{Store, StoreError};
use awm_pycompat::json::dumps;
use rusqlite::{Connection, OptionalExtension};
use serde::{de::DeserializeOwned, Serialize};
use serde_json::{json, Map, Value};

pub const DEFAULT_KEY: &str = "service_config";

fn ensure_table(conn: &Connection) -> rusqlite::Result<()> {
    conn.execute(
        "CREATE TABLE IF NOT EXISTS awm_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)",
        [],
    )?;
    Ok(())
}

/// The stored object under `key`. A missing row, bad JSON or a non-object reads as empty.
pub fn read(store: &Store, key: &str) -> Result<Map<String, Value>, StoreError> {
    let raw: Option<String> = store.blocking_read(|conn| {
        ensure_table(conn)?;
        conn.query_row("SELECT value FROM awm_settings WHERE key = ?1", [key], |r| r.get(0))
            .optional()
            .map_err(StoreError::from)
    })?;
    Ok(match raw.and_then(|s| serde_json::from_str::<Value>(&s).ok()) {
        Some(Value::Object(m)) => m,
        _ => Map::new(),
    })
}

/// Replace the object under `key`.
pub fn write(store: &Store, key: &str, values: &Map<String, Value>) -> Result<(), StoreError> {
    let body = dumps(&Value::Object(values.clone()));
    let now = crate::time::now_iso();
    store.blocking_write(|tx| {
        ensure_table(tx)?;
        tx.execute(
            "INSERT INTO awm_settings (key, value, updated_at) VALUES (?1, ?2, ?3) \
             ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            [key, body.as_str(), now.as_str()],
        )?;
        Ok(())
    })
}

/// A plugin's settings: a JSON Schema it publishes, and values stored in its own database.
///
/// `T` gives the fields, their defaults (through `#[serde(default)]`) and their order. Unknown
/// stored keys are dropped on the next save, as the Python's pydantic model drops them.
pub struct Contract<T> {
    store: Store,
    key: String,
    pub title: String,
    pub schema: Value,
    _shape: std::marker::PhantomData<T>,
}

impl<T: Serialize + DeserializeOwned> Contract<T> {
    pub fn new(store: Store, title: impl Into<String>, schema: Value) -> Self {
        Contract { store, key: DEFAULT_KEY.into(), title: title.into(), schema, _shape: Default::default() }
    }

    pub fn with_key(mut self, key: impl Into<String>) -> Self {
        self.key = key.into();
        self
    }

    /// The manifest's `config` block.
    pub fn manifest_fragment(&self) -> Value {
        json!({"title": self.title, "schema": self.schema})
    }

    /// Defaults merged with the stored values.
    pub fn load(&self) -> Result<T, ContractError> {
        let raw = read(&self.store, &self.key)?;
        Ok(serde_json::from_value(Value::Object(raw))?)
    }

    /// Merge `patch` over the stored values, validate the whole object, store and return it.
    pub fn save(&self, patch: &Map<String, Value>) -> Result<T, ContractError> {
        let mut merged = read(&self.store, &self.key)?;
        merged.extend(patch.iter().map(|(k, v)| (k.clone(), v.clone())));
        let typed: T = serde_json::from_value(Value::Object(merged))?;
        let Value::Object(values) = serde_json::to_value(&typed)? else {
            return Err(ContractError::NotAnObject);
        };
        write(&self.store, &self.key, &values)?;
        Ok(typed)
    }

    /// The `config_get` answer.
    pub fn get(&self) -> Result<Value, ContractError> {
        Ok(json!({"title": self.title, "schema": self.schema, "values": serde_json::to_value(self.load()?)?}))
    }

    /// The `config_set` answer, for `args.values`.
    pub fn set(&self, args: &Value) -> Result<Value, ContractError> {
        let patch = args.get("values").and_then(Value::as_object).cloned().unwrap_or_default();
        let saved = self.save(&patch)?;
        Ok(json!({"title": self.title, "schema": self.schema, "values": serde_json::to_value(saved)?}))
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ContractError {
    #[error(transparent)]
    Store(#[from] StoreError),
    #[error("invalid settings: {0}")]
    Invalid(#[from] serde_json::Error),
    #[error("the settings shape must serialise to an object")]
    NotAnObject,
}
