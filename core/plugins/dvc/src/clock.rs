//! routine's clock for the two jobs. dvc installs each job's routine definition (in
//! `routines/`) and keeps its timer in step with the job's `dvc_jobs` row: an enabled job is
//! scheduled at its cron as OnCalendar, with a 60 s jitter, the row's catch-up window, and
//! its last fire as the slot already served, so the handover neither fires twice nor misses
//! a window. A disabled job is unscheduled. All of it is plain calls: a routine outage shows
//! as `registered: false` and refuses nothing.

use std::collections::BTreeMap;
use std::path::Path;

use awm_plugin_sdk::Hub;
use serde_json::{json, Value};

use crate::cron;
use crate::jobs::{self, JOBS};
use crate::runs::Row;

pub const PROJECT: &str = "awm";
pub const JITTER_S: i64 = 60;

/// The `routine schedule` arguments for one job row, or None when the job is disabled.
pub fn schedule_args(spec: &jobs::JobSpec, row: &Row) -> Result<Option<Value>, cron::CronError> {
    if row.get("enabled").and_then(Value::as_i64).unwrap_or(1) == 0 {
        return Ok(None);
    }
    let on_calendar = cron::to_on_calendar(row.get("cron").and_then(Value::as_str).unwrap_or(spec.cron))?;
    let mut args = json!({"project": PROJECT, "name": spec.routine, "timer": on_calendar, "jitter_s": JITTER_S});
    let window = row.get("catchup_window_s").and_then(Value::as_i64).unwrap_or(0);
    if window > 0 {
        args["catchup_s"] = json!(window.max(JITTER_S));
    }
    let last = row.get("last_fire_at").and_then(Value::as_f64).unwrap_or(0.0);
    if last > 0.0 {
        args["last_slot"] = json!(crate::iso(last.floor()));
    }
    Ok(Some(args))
}

/// Install each job's routine and set its timer from its row. `unconfigured` is why this node
/// cannot run a backup, such as a missing dvc.toml: then no job gets a timer, since each fire
/// would fail and page. Returns one line per job.
pub async fn sync_all(hub: &Hub, plugin_dir: &Path, rows: &[Row], unconfigured: Option<&str>) -> Value {
    let mut out = serde_json::Map::new();
    for spec in &JOBS {
        let row = rows.iter().find(|r| r.get("name").and_then(Value::as_str) == Some(spec.name)).cloned().unwrap_or_default();
        out.insert(spec.name.into(), sync_one(hub, plugin_dir, spec, &row, unconfigured).await);
    }
    Value::Object(out)
}

pub async fn sync_one(hub: &Hub, plugin_dir: &Path, spec: &jobs::JobSpec, row: &Row, unconfigured: Option<&str>) -> Value {
    let def = plugin_dir.join("routines").join(format!("{}.toml", spec.routine));
    if let Err(e) = hub.call("routine", "install", json!({"project": PROJECT, "path": def.to_string_lossy()}), None).await {
        return json!({"registered": false, "error": format!("routine install: {e}")});
    }
    let args = match schedule_args(spec, row) {
        Ok(a) => a.filter(|_| unconfigured.is_none()),
        Err(e) => return json!({"registered": false, "error": e.0}),
    };
    match args {
        Some(a) => match hub.call("routine", "schedule", a.clone(), None).await {
            Ok(_) => json!({"registered": true, "timer": a["timer"]}),
            Err(e) => json!({"registered": false, "error": format!("routine schedule: {e}")}),
        },
        None => {
            let mut v = match hub.call("routine", "unschedule", json!({"project": PROJECT, "name": spec.routine}), None).await {
                Ok(_) => json!({"registered": false}),
                Err(e) => json!({"registered": false, "error": format!("routine unschedule: {e}")}),
            };
            match unconfigured {
                Some(why) => v["unconfigured"] = json!(why),
                None => v["disabled"] = json!(true),
            }
            v
        }
    }
}

/// Which job routines routine has scheduled, or an error when routine cannot say.
pub async fn registered(hub: &Hub) -> Result<BTreeMap<String, bool>, String> {
    let v = hub.call("routine", "list", json!({"project": PROJECT}), None).await.map_err(|e| e.to_string())?;
    let mut out = BTreeMap::new();
    for r in v.get("routines").and_then(Value::as_array).into_iter().flatten() {
        if let Some(name) = r.get("name").and_then(Value::as_str) {
            let on = match r.get("scheduled") {
                Some(Value::Bool(b)) => *b,
                Some(Value::Number(n)) => n.as_i64().unwrap_or(0) != 0,
                _ => false,
            };
            out.insert(name.to_string(), on);
        }
    }
    Ok(out)
}
