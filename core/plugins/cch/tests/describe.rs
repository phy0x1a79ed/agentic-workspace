//! What the verb table projects onto the gateway: the describe against the Python baseline, the
//! contract coverage, the manifest file, and the budgets the `cx` shell half depends on.

use std::path::PathBuf;

use cch::verbs::{specs, Spec, CONTRACT_VERBS, OPTIONAL_VERBS, TOOL};
use serde_json::{json, Value};

/// `claim.MOVE_TIMEOUT_S` in the Python cch: how long a claim waits for the warm session to move.
const MOVE_TIMEOUT_S: f64 = 6.0;

fn baseline() -> Value {
    serde_json::from_str(include_str!("../../../../docs/plugin-redux/ports/baseline/harness-cc.describe.json")).unwrap()
}

fn plugin_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn cx_script() -> String {
    std::fs::read_to_string(plugin_dir().join("bin").join("cx")).unwrap()
}

fn claim() -> Spec {
    specs().into_iter().find(|s| s.verb == "pool_claim").unwrap()
}

/// A param as `describe` shows it: its type only when it is not a string.
fn as_described(param: &Value) -> Value {
    let mut p = param.clone();
    if p["type"] == "string" {
        p.as_object_mut().unwrap().remove("type");
    }
    p
}

/// Every difference between the served verbs and the baseline, as `<verb>: <what>`.
fn differences() -> Vec<String> {
    let base = baseline();
    let base_verbs = base["verbs"].as_array().unwrap();
    let ours = specs();
    let mut diffs = vec![];
    let mut names: Vec<&str> = ours.iter().map(|s| s.verb).collect();
    let mut base_names: Vec<&str> = base_verbs.iter().map(|v| v["verb"].as_str().unwrap()).collect();
    names.sort_unstable();
    base_names.sort_unstable();
    if names != base_names {
        diffs.push(format!("*: verbs {names:?}, baseline {base_names:?}"));
    }
    for spec in &ours {
        let Some(b) = base_verbs.iter().find(|v| v["verb"] == spec.verb) else { continue };
        if b["description"] != spec.description {
            diffs.push(format!("{}: description", spec.verb));
        }
        if b.get("surfaces").is_some() {
            diffs.push(format!("{}: surfaces", spec.verb));
        }
        // Ours must be the baseline's params in its order, less any it drops.
        let mut left: Vec<Value> = b["params"].as_array().unwrap().clone();
        let mut from = 0;
        for p in spec.params.as_array().unwrap().iter().map(as_described) {
            match left[from..].iter().position(|q| *q == p) {
                Some(i) => {
                    left.remove(from + i);
                    from += i;
                }
                None => diffs.push(format!("{}: param {} changed or moved", spec.verb, p["name"])),
            }
        }
        diffs.extend(left.iter().map(|q| format!("{}: drops param {}", spec.verb, q["name"].as_str().unwrap())));
    }
    diffs
}

/// The listed changes against the baseline. `start` drops its duplicate `task_id`, `role` and
/// `suggest`. The rows of `list` gain `session_id`, `parent`, `alive`, `transcript` and `task_id`,
/// which the describe does not show: `verbs.rs` tests them.
#[test]
fn the_describe_differs_from_the_baseline_only_by_the_listed_changes() {
    let want = ["start: drops param task_id", "start: drops param role", "start: drops param suggest"];
    assert_eq!(differences(), want);
}

#[test]
fn the_timeouts_match_the_python_manifest() {
    let want: [(&str, Option<u64>); 29] = [
        ("platform", None),
        ("resolve", Some(30)),
        ("find", Some(30)),
        ("status", Some(30)),
        ("list", None),
        ("send", Some(60)),
        ("interrupt", Some(30)),
        ("start", Some(1800)),
        ("stop", Some(30)),
        ("resume", Some(90)),
        ("set", Some(120)),
        ("compact", Some(60)),
        ("mode", Some(60)),
        ("remote_control", Some(60)),
        ("mcp", Some(60)),
        ("link", Some(30)),
        ("adopt", Some(30)),
        ("revive_pinned", Some(600)),
        ("pending", None),
        ("probe", Some(30)),
        ("live_session_ids", Some(30)),
        ("caller_of_token", Some(10)),
        ("pool_claim", Some(9)),
        ("pool_seed", Some(60)),
        ("pool_remove", Some(120)),
        ("daemon_status", None),
        ("daemon_ensure", Some(60)),
        ("daemon_hold", Some(60)),
        ("daemon_release", Some(60)),
    ];
    let got: Vec<(&str, Option<u64>)> = specs().iter().map(|s| (s.verb, s.timeout)).collect();
    assert_eq!(got, want);
}

// --- tests/test_manifest_surface.py ---

#[test]
fn test_every_function_projects_into_the_hidden_harness_tool() {
    for s in specs() {
        let flat = format!("{TOOL}_{}", s.verb);
        assert_eq!(flat.split_once('_'), Some((TOOL, s.verb)), "the gateway folds {flat} on its first underscore");
    }
}

#[test]
fn test_the_service_is_hidden_and_implements_the_harness_contract() {
    let toml = std::fs::read_to_string(plugin_dir().join("awm-plugin.toml")).unwrap();
    let tools: Vec<&str> = toml.split("[[tool]]").skip(1).collect();
    assert_eq!(tools.len(), 1, "{toml}");
    let lines: Vec<&str> = tools[0].lines().map(str::trim).collect();
    for want in [r#"name = "harness-cc""#, r#"tier = "off""#, r#"implements = ["harness@1"]"#] {
        assert!(lines.contains(&want), "{want} not in {toml}");
    }
    assert!(!tools[0].contains("uuid"));
}

#[test]
fn test_it_serves_every_contract_verb_and_lists_its_optional_ones() {
    let names: Vec<&str> = specs().iter().map(|s| s.verb).collect();
    assert!(CONTRACT_VERBS.iter().all(|v| names.contains(v)));
    let mut optional: Vec<&str> = names.into_iter().filter(|v| !CONTRACT_VERBS.contains(v)).collect();
    let mut listed = OPTIONAL_VERBS.to_vec();
    optional.sort_unstable();
    listed.sort_unstable();
    assert_eq!(optional, listed);
}

/// The default thirty-second ceiling is far too long to sit in front of a terminal, and the
/// manifest budget is the only one the gateway enforces.
#[test]
fn test_a_claim_answers_faster_than_the_client_would_wait() {
    assert!(claim().timeout.unwrap() <= 15);
}

/// Below the move's own timeout the gateway would abort a claim that was about to succeed, and
/// the session would be spent for nothing.
#[test]
fn test_the_claim_budget_covers_a_move_that_is_merely_slow() {
    assert!(claim().timeout.unwrap() as f64 > MOVE_TIMEOUT_S);
}

#[test]
fn test_the_shell_half_invokes_the_published_verb() {
    let src = cx_script();
    assert!(src.contains("POST /invoke ") && src.contains("harness-cc_pool_claim"));
}

/// Cutting the read off early turns a slow warm start into a cold one.
#[test]
fn test_the_shell_half_waits_out_the_service_budget() {
    let src = cx_script();
    let wait: u64 = src.split("CX_WAIT=${CX_WAIT:-").nth(1).unwrap().split('}').next().unwrap().parse().unwrap();
    assert!(wait >= claim().timeout.unwrap());
}

/// A branch that does not `exec` leaves this script in the process tree for the whole session,
/// which is the one thing it exists not to do.
#[test]
fn test_the_shell_half_hands_the_terminal_over_on_every_path() {
    let src = cx_script();
    let launches: Vec<&str> =
        src.lines().map(str::trim).filter(|l| l.contains(r#""$CX_CLAUDE""#) && !l.starts_with('#')).collect();
    assert!(!launches.is_empty(), "the script never launches Claude Code");
    for line in launches {
        assert!(line.contains("exec "), "{line}");
    }
}

// --- tests/test_lifecycle.py ---

/// The cx manifest's verbs live on under `harness-cc`, with no `wake`, no `effect` on any verb and
/// no `tier` anywhere.
#[test]
fn test_the_manifest_drops_status() {
    let names: Vec<&str> = specs().iter().map(|s| s.verb).collect();
    for cx in ["start", "list", "stop", "resume", "adopt", "probe", "pool_claim", "pool_seed", "pool_remove"] {
        assert!(names.contains(&cx), "{cx}");
    }
    assert!(!names.contains(&"wake"));
    for s in specs() {
        let text = format!("{} {}", s.description, s.params);
        assert!(!text.contains("\"effect\"") && !text.contains("tier"), "{}", s.verb);
    }
}

/// A refactor of the shared component fails here rather than in a terminal.
#[test]
fn test_the_protocol_component_still_has_what_the_claim_reaches_for() {
    use awm_harness::claude::pty::{self, DaemonError, DaemonLane};

    let lane = DaemonLane::from_parts("/x", "t", "s", 1, vec![]);
    let _ = (&lane.sock, &lane.auth, &lane.session_id, &lane.repl_pid, &lane.dec_modes);
    let _connect = pty::connect;
    let _error: Option<DaemonError> = None;
    let _open = <cch::lane::Host as cch::lane::Opener>::open_writer;
    assert_eq!(json!(lane.repl_pid), json!(1));
}
