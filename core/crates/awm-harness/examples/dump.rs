//! Print what the readers see in a Claude home, for a parity diff against another reader.
//! Usage: dump <claude home> <pid>...

use awm_harness::claude::{live, records, roster, ClaudeHome};
use serde_json::{json, Map, Value};

fn main() {
    let mut args = std::env::args().skip(1);
    let root = args.next().expect("a claude home");
    let home = ClaudeHome::at(&root, format!("{root}/../.claude.json"));
    let jobs: Vec<Value> = roster::load(&home).into_iter().map(|j| serde_json::to_value(j).unwrap()).collect();
    let mut pids = Map::new();
    for pid in args.filter_map(|p| p.parse::<u32>().ok()) {
        pids.insert(pid.to_string(), json!({
            "session_id": records::session_by_pid(&home, pid).and_then(|r| r.get("sessionId").cloned()),
            "job_of_pid": records::job_of_pid(&home, pid),
            "pid_bridge_id": records::pid_bridge_id(&home, pid),
        }));
    }
    let out = json!({
        "jobs": jobs,
        "daemon_pid": roster::daemon_pid(&home),
        "live_session_ids": live::live_session_ids(&home).map_err(|e| e.to_string()),
        "pids": pids,
    });
    println!("{}", serde_json::to_string_pretty(&out).unwrap());
}
