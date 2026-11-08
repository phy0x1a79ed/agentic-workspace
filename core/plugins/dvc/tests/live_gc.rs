//! A gc dry run over every DVC-wired project of a real workspace, for the parity check
//! against Python. Run by hand: `AWM_DVC_LIVE_WS=<workspace> AWM_DVC_LIVE_OUT=<file>`
//! with `--ignored`. It deletes nothing.

use awm_plugin_dvc::scopedata::{self, GcArgs};
use awm_plugin_dvc::Paths;

#[test]
#[ignore = "reads a live workspace"]
fn live_gc_dry_run() {
    let ws = std::path::PathBuf::from(std::env::var("AWM_DVC_LIVE_WS").expect("AWM_DVC_LIVE_WS"));
    let paths = Paths { workspace: ws.clone(), awm_dir: ws.join(".awm") };
    let wired: Vec<String> = scopedata::dvc_wired_projects(&paths).into_iter().collect();
    let out = scopedata::gc(&paths, GcArgs { projects: wired.clone(), dry_run: true, keep: "all-commits".into(), exclude: vec![], accept_missing: vec![] }).unwrap();
    std::fs::write(std::env::var("AWM_DVC_LIVE_OUT").expect("AWM_DVC_LIVE_OUT"), serde_json::to_string_pretty(&serde_json::json!({"wired": wired, "gc": out})).unwrap()).unwrap();
}
