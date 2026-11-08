//! The harness's model route in `$DSH_HOME/settings.yaml`: seeded once, and steerable from awm.
//!
//! The file is the user's and the harness's. [`Settings::ensure`] adds a missing section and
//! otherwise changes nothing. Every write takes the `<file>.lock` sibling that the fork's
//! `dsh-settings-file` writes under, and never steals it. A write re-renders the parsed document,
//! so comments do not survive. The route names the key's environment variable, never the key.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use serde_yaml::{Mapping, Value as Yaml};

pub const PROVIDER_ID: &str = "openrouter";
pub const API_KEY_ENV: &str = "OPENROUTER_API_KEY";
/// Settings sections are keyed by plugin id in the composed profile (`dsh --profile web --dump-config`).
pub const PLUGIN_KEY: &str = "llm-pi-ai";
pub const DEFAULT_MODEL_KEY: &str = "agent-default-model";
pub const BASE_URL: &str = "https://openrouter.ai/api/v1";
pub const LOCK_TIMEOUT: Duration = Duration::from_secs(2);

/// What the model picker offers: the workspace's habitual default, a stronger DeepSeek and a
/// non-DeepSeek fallback.
pub const MODELS: [&str; 3] = ["deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro", "anthropic/claude-sonnet-5"];

#[derive(Debug, thiserror::Error)]
pub enum SettingsError {
    /// The document could not be read or written. Nothing was partly written.
    #[error("{0}")]
    Unavailable(String),
    /// The request itself is wrong, such as an undeclared model.
    #[error("{0}")]
    Value(String),
}

pub struct Settings {
    pub file: PathBuf,
    pub lock_timeout: Duration,
}

struct Lock(PathBuf);

impl Drop for Lock {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

fn key(k: &str) -> Yaml {
    Yaml::String(k.into())
}

fn ids_of(provider: Option<&Yaml>) -> Vec<String> {
    provider
        .and_then(|p| p.get("models"))
        .and_then(Yaml::as_sequence)
        .map(|models| models.iter().filter_map(|m| m.get("id").and_then(yaml_text)).filter(|s| !s.is_empty()).collect())
        .unwrap_or_default()
}

fn yaml_text(v: &Yaml) -> Option<String> {
    match v {
        Yaml::String(s) => Some(s.clone()),
        Yaml::Number(n) => Some(n.to_string()),
        Yaml::Bool(b) => Some(b.to_string()),
        _ => None,
    }
}

fn truthy(v: Option<&Yaml>) -> Option<&Yaml> {
    v.filter(|v| match v {
        Yaml::Null => false,
        Yaml::Mapping(m) => !m.is_empty(),
        Yaml::Sequence(s) => !s.is_empty(),
        Yaml::String(s) => !s.is_empty(),
        Yaml::Bool(b) => *b,
        _ => true,
    })
}

/// The model ids the OpenRouter route advertises, in declared order.
pub fn declared_models(doc: &Mapping) -> Vec<String> {
    let providers = truthy(doc.get(PLUGIN_KEY)).and_then(|p| truthy(p.get("providers")));
    ids_of(providers.and_then(|p| p.get(PROVIDER_ID)))
}

/// The default model selection, as the harness reads it.
pub fn selection(doc: &Mapping) -> Value {
    match doc.get(DEFAULT_MODEL_KEY) {
        Some(v @ Yaml::Mapping(_)) => serde_json::to_value(v).unwrap_or_else(|_| json!({})),
        _ => json!({}),
    }
}

/// The provider block the seed declares.
pub fn provider_block() -> Yaml {
    let models: Vec<Yaml> = MODELS.iter().map(|m| serde_yaml::to_value(json!({"id": m})).expect("yaml")).collect();
    let mut block = Mapping::new();
    block.insert(key("apiKeyEnv"), key(API_KEY_ENV));
    block.insert(key("api"), key("openai-completions"));
    block.insert(key("baseURL"), key(BASE_URL));
    let mut compat = Mapping::new();
    compat.insert(key("supportsDeveloperRole"), Yaml::Bool(false));
    compat.insert(key("maxTokensField"), key("max_tokens"));
    block.insert(key("compat"), Yaml::Mapping(compat));
    block.insert(key("models"), Yaml::Sequence(models));
    Yaml::Mapping(block)
}

fn mapping_at<'a>(parent: &'a mut Mapping, k: &str, file: &Path) -> Result<&'a mut Mapping, SettingsError> {
    let slot = parent.entry(key(k)).or_insert_with(|| Yaml::Mapping(Mapping::new()));
    if slot.is_null() {
        *slot = Yaml::Mapping(Mapping::new());
    }
    slot.as_mapping_mut().ok_or_else(|| SettingsError::Unavailable(format!("{} section {k} is not a mapping", file.display())))
}

impl Settings {
    pub fn new(file: PathBuf) -> Self {
        Self { file, lock_timeout: LOCK_TIMEOUT }
    }

    pub fn lock_path(&self) -> PathBuf {
        let mut name = self.file.file_name().unwrap_or_default().to_os_string();
        name.push(".lock");
        self.file.with_file_name(name)
    }

    /// Take the document lock: exclusive create with backoff, and on timeout give up rather
    /// than remove a lock that a live writer may hold. Blocks; call it off the async runtime.
    fn lock(&self) -> Result<Lock, SettingsError> {
        let lock = self.lock_path();
        if let Some(dir) = self.file.parent() {
            std::fs::create_dir_all(dir).map_err(|e| SettingsError::Unavailable(format!("cannot lock {}: {e}", lock.display())))?;
        }
        let deadline = Instant::now() + self.lock_timeout;
        let mut delay = Duration::from_millis(10);
        loop {
            use std::os::unix::fs::OpenOptionsExt;
            match std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(&lock) {
                Ok(_) => return Ok(Lock(lock)),
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                    if Instant::now() >= deadline {
                        return Err(SettingsError::Unavailable(format!(
                            "{} is held; the harness is mid-write, or an earlier writer crashed and the lock needs removing by hand",
                            lock.display()
                        )));
                    }
                    std::thread::sleep(delay);
                    delay = (delay * 2).min(Duration::from_millis(200));
                }
                Err(e) => return Err(SettingsError::Unavailable(format!("cannot lock {}: {e}", lock.display()))),
            }
        }
    }

    /// The document, or an empty one when there is no file. A file that does not parse is an
    /// error, never an empty document: treating it as empty is how a write deletes it.
    pub fn read(&self) -> Result<Mapping, SettingsError> {
        let f = self.file.display();
        let text = match std::fs::read_to_string(&self.file) {
            Ok(t) => t,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Mapping::new()),
            Err(e) => return Err(SettingsError::Unavailable(format!("cannot read {f}: {e}"))),
        };
        match serde_yaml::from_str::<Yaml>(&text) {
            Ok(Yaml::Null) => Ok(Mapping::new()),
            Ok(Yaml::Mapping(m)) => Ok(m),
            Ok(_) => Err(SettingsError::Unavailable(format!("{f} top level is not a mapping"))),
            Err(e) => Err(SettingsError::Unavailable(format!("{f} does not parse: {e}"))),
        }
    }

    /// Render and rename into place, mode 0600, so a reader never sees half a document.
    fn write(&self, doc: &Mapping) -> Result<(), SettingsError> {
        let fail = |e: &dyn std::fmt::Display| SettingsError::Unavailable(format!("cannot write {}: {e}", self.file.display()));
        let dir = self.file.parent().unwrap_or(Path::new("."));
        std::fs::create_dir_all(dir).map_err(|e| fail(&e))?;
        let text = serde_yaml::to_string(doc).map_err(|e| fail(&e))?;
        let mut tmp = tempfile::Builder::new().prefix(".settings-").suffix(".yaml").tempfile_in(dir).map_err(|e| fail(&e))?;
        tmp.write_all(text.as_bytes()).map_err(|e| fail(&e))?;
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(tmp.path(), std::fs::Permissions::from_mode(0o600)).map_err(|e| fail(&e))?;
        tmp.persist(&self.file).map_err(|e| fail(&e.error))?;
        Ok(())
    }

    /// Fill in the missing sections in place; answer which ones were added.
    fn seed(&self, doc: &mut Mapping) -> Result<Vec<&'static str>, SettingsError> {
        let mut seeded = vec![];
        let providers = mapping_at(mapping_at(doc, PLUGIN_KEY, &self.file)?, "providers", &self.file)?;
        if !providers.contains_key(PROVIDER_ID) {
            providers.insert(key(PROVIDER_ID), provider_block());
            seeded.push("provider");
        }
        // Declaring a route is not selecting it: the profile's own default needs a key this
        // workspace does not hold.
        if !doc.contains_key(DEFAULT_MODEL_KEY) {
            let sel: Mapping = [(key("provider"), key(PROVIDER_ID)), (key("model"), key(MODELS[0]))].into_iter().collect();
            doc.insert(key(DEFAULT_MODEL_KEY), Yaml::Mapping(sel));
            seeded.push("default_model");
        }
        Ok(seeded)
    }

    /// Add the OpenRouter provider and the default selection when absent. A document this
    /// cannot read is left alone and reported.
    pub fn ensure(&self) -> Value {
        let out = (|| {
            let _lock = self.lock()?;
            let mut doc = self.read()?;
            let seeded = self.seed(&mut doc)?;
            if !seeded.is_empty() {
                self.write(&doc)?;
            }
            Ok::<_, SettingsError>(seeded)
        })();
        match out {
            Ok(seeded) => {
                if !seeded.is_empty() {
                    tracing::info!("dsh: seeded {} in {}", seeded.join(" and "), self.file.display());
                }
                json!({"changed": !seeded.is_empty(), "seeded": seeded, "provider": PROVIDER_ID, "models": MODELS})
            }
            Err(e) => {
                tracing::warn!("dsh: leaving {} alone — {e}", self.file.display());
                json!({"changed": false, "error": e.to_string()})
            }
        }
    }

    /// Point the default selection at `model` on the OpenRouter route, editing the selection
    /// in place so a `reasoningEffort` the harness wrote survives. `declare` adds an id the
    /// route does not advertise yet.
    pub fn set_default_model(&self, model: &str, declare: bool, reasoning: Option<&str>) -> Result<Value, SettingsError> {
        let model = model.trim();
        if model.is_empty() {
            return Err(SettingsError::Value("model is required".into()));
        }
        let before = {
            let _lock = self.lock()?;
            let mut doc = self.read()?;
            self.seed(&mut doc)?;
            let known = declared_models(&doc);
            if !known.iter().any(|k| k == model) {
                if !declare {
                    let list = if known.is_empty() { "none".to_string() } else { known.join(", ") };
                    return Err(SettingsError::Value(format!(
                        "'{model}' is not declared on the {PROVIDER_ID} route ({list}). Pass declare=true to add it."
                    )));
                }
                let providers = mapping_at(mapping_at(&mut doc, PLUGIN_KEY, &self.file)?, "providers", &self.file)?;
                let provider = mapping_at(providers, PROVIDER_ID, &self.file)?;
                let models = provider.entry(key("models")).or_insert_with(|| Yaml::Sequence(vec![]));
                if !models.is_sequence() {
                    *models = Yaml::Sequence(vec![]);
                }
                let id: Mapping = [(key("id"), key(model))].into_iter().collect();
                models.as_sequence_mut().expect("a sequence").push(Yaml::Mapping(id));
            }
            let before = selection(&doc);
            let selected = mapping_at(&mut doc, DEFAULT_MODEL_KEY, &self.file)?;
            selected.insert(key("provider"), key(PROVIDER_ID));
            selected.insert(key("model"), key(model));
            if let Some(r) = reasoning.map(str::trim).filter(|r| !r.is_empty()) {
                selected.insert(key("reasoningEffort"), key(r));
            }
            self.write(&doc)?;
            before
        };
        tracing::info!("dsh: default model {} -> {model}", before.get("model").and_then(serde_json::Value::as_str).unwrap_or("None"));
        let doc = self.read()?;
        let now = selection(&doc);
        Ok(json!({"changed": now != before, "previous": before.get("model").cloned().unwrap_or(Value::Null),
                  "provider": PROVIDER_ID, "model": model, "selection": now, "models": declared_models(&doc)}))
    }

    /// The selected provider, or None when the document cannot say.
    pub fn provider_name(&self) -> Option<String> {
        let doc = self.read().ok()?;
        selection(&doc).get("provider").and_then(Value::as_str).filter(|p| !p.is_empty()).map(str::to_string)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fresh() -> (tempfile::TempDir, Settings) {
        let dir = tempfile::tempdir().unwrap();
        let s = Settings::new(dir.path().join("home/settings.yaml"));
        (dir, s)
    }

    fn read(s: &Settings) -> Yaml {
        serde_yaml::from_str(&std::fs::read_to_string(&s.file).unwrap()).unwrap()
    }

    fn put(s: &Settings, doc: Value) {
        std::fs::create_dir_all(s.file.parent().unwrap()).unwrap();
        std::fs::write(&s.file, serde_yaml::to_string(&doc).unwrap()).unwrap();
    }

    fn y(v: Value) -> Yaml {
        serde_yaml::to_value(v).unwrap()
    }

    #[test]
    fn a_missing_file_is_seeded_once_with_the_credential_by_env_name() {
        let (_d, s) = fresh();
        assert_eq!(s.ensure()["changed"], true);
        let doc = read(&s);
        let p = &doc[PLUGIN_KEY]["providers"]["openrouter"];
        assert_eq!(p["baseURL"], y(json!(BASE_URL)));
        assert_eq!(p["api"], y(json!("openai-completions")));
        assert_eq!(doc[DEFAULT_MODEL_KEY], y(json!({"provider": "openrouter", "model": MODELS[0]})));
        let before = std::fs::read_to_string(&s.file).unwrap();
        assert!(before.contains("apiKeyEnv: OPENROUTER_API_KEY") && !before.contains("apiKey:"));
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(&s.file).unwrap().permissions().mode() & 0o777, 0o600);
        assert_eq!(s.ensure()["changed"], false);
        assert_eq!(std::fs::read_to_string(&s.file).unwrap(), before);
    }

    #[test]
    fn what_the_user_set_survives_the_seed() {
        let (_d, s) = fresh();
        let mine = json!({"providers": {"openrouter": {"models": [{"id": "mine"}]}}});
        put(&s, json!({PLUGIN_KEY: mine.clone(), "theme": "dark", DEFAULT_MODEL_KEY: {"provider": "openrouter", "model": "mine"}}));
        assert_eq!(s.ensure()["changed"], false);
        let doc = read(&s);
        assert_eq!((&doc[PLUGIN_KEY], &doc["theme"], &doc[DEFAULT_MODEL_KEY]["model"]), (&y(mine), &y(json!("dark")), &y(json!("mine"))));
        // The two sections seed independently.
        put(&s, json!({DEFAULT_MODEL_KEY: {"provider": "openrouter", "model": "mine"}}));
        assert_eq!(s.ensure()["seeded"], json!(["provider"]));
    }

    #[test]
    fn an_unparseable_file_is_reported_and_never_rewritten() {
        let (_d, s) = fresh();
        std::fs::create_dir_all(s.file.parent().unwrap()).unwrap();
        std::fs::write(&s.file, "this: [is: not: yaml\n").unwrap();
        let res = s.ensure();
        assert_eq!(res["changed"], false);
        assert!(res["error"].as_str().is_some_and(|e| !e.is_empty()));
        let err = s.set_default_model("anything", true, None).unwrap_err();
        assert!(matches!(&err, SettingsError::Unavailable(m) if m.contains("does not parse")), "{err}");
        assert_eq!(std::fs::read_to_string(&s.file).unwrap(), "this: [is: not: yaml\n");
    }

    #[test]
    fn selecting_a_model_moves_only_the_default() {
        let (_d, s) = fresh();
        s.ensure();
        let mut doc: Value = serde_json::to_value(read(&s)).unwrap();
        doc["some-other-plugin"] = json!({"kept": true});
        doc[DEFAULT_MODEL_KEY]["reasoningEffort"] = json!("high");
        put(&s, doc);
        let res = s.set_default_model(MODELS[2], false, None).unwrap();
        assert_eq!((res["changed"].clone(), res["previous"].clone()), (json!(true), json!(MODELS[0])));
        let doc = read(&s);
        assert_eq!(doc[DEFAULT_MODEL_KEY], y(json!({"provider": "openrouter", "model": MODELS[2], "reasoningEffort": "high"})));
        assert_eq!(doc["some-other-plugin"], y(json!({"kept": true})));
        assert_eq!(s.set_default_model(MODELS[2], false, None).unwrap()["changed"], false);
        s.set_default_model(MODELS[1], false, Some("low")).unwrap();
        assert_eq!(read(&s)[DEFAULT_MODEL_KEY]["reasoningEffort"], y(json!("low")));
    }

    #[test]
    fn an_undeclared_model_is_refused_unless_declared() {
        let (_d, s) = fresh();
        s.ensure();
        let err = s.set_default_model("vendor/not-listed", false, None).unwrap_err();
        assert!(matches!(&err, SettingsError::Value(m) if m.contains("not declared")), "{err}");
        assert_eq!(read(&s)[DEFAULT_MODEL_KEY]["model"], y(json!(MODELS[0])));
        let res = s.set_default_model("vendor/not-listed", true, None).unwrap();
        assert_eq!(res["model"], "vendor/not-listed");
        assert!(declared_models(&s.read().unwrap()).contains(&"vendor/not-listed".to_string()));
    }

    #[test]
    fn a_write_never_lands_while_the_harness_holds_the_lock() {
        let (_d, mut s) = fresh();
        s.ensure();
        std::fs::write(s.lock_path(), "").unwrap();
        s.lock_timeout = Duration::from_millis(50);
        let err = s.set_default_model(MODELS[1], false, None).unwrap_err();
        assert!(matches!(&err, SettingsError::Unavailable(m) if m.contains("is held")), "{err}");
        assert!(s.lock_path().exists(), "the contender must not remove somebody else's lock");
        std::fs::remove_file(s.lock_path()).unwrap();
        assert_eq!(read(&s)[DEFAULT_MODEL_KEY]["model"], y(json!(MODELS[0])));
    }
}
