//! The `awm` tool's service and config verbs: what `awm services` and the settings page use.

use std::collections::BTreeMap;
use std::time::Duration;

use awm_proto::Caller;
use serde_json::{json, Value};

use crate::gateway::{ApiError, Gateway};
use crate::hub::Status;
use crate::loader::ServiceSpec;

const CONFIG_TIMEOUT: Duration = Duration::from_secs(10);

impl Gateway {
    fn spec_of(&self, name: &str) -> Option<ServiceSpec> {
        self.read_load().plugins.iter().flat_map(|p| p.services.iter()).find(|s| s.name == name).cloned()
    }

    /// One row per service: each the loaded plugins run, then each other one the hub knows.
    pub fn services_view(&self) -> Value {
        let live: BTreeMap<String, crate::hub::ServiceView> = self.hub.services().into_iter().map(|s| (s.name.clone(), s)).collect();
        let failed = self.supervisor.failed();
        let mut rows = Vec::new();
        let mut seen = Vec::new();
        for spec in self.read_load().plugins.iter().flat_map(|p| p.services.iter()) {
            seen.push(spec.name.clone());
            let hub = live.get(&spec.name);
            let status = match hub {
                Some(h) => json!(h.status),
                None if !spec.enabled => json!("disabled"),
                None => json!("stopped"),
            };
            rows.push(json!({
                "name": spec.name, "plugin": spec.plugin, "enabled": spec.enabled, "status": status,
                "id": hub.map(|h| h.id.clone()), "pid": hub.and_then(|h| h.pid), "verbs": hub.map_or(0, |h| h.verbs),
                "refusal": hub.and_then(|h| h.refusal.clone()), "mount": hub.and_then(|h| h.mount.clone()),
                "tools": spec.tools, "archive": spec.archive, "external": self.cfg.external(&spec.name).is_some(),
                "failed": failed.contains(&spec.name),
            }));
        }
        for (name, h) in live.iter().filter(|(n, _)| !seen.contains(n)) {
            rows.push(json!({
                "name": name, "plugin": h.plugin, "enabled": true, "status": h.status, "id": h.id, "pid": h.pid,
                "verbs": h.verbs, "refusal": h.refusal, "mount": h.mount, "tools": [], "archive": [],
                "external": false, "failed": false,
            }));
        }
        json!(rows)
    }

    /// `start`, `stop`, `restart`, `enable` or `disable` one service.
    pub async fn service_control(&self, verb: &str, name: &str) -> Result<Value, ApiError> {
        let Some(gw) = self.weak().upgrade() else { return Err(ApiError::new(503, "the gateway is stopping")) };
        let Some(spec) = self.spec_of(name) else { return Err(ApiError::new(404, format!("no service {name:?} on this node"))) };
        if self.cfg.external(name).is_some() {
            return Err(ApiError::new(409, format!("service {name:?} is external: its own unit runs it")));
        }
        match verb {
            "enable" | "disable" => {
                let path = self.state.join("enabled.json");
                let mut map: BTreeMap<String, bool> =
                    std::fs::read_to_string(&path).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default();
                map.insert(name.to_string(), verb == "enable");
                let text = serde_json::to_string_pretty(&map).map_err(|e| ApiError::new(500, e.to_string()))?;
                std::fs::write(&path, text).map_err(|e| ApiError::new(500, format!("{}: {e}", path.display())))?;
                let reload = self.reload().await?;
                Ok(json!({"service": name, "enabled": verb == "enable", "reload": reload}))
            }
            "start" => {
                if !spec.enabled {
                    return Err(ApiError::new(409, format!("service {name:?} is disabled; enable it")));
                }
                crate::supervisor::start(&gw, spec);
                Ok(json!({"service": name, "started": true}))
            }
            "stop" => {
                crate::supervisor::stop(&gw, &[name.to_string()]).await;
                Ok(json!({"service": name, "stopped": true}))
            }
            _ => {
                crate::supervisor::stop(&gw, &[name.to_string()]).await;
                let started = spec.enabled;
                if started {
                    crate::supervisor::start(&gw, spec);
                }
                Ok(json!({"service": name, "restarted": started}))
            }
        }
    }

    /// Every service whose ready api carries a `config` block, with its live values. A service
    /// that does not answer shows its declared schema, marked unavailable.
    pub async fn config_contracts(&self) -> Value {
        let mut out = Vec::new();
        for s in self.hub.services() {
            let api = self.hub.api_of(&s.name);
            let Some(cfg) = api.get("config").filter(|c| !c.is_null() && *c != &json!(false)) else { continue };
            let mut entry = json!({"key": s.name, "title": cfg.get("title").cloned().unwrap_or(json!(s.name)),
                                   "schema": cfg.get("schema").cloned().unwrap_or(json!({})), "values": null, "unavailable": true});
            if s.status == Status::Ready {
                if let Ok(got) = self.config_call(&s.name, "config_get", json!({})).await {
                    for k in ["title", "schema"] {
                        if let Some(v) = got.get(k) {
                            entry[k] = v.clone();
                        }
                    }
                    entry["values"] = got.get("values").cloned().unwrap_or(json!({}));
                    entry["unavailable"] = json!(false);
                }
            }
            out.push(entry);
        }
        json!({"contracts": out})
    }

    pub async fn config_set(&self, key: &str, values: Value) -> Result<Value, ApiError> {
        if self.hub.api_of(key).get("config").is_none_or(Value::is_null) {
            return Err(ApiError::new(404, format!("no config contract {key:?}")));
        }
        let saved = self.config_call(key, "config_set", json!({"values": values})).await?;
        Ok(json!({"key": key, "unavailable": false, "title": saved.get("title").cloned().unwrap_or(json!(key)),
                  "schema": saved.get("schema").cloned().unwrap_or(json!({})), "values": saved.get("values").cloned().unwrap_or(json!({}))}))
    }

    async fn config_call(&self, service: &str, function: &str, args: Value) -> Result<Value, ApiError> {
        let caller = Caller { node: self.node.clone(), lane: awm_proto::Lane::Service, trust: Some(crate::caller::TRUSTED.into()), ..Default::default() };
        let v = self.hub.call(service, function, args, None, &caller, CONFIG_TIMEOUT).await?;
        Ok(match v {
            Value::String(s) => serde_json::from_str(&s).unwrap_or(Value::String(s)),
            other => other,
        })
    }
}
