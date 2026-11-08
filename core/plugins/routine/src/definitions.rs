//! Routine definitions: one TOML file per routine, kept in its project's repo.
//!
//! A definition names no node. It runs wherever it is installed (routines.md R36). The
//! stored form is the JSON Python's `asdict` wrote, so either side reads the other's rows.

use serde::{Deserialize, Serialize};
use toml::Value;

pub const STEP_KINDS: [&str; 2] = ["check", "condition"];
pub const DEFAULT_STEP_TIMEOUT: f64 = 600.0;

#[derive(Debug, thiserror::Error, PartialEq)]
#[error("{0}")]
pub struct DefinitionError(pub String);

fn default_kind() -> String {
    "check".into()
}
fn default_timeout() -> f64 {
    DEFAULT_STEP_TIMEOUT
}
fn default_peer() -> String {
    "local".into()
}
fn yes() -> bool {
    true
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Step {
    pub name: String,
    pub run: String,
    #[serde(default = "default_kind")]
    pub kind: String,
    #[serde(default = "default_timeout")]
    pub timeout: f64,
    /// For a condition step: the brief for the agent it dispatches when it holds.
    #[serde(default)]
    pub prompt: String,
}

/// Where a dispatched session runs. An empty scope means a fresh scope per run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AgentSpec {
    #[serde(default = "default_peer")]
    pub peer: String,
    #[serde(default)]
    pub scope: String,
    #[serde(default)]
    pub prompt: String,
}

impl Default for AgentSpec {
    fn default() -> Self {
        Self { peer: default_peer(), scope: String::new(), prompt: String::new() }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Routine {
    pub name: String,
    #[serde(default)]
    pub description: String,
    /// A systemd OnCalendar expression. Empty means no timer.
    #[serde(default)]
    pub timer: String,
    #[serde(default = "yes")]
    pub call: bool,
    #[serde(default)]
    pub steps: Vec<Step>,
    #[serde(default, deserialize_with = "null_default")]
    pub agent: AgentSpec,
    /// A routine without a fixer posts its failure as a notice and starts no agent.
    #[serde(default)]
    pub fixer: Option<AgentSpec>,
}

fn null_default<'de, D: serde::Deserializer<'de>>(d: D) -> Result<AgentSpec, D::Error> {
    Ok(Option::<AgentSpec>::deserialize(d)?.unwrap_or_default())
}

/// Python's `str()` of a TOML value, for the fields the definition reads as text.
fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Boolean(b) => if *b { "True" } else { "False" }.into(),
        Value::Integer(i) => i.to_string(),
        Value::Float(f) => format!("{f:?}"),
        other => other.to_string(),
    }
}

/// Python's truthiness of a TOML value.
fn truthy(v: &Value) -> bool {
    match v {
        Value::String(s) => !s.is_empty(),
        Value::Boolean(b) => *b,
        Value::Integer(i) => *i != 0,
        Value::Float(f) => *f != 0.0,
        Value::Array(a) => !a.is_empty(),
        Value::Table(t) => !t.is_empty(),
        Value::Datetime(_) => true,
    }
}

fn get_str(t: &toml::Table, key: &str, default: &str) -> String {
    t.get(key).map(py_str).unwrap_or_else(|| default.into())
}

fn is_name(name: &str) -> bool {
    let b = name.as_bytes();
    let ok = |c: u8, first: bool| c.is_ascii_lowercase() || c.is_ascii_digit() || (!first && c == b'-');
    !b.is_empty() && b.len() <= 63 && ok(b[0], true) && b[1..].iter().all(|c| ok(*c, false))
}

fn agent(raw: Option<&Value>, where_: &str) -> Result<AgentSpec, DefinitionError> {
    let Some(raw) = raw else { return Ok(AgentSpec::default()) };
    let Some(t) = raw.as_table() else {
        return Err(DefinitionError(format!("[{where_}] must be a table")));
    };
    let mut unknown: Vec<&str> = t.keys().map(String::as_str).filter(|k| !["peer", "scope", "prompt"].contains(k)).collect();
    if !unknown.is_empty() {
        unknown.sort();
        let list = unknown.iter().map(|k| format!("'{k}'")).collect::<Vec<_>>().join(", ");
        return Err(DefinitionError(format!("[{where_}] has unknown keys: [{list}]")));
    }
    let peer = get_str(t, "peer", "local");
    Ok(AgentSpec {
        peer: if peer.is_empty() { "local".into() } else { peer },
        scope: get_str(t, "scope", ""),
        prompt: get_str(t, "prompt", ""),
    })
}

/// Parse and validate one routine definition.
pub fn parse(text: &str, source: &str) -> Result<Routine, DefinitionError> {
    let raw: toml::Table = text.parse().map_err(|e| DefinitionError(format!("{source}: {e}")))?;
    let err = |m: String| DefinitionError(format!("{source}: {m}"));
    let empty = toml::Table::new();
    let table = |key: &str| raw.get(key).and_then(Value::as_table).unwrap_or(&empty);

    let head = table("routine");
    let name = get_str(head, "name", "").trim().to_string();
    if !is_name(&name) {
        return Err(err("[routine] name must be lowercase letters, digits and hyphens".into()));
    }
    if head.contains_key("node") || head.contains_key("peer") {
        return Err(err("a routine names no node; it runs where it is installed (R36)".into()));
    }

    let trig = table("trigger");
    let timer = get_str(trig, "timer", "").trim().to_string();
    let call = trig.get("call").map(truthy).unwrap_or(true);
    if timer.is_empty() && !call {
        return Err(err("a routine needs a timer, a call trigger or both".into()));
    }
    if timer.contains(['\n', '\r']) {
        return Err(err("[trigger] timer must be one line".into()));
    }

    let steps_raw = match raw.get("step") {
        Some(Value::Array(a)) if !a.is_empty() => a,
        _ => return Err(err("a routine needs at least one [[step]]".into())),
    };
    let mut steps: Vec<Step> = Vec::new();
    for (i, s) in steps_raw.iter().enumerate() {
        let Some(s) = s.as_table() else { return Err(err(format!("step {} must be a table", i + 1))) };
        let sname = get_str(s, "name", &format!("step-{}", i + 1));
        if steps.iter().any(|x| x.name == sname) {
            return Err(err(format!("duplicate step name '{sname}'")));
        }
        let run = get_str(s, "run", "").trim().to_string();
        if run.is_empty() {
            return Err(err(format!("step '{sname}' has no run command")));
        }
        let kind = get_str(s, "kind", "check");
        if !STEP_KINDS.contains(&kind.as_str()) {
            return Err(err(format!("step '{sname}' kind must be one of ('check', 'condition')")));
        }
        let timeout = match s.get("timeout") {
            None => DEFAULT_STEP_TIMEOUT,
            Some(Value::Integer(i)) => *i as f64,
            Some(Value::Float(f)) => *f,
            Some(Value::String(t)) => t.trim().parse().map_err(|_| err(format!("step '{sname}' timeout must be a number")))?,
            Some(_) => return Err(err(format!("step '{sname}' timeout must be a number"))),
        };
        if timeout.is_nan() || timeout <= 0.0 {
            return Err(err(format!("step '{sname}' timeout must be positive")));
        }
        steps.push(Step { name: sname, run, kind, timeout, prompt: get_str(s, "prompt", "") });
    }

    let agent_spec = agent(raw.get("agent"), "agent")?;
    let conditions: Vec<&Step> = steps.iter().filter(|s| s.kind == "condition").collect();
    if !conditions.is_empty() && agent_spec.prompt.is_empty() && !conditions.iter().all(|s| !s.prompt.is_empty()) {
        return Err(err("a condition step needs a prompt, on the step or in [agent]".into()));
    }
    let fixer = match raw.get("fixer") {
        Some(v) => Some(agent(Some(v), "fixer")?),
        None => None,
    };
    Ok(Routine { name, description: get_str(head, "description", ""), timer, call, steps, agent: agent_spec, fixer })
}

pub fn load(path: &std::path::Path) -> Result<Routine, DefinitionError> {
    let text = std::fs::read_to_string(path).map_err(|e| DefinitionError(format!("{}: {e}", path.display())))?;
    parse(&text, &path.display().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    const BASIC: &str = r#"
[routine]
name = "nightly"
description = "two steps"

[trigger]
timer = "*-*-* 03:00:00"

[[step]]
name = "one"
run = "echo one > out.txt"

[[step]]
name = "two"
run = "echo two >> out.txt"

[fixer]
peer = "alcor"
prompt = "Check the disk first."
"#;

    #[test]
    fn parse_basic() {
        let r = parse(BASIC, "<string>").unwrap();
        assert_eq!(r.name, "nightly");
        assert_eq!(r.timer, "*-*-* 03:00:00");
        assert!(r.call);
        assert_eq!(r.steps.iter().map(|s| s.name.as_str()).collect::<Vec<_>>(), ["one", "two"]);
        assert_eq!(r.fixer.as_ref().unwrap().peer, "alcor");
        let back: Routine = serde_json::from_value(serde_json::to_value(&r).unwrap()).unwrap();
        assert_eq!(back, r);
    }

    #[test]
    fn reads_the_json_python_stored() {
        let py = r#"{"name": "tick", "description": "", "timer": "hourly", "call": true, "steps": [{"name": "step-1", "run": "true", "kind": "check", "timeout": 600.0, "prompt": ""}], "agent": {"peer": "local", "scope": "", "prompt": ""}, "fixer": null}"#;
        let r: Routine = serde_json::from_str(py).unwrap();
        assert_eq!((r.steps[0].timeout, r.fixer.is_none(), r.agent.peer.as_str()), (600.0, true, "local"));
        let sparse: Routine = serde_json::from_str(r#"{"name": "r"}"#).unwrap();
        assert!(sparse.call && sparse.steps.is_empty() && sparse.agent == AgentSpec::default());
    }

    #[test]
    fn parse_rejects() {
        for (text, msg) in [
            ("[routine]\nname='X'\n[[step]]\nrun='true'", "lowercase"),
            ("[routine]\nname='a'\nnode='mizar'\n[[step]]\nrun='true'", "names no node"),
            ("[routine]\nname='a'\n[trigger]\ncall=false\n[[step]]\nrun='true'", "needs a timer"),
            ("[routine]\nname='a'", "at least one"),
            ("[routine]\nname='a'\n[[step]]\nrun='true'\nkind='maybe'", "kind must be"),
            ("[routine]\nname='a'\n[[step]]\nrun='true'\nkind='condition'", "needs a prompt"),
            ("[routine]\nname='a'\n[[step]]\nname='s'\nrun='true'\n[[step]]\nname='s'\nrun='true'", "duplicate"),
            ("[routine]\nname='a'\n[[step]]\nrun='true'\n[fixer]\nhost='x'", "unknown keys"),
            ("[routine]\nname='a'\n[[step]]\nrun='true'\ntimeout=0", "positive"),
        ] {
            let e = parse(text, "<string>").unwrap_err();
            assert!(e.0.contains(msg), "{text:?}: {e}");
        }
    }
}
