//! Node rollout: fleet order, a failed node, and a rerun.

use std::path::{Path, PathBuf};

use awm_plugin_routine::rollout::{fleetconf, restart_list, Outcome, Rollout, RESULT_MARK};

use crate::common::{git, Workspace};

const REFRESH: &str = "[refresh]\nsync = \"true\"\ncommand = \"scripts/refresh.sh\"\n";

struct T {
    _tmp: tempfile::TempDir,
    root: PathBuf,
    ws: Workspace,
    log: PathBuf,
}

/// A workspace whose release carries a refresh script that logs each call.
fn setup() -> T {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().canonicalize().unwrap();
    let ws = Workspace::new(&root.join("ws"));
    let log = root.join("refresh.log");
    let script = format!(
        "#!/bin/sh\necho \"$AWM_REFRESH_NODE restart=$AWM_REFRESH_RESTART\" >> {log}\necho '{RESULT_MARK}{{\"ok\": true, \"node\": \"mizar\"}}'\n",
        log = log.display()
    );
    ws.scope("base", &[("deploy.toml", REFRESH), ("scripts/refresh.sh", &script)], "dev");
    let base = ws.rev("feat/base");
    git(&ws.bare, &["update-ref", "refs/heads/release", &base]);
    git(&ws.proj.join("release"), &["reset", "-q", "--hard", "release"]);
    T { _tmp: tmp, root, ws, log }
}

fn rollout(t: &T, fleet: &str, restart: &str) -> Rollout {
    Rollout {
        checkout: t.ws.proj.join("release"),
        bare: t.ws.bare.clone(),
        awm_dir: t.root.join("awm"),
        node: "mizar".into(),
        fleet: fleetconf::parse(fleet, "config.toml").unwrap(),
        restart: restart_list(restart).unwrap(),
    }
}

fn lines(p: &Path) -> Vec<String> {
    std::fs::read_to_string(p).unwrap().lines().map(str::to_string).collect()
}

fn names(out: &[(String, Outcome)]) -> Vec<&str> {
    out.iter().filter(|(_, o)| matches!(o, Outcome::Failed(_))).map(|(n, _)| n.as_str()).collect()
}

#[test]
fn a_failed_node_does_not_stop_the_others_and_a_rerun_retries_it() {
    let t = setup();
    git(&t.root, &["init", "-q", "--bare", &t.root.join("peer-awm.git").to_string_lossy()]);
    let flag = t.root.join("alcor-ok");
    let peer = |name: &str, cmd: &str| format!("[fleet.push.{name}]\nurl = \"{}\"\nprojects = [\"awm\"]\nrefresh = \"{cmd}\"\n", t.root.join("peer-{project}.git").display());
    let fleet = format!(
        "[fleet]\nhome = \"mizar\"\nnodes = [\"mizar\", \"alcor\", \"lyra\"]\n{}{}",
        peer("alcor", &format!("test -e {} && echo $AWM_REFRESH_NODE >> {}", flag.display(), t.log.display())),
        peer("lyra", &format!("echo $AWM_REFRESH_NODE >> {}", t.log.display())),
    );
    let ro = rollout(&t, &fleet, "routine,cch");
    let to = t.ws.rev("release");

    let first = ro.run(&to).unwrap();
    assert_eq!(names(&first), ["alcor"]);
    assert_eq!(first.len(), 3);
    assert_eq!(lines(&t.log), ["mizar restart=routine,cch", "lyra"]);
    assert_eq!(git(&t.root.join("peer-awm.git"), &["rev-parse", "refs/mizar/release"]), to);

    std::fs::write(&flag, "").unwrap();
    assert!(names(&ro.run(&to).unwrap()).is_empty());
    assert!(lines(&t.log).contains(&"alcor".to_string()));
}

#[test]
fn a_node_whose_push_fails_counts_as_failed_and_the_run_goes_on() {
    let t = setup();
    let fleet = format!(
        "[fleet]\nhome = \"mizar\"\nnodes = [\"mizar\", \"alcor\"]\n[fleet.push.alcor]\nurl = \"{}\"\nprojects = [\"awm\"]\n",
        t.root.join("missing-{project}.git").display()
    );
    let out = rollout(&t, &fleet, "none").run(&t.ws.rev("release")).unwrap();
    assert_eq!(names(&out), ["alcor"]);
    assert_eq!(out[0], ("mizar".to_string(), Outcome::Done));
}

#[test]
fn restart_needs_names_or_none() {
    assert_eq!(restart_list("none").unwrap(), "");
    assert_eq!(restart_list(" a, b ").unwrap(), "a,b");
    assert!(restart_list(" , ").unwrap_err().contains("--restart"));
}
