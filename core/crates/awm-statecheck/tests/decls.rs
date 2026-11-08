//! Every drafted state.toml parses, and its paths land where the Python plugin keeps its state.

use std::path::{Path, PathBuf};

use awm_statecheck::Decl;

fn env(k: &str) -> Option<String> {
    (k == "HOME").then(|| "/home/u".to_string())
}

fn load(name: &str) -> Decl {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("decls").join(format!("{name}.toml"));
    let text = std::fs::read_to_string(&path).unwrap();
    Decl::parse(&text, &Path::new("/a/services").join(name), Path::new("/a"), env).unwrap()
}

#[test]
fn every_decl_parses() {
    assert!(load("fileviewer").dirs.is_empty());
    assert!(load("dashboard").dirs.is_empty());
    let r = load("routine");
    assert_eq!(r.dirs, ["/a/services/routine", "/a/land", "/a/services/events"].map(PathBuf::from));
    assert!(r.is_transient_file(Path::new("/a/land/awm/lease.json")));
    assert!(r.is_transient_file(Path::new("/a/land/awm/tickets/t1.json")));
    assert_eq!(r.extra[0].path, PathBuf::from("/home/u/.config/systemd/user/awm-routine-*"));
    let d = load("dvc");
    assert_eq!(d.extra[0].path, PathBuf::from("/a/dvc.toml"));
    let a = load("auth");
    assert_eq!(a.extra[0].path, PathBuf::from("/home/u/agentic_workspace/awm/services/httpsfront/.certs"));
    assert!(a.is_size_only(Path::new("/home/u/.config/remote-audio/ca/ca.key")));
    let o = load("operations");
    assert_eq!(o.dirs, ["/home/u/.awm/operations", "/home/u/.awm/approver"].map(PathBuf::from));
    assert!(o.is_transient_table(Path::new("/home/u/.awm/operations/relay.db"), "outbox"));
    assert!(o.extra.is_empty(), "the pending approvals are recorded, never copied");
}
