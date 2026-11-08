//! `AWM_DATA_DVC=0` provisions nothing. Its own binary, since the variable is process-wide.

mod common;

#[test]
fn the_kill_switch_provisions_no_project() {
    let w = common::dvc_project();
    std::env::set_var("AWM_DATA_DVC", "0");
    let rep = w.provision(&w.main);
    std::env::remove_var("AWM_DATA_DVC");
    assert_eq!(rep["mode"], "none");
    assert!(rep["detail"].as_str().unwrap().contains("AWM_DATA_DVC=0"));
    assert!(!w.main.join(".dvc/config.local").exists());
}
