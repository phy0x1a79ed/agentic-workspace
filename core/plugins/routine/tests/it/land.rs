//! The land queue: submit, the merger's batch, bounce, the lease and the rollout.


use std::path::{Path, PathBuf};

use awm_plugin_routine::land::json::J;
use awm_plugin_routine::land::{fleetconf, Land, RESULT_MARK};
use crate::common::{git, Workspace};

const INSTALL: &str = "[fleet]\nhome = \"mizar\"\nnodes = [\"mizar\"]\n";
const REFRESH: &str = "[refresh]\nsync = \"true\"\ncommand = \"scripts/refresh.sh\"\n";

struct T {
    _tmp: tempfile::TempDir,
    root: PathBuf,
    ws: Workspace,
}

fn setup() -> T {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().canonicalize().unwrap();
    let ws = Workspace::new(&root.join("ws"));
    T { _tmp: tmp, root, ws }
}

fn land(t: &T, fleet: &str) -> Land {
    Land::new("proj", &t.ws.root, &t.root.join("awm"), "mizar", fleetconf::parse(fleet, "config.toml").unwrap()).unwrap()
}

fn submit(ln: &Land, wt: &Path, restart: &str) -> J {
    ln.submit(wt, "Behavior: x\n", restart).unwrap()
}

fn with_refresh(t: &T) -> PathBuf {
    let log = t.root.join("refresh.log");
    let script = format!(
        "#!/bin/sh\necho \"$1 $AWM_REFRESH_NODE\" >> {log}\necho \"restart=$AWM_REFRESH_RESTART\" >> {log}.restart\necho '{RESULT_MARK}{{\"ok\": true, \"node\": \"mizar\"}}'\n",
        log = log.display()
    );
    t.ws.scope("base", &[("deploy.toml", REFRESH), ("scripts/refresh.sh", &script)], "dev");
    let base = t.ws.rev("feat/base");
    git(&t.ws.bare, &["update-ref", "refs/heads/release", &base]);
    git(&t.ws.bare, &["update-ref", "refs/heads/dev", &base]);
    git(&t.ws.proj.join("release"), &["reset", "-q", "--hard", "release"]);
    log
}

fn words(p: &Path) -> Vec<String> {
    std::fs::read_to_string(p).unwrap().split_whitespace().map(str::to_string).collect()
}

fn failed(ln: &Land) -> String {
    ln.batch().unwrap().unwrap().str("failed").to_string()
}

#[test]
fn two_branches_land_in_one_batch() {
    let t = setup();
    let log = with_refresh(&t);
    let mut ln = land(&t, INSTALL);
    let a = submit(&ln, &t.ws.scope("a", &[("a.txt", "a\n")], "release"), "none");
    let b = submit(&ln, &t.ws.scope("b", &[("b.txt", "b\n")], "release"), "none");
    assert_eq!((a.str("role"), b.str("role")), ("merger", "merger"));
    assert_eq!(ln.run_batch(a.str("ticket")).unwrap(), 0);
    let head = t.ws.rev("release");
    assert_eq!((t.ws.show("release", "a.txt"), t.ws.show("release", "b.txt")), ("a".into(), "b".into()));
    assert_eq!(t.ws.rev("dev"), head);
    assert_eq!(ln.ticket(a.str("ticket")).unwrap().str("state"), "landed");
    assert_eq!(ln.ticket(b.str("ticket")).unwrap().str("state"), "landed");
    assert_eq!(words(&log), [head.as_str(), "mizar"]);
    assert_eq!(ln.wait(b.str("ticket"), 0.01).unwrap().str("verdict"), "landed");
    assert!(ln.batch().unwrap().is_none() && ln.merger().unwrap().is_empty());
}

#[test]
fn conflict_stops_and_the_merger_resolves_it() {
    let t = setup();
    with_refresh(&t);
    let mut ln = land(&t, INSTALL);
    let a = submit(&ln, &t.ws.scope("a", &[("app.txt", "from a\n")], "release"), "none");
    submit(&ln, &t.ws.scope("b", &[("app.txt", "from b\n")], "release"), "none");
    let rel0 = t.ws.rev("release");
    assert_eq!(ln.run_batch(a.str("ticket")).unwrap(), 1);
    assert!(failed(&ln).contains("conflicts with the batch in app.txt"), "{}", failed(&ln));
    assert_eq!(t.ws.rev("release"), rel0);
    std::fs::write(ln.tree.join("app.txt"), "from a and b\n").unwrap();
    git(&ln.tree, &["add", "app.txt"]);
    git(&ln.tree, &["commit", "-q", "--no-edit"]);
    assert_eq!(ln.run_batch(a.str("ticket")).unwrap(), 0);
    assert_eq!(t.ws.show("release", "app.txt"), "from a and b");
}

#[test]
fn bounce_returns_the_branch_and_lands_the_rest() {
    let t = setup();
    with_refresh(&t);
    let mut ln = land(&t, INSTALL);
    let a = submit(&ln, &t.ws.scope("a", &[("a.txt", "a\n")], "release"), "none");
    let b = submit(&ln, &t.ws.scope("b", &[("deploy.toml", "[deploy]\ncheck = \"exit 3\"\n")], "release"), "none");
    assert_eq!(ln.run_batch(a.str("ticket")).unwrap(), 1);
    assert!(failed(&ln).contains("check: exit 3"), "{}", failed(&ln));
    ln.bounce(b.str("ticket"), "its deploy.toml breaks the check", a.str("ticket")).unwrap();
    assert_eq!(ln.run_batch(a.str("ticket")).unwrap(), 0);
    assert_eq!(t.ws.show("release", "a.txt"), "a");
    assert_eq!(t.ws.show("release", "deploy.toml"), REFRESH.trim());
    let w = ln.wait(b.str("ticket"), 0.01).unwrap();
    assert_eq!(w.str("verdict"), "bounced");
    assert!(w.str("reason").contains("breaks the check"));
}

#[test]
fn a_failed_node_stops_the_rollout_and_resumes_after_it() {
    let t = setup();
    let log = with_refresh(&t);
    git(&t.root, &["init", "-q", "--bare", &t.root.join("peer-proj.git").to_string_lossy()]);
    let flag = t.root.join("alcor-ok");
    let cmd = format!("test -e {} && echo $AWM_REFRESH_NODE >> {}", flag.display(), log.display());
    let fleet = format!(
        "[fleet]\nhome = \"mizar\"\nnodes = [\"mizar\", \"alcor\"]\n[fleet.push.alcor]\nurl = \"{}\"\nprojects = [\"proj\"]\nrefresh = \"{cmd}\"\n",
        t.root.join("peer-{project}.git").display()
    );
    let mut ln = land(&t, &fleet);
    let a = submit(&ln, &t.ws.scope("a", &[("a.txt", "a\n")], "release"), "none");
    assert_eq!(ln.run_batch(a.str("ticket")).unwrap(), 1);
    assert!(failed(&ln).contains("rollout alcor"));
    let nodes: Vec<String> = ln.batch().unwrap().unwrap().get("nodes").unwrap().as_arr().iter().map(|n| n.str("node").to_string()).collect();
    assert_eq!(nodes, ["mizar", "alcor"]);
    std::fs::write(&flag, "").unwrap();
    assert_eq!(ln.run_batch(a.str("ticket")).unwrap(), 0);
    // mizar refreshed once, alcor on the second run.
    assert_eq!(words(&log)[1..], ["mizar", "alcor"]);
    assert_eq!(git(&t.root.join("peer-proj.git"), &["rev-parse", "refs/mizar/release"]), t.ws.rev("release"));
}

#[test]
fn submit_refuses_uncommitted_work_and_release() {
    let t = setup();
    let ln = land(&t, INSTALL);
    let wt = t.ws.scope("a", &[("a.txt", "a\n")], "dev");
    std::fs::write(wt.join("a.txt"), "dirty\n").unwrap();
    let e = ln.submit(&wt, "b", "none").unwrap_err();
    assert!(e.to_string().contains("commit your work"), "{e}");
    let e = ln.submit(&t.ws.proj.join("release"), "b", "none").unwrap_err();
    assert!(e.to_string().contains("feature branch"), "{e}");
}

#[test]
fn the_batch_restarts_the_union_of_what_its_agents_name() {
    let t = setup();
    let log = with_refresh(&t);
    let mut ln = land(&t, INSTALL);
    let a = submit(&ln, &t.ws.scope("a", &[("a.txt", "a\n")], "release"), "journal,scopes");
    submit(&ln, &t.ws.scope("b", &[("b.txt", "b\n")], "release"), "scopes,search");
    submit(&ln, &t.ws.scope("c", &[("c.txt", "c\n")], "release"), "none");
    assert_eq!(ln.run_batch(a.str("ticket")).unwrap(), 0);
    assert_eq!(words(&PathBuf::from(format!("{}.restart", log.display()))), ["restart=journal,scopes,search"]);
    let e = ln.submit(&t.ws.scope("d", &[("d.txt", "d\n")], "release"), "b", " , ").unwrap_err();
    assert!(e.to_string().contains("--restart"));
}

#[test]
fn a_ticket_is_written_as_python_writes_it() {
    let t = setup();
    let ln = land(&t, INSTALL);
    let res = ln.submit(&t.ws.scope("a", &[("a.txt", "a\n")], "release"), "Behavior: x → y\n", "routine,events").unwrap();
    let text = std::fs::read_to_string(ln.tickets.join(format!("{}.json", res.str("ticket")))).unwrap();
    let keys: Vec<&str> = text.lines().filter_map(|l| l.strip_prefix(" \"")).filter_map(|l| l.split_once('"')).map(|(k, _)| k).collect();
    assert_eq!(keys, ["id", "project", "scope", "branch", "sha", "brief", "state", "restart", "submitted", "verdict", "reason", "batch"]);
    assert!(text.contains("\"brief\": \"Behavior: x \\u2192 y\\n\""), "{text}");
    assert!(text.contains("\"restart\": [\n  \"routine\",\n  \"events\"\n ]"), "{text}");
    assert!(!text.ends_with('\n'));
}
