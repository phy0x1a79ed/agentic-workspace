//! A project's create hook (`awm-project.toml`, `[scope] create_hook`): when it runs, what
//! it is given, and what scopes checks after it.

mod common;

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use awm_plugin_scopes::lifecycle::CreateReq;
use awm_plugin_scopes::{db, hook, Fail};
use common::{git, sha, Rig};
use serde_json::json;

const P: &str = "social";

/// A rig whose stand-in for `scope ensure` registers `register` once the hook has run, as
/// a real hook would through the gateway.
fn rig(register: Option<&'static str>) -> Rig {
    common::rig_with(move |app| {
        let db_path = app.paths.workspace.join(".awm/services/scopes/scopes.db");
        let worktree = app.paths.projects.join(P);
        app.fault = Some(Arc::new(move |point| {
            if let (Some(scope), "hooked") = (register, point) {
                let wt = worktree.join(scope).to_string_lossy().into_owned();
                db::open(&db_path)?.blocking_write(|c| {
                    db::ensure_agent(c, P, scope, &db::NewAgent { worktree: &wt, status: "active", ..db::NewAgent::default() })?;
                    Ok::<_, Fail>(())
                })?;
            }
            Ok(())
        }));
    })
}

/// The project, its main checkout, and its `awm-project.toml` with `body` under `[scope]`.
fn project(r: &Rig, body: &str) -> PathBuf {
    let bare = common::bare_project(r, P);
    let main = r.projects.join(P).join("main");
    git(&bare, &["worktree", "add", "-q", &main.to_string_lossy(), "main"]);
    if !body.is_empty() {
        std::fs::write(main.join(hook::FILE), format!("[scope]\n{body}\n")).unwrap();
    }
    main
}

fn script(main: &Path, rel: &str, body: &str) {
    let path = main.join(rel);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
}

/// A hook that logs its argv and cwd, then makes the seat's directory.
const MAKES_SEAT: &str = "echo \"argv: $*\" >> \"$(pwd)/../hook.log\"\necho \"cwd: $(pwd)\" >> \"$(pwd)/../hook.log\"\nmkdir -p \"../seats/$1\"\necho made it";

fn create(r: &Rig, scope: &str, tune: impl FnOnce(&mut CreateReq)) -> Result<awm_plugin_scopes::lifecycle::Action, Fail> {
    let mut req = CreateReq { project: P.into(), scope: Some(scope.into()), ..Default::default() };
    tune(&mut req);
    r.op.create_scope(&req)
}

fn log(r: &Rig) -> String {
    std::fs::read_to_string(r.projects.join(P).join("hook.log")).unwrap_or_default()
}

#[test]
fn the_hook_makes_a_scope_under_its_prefix() {
    let r = rig(Some("seats/ana"));
    let main = project(&r, "create_hook = \"scripts/new-seat.sh\"\ncreate_hook_prefix = \"seats/\"");
    script(&main, "scripts/new-seat.sh", MAKES_SEAT);
    let out = create(&r, "seats/ana", |q| {
        q.context = Some("Ana's seat.".into());
        q.tags = Some(vec!["seat".into()]);
    })
    .unwrap();
    assert_eq!(out.to_json(), json!({"project": P, "scope": "seats/ana", "status": "active",
        "message": "Created scope at projects/social/seats/ana with the create hook scripts/new-seat.sh ana, session 1", "session": 1, "discarded": null}));
    let canon = main.canonicalize().unwrap();
    assert_eq!(log(&r), format!("argv: ana\ncwd: {}\n", canon.display()));
    assert_eq!(sha(&r.projects.join(P).join(".bare"), "refs/heads/feat/seats/ana"), None, "the hook, not git, made the scope");
    assert_eq!(r.out.calls_to("brief_set"), [json!({"project": P, "scope": "seats/ana", "text": "Ana's seat."})]);
    assert_eq!(r.op.effective_tags(P, Some("seats/ana")).unwrap(), ["seat"]);
    assert_eq!(r.op.hygiene_status().unwrap()["creates"], json!(1));
}

#[test]
fn a_scope_outside_the_prefix_is_a_git_worktree() {
    let r = rig(None);
    let main = project(&r, "create_hook = \"scripts/new-seat.sh\"\ncreate_hook_prefix = \"seats/\"");
    script(&main, "scripts/new-seat.sh", MAKES_SEAT);
    create(&r, "work", |_| {}).unwrap();
    assert!(r.projects.join(P).join("work/.git").exists());
    assert!(log(&r).is_empty());
    let req = |s: &str| hook::find(&r.op, P, s).unwrap().map(|h| h.arg);
    assert_eq!(req("seats/bo"), Some("bo".into()));
    assert_eq!(req("seats/"), None, "the prefix alone names no scope");
    assert_eq!(req("seatsy"), None);
}

#[test]
fn without_a_prefix_every_scope_is_the_hooks() {
    let r = rig(Some("bo"));
    let main = project(&r, "create_hook = \"new.sh\"");
    script(&main, "new.sh", "mkdir -p ../bo");
    let out = create(&r, "bo", |_| {}).unwrap();
    assert!(out.message.contains("with the create hook new.sh bo"));
}

#[test]
fn git_parameters_are_refused_before_the_hook_runs() {
    let r = rig(Some("seats/ana"));
    let main = project(&r, "create_hook = \"scripts/new-seat.sh\"\ncreate_hook_prefix = \"seats/\"");
    script(&main, "scripts/new-seat.sh", MAKES_SEAT);
    let e = create(&r, "seats/ana", |q| {
        q.branch_name = Some("x".into());
        q.from_branch = Some("main".into());
    })
    .unwrap_err();
    assert_eq!(e.class, "ValueError");
    assert_eq!(e.message, "social/seats/ana is made by the project's create hook scripts/new-seat.sh, which takes no git parameters; drop from_branch, branch_name");
    assert!(log(&r).is_empty());
}

#[test]
fn a_failing_hook_quotes_its_last_lines() {
    let r = rig(None);
    let main = project(&r, "create_hook = \"fail.sh\"");
    script(&main, "fail.sh", "i=1; while [ $i -le 30 ]; do echo \"line $i\"; i=$((i+1)); done; echo oops >&2; exit 3");
    let e = create(&r, "ana", |_| {}).unwrap_err();
    assert_eq!(e.class, "RuntimeError");
    assert!(e.message.starts_with("create hook fail.sh ana failed for social/ana with exit 3; its last output:\nline 12\n"), "{}", e.message);
    assert!(e.message.ends_with("line 30\noops"), "stderr interleaves with stdout");
    assert!(!e.message.contains("line 11\n"));
}

#[test]
fn a_silent_hook_that_makes_nothing_is_a_failure() {
    let r = rig(None);
    let main = project(&r, "create_hook = \"noop.sh\"");
    script(&main, "noop.sh", "true");
    let e = create(&r, "ana", |_| {}).unwrap_err();
    assert_eq!(e.message, format!("create hook noop.sh ana exited 0 but made no directory at {}; the hook printed nothing", r.projects.join(P).join("ana").display()));
}

#[test]
fn a_hook_that_registers_no_row_fails_and_its_directory_stays() {
    let r = rig(None);
    let main = project(&r, "create_hook = \"scripts/new-seat.sh\"");
    script(&main, "scripts/new-seat.sh", "mkdir -p \"../$1\"\necho made it");
    let e = create(&r, "seats/ana", |_| {}).unwrap_err();
    assert!(e.message.starts_with("create hook scripts/new-seat.sh seats/ana exited 0 but registered no active scope social/seats/ana (the hook must run `scope ensure`); its last output:\nmade it"), "{}", e.message);
    assert!(r.projects.join(P).join("seats/ana").is_dir(), "scopes removes nothing a hook made");
}

#[test]
fn a_hook_outside_its_checkout_missing_or_not_executable_is_refused() {
    let r = rig(None);
    let main = project(&r, "create_hook = \"../escape.sh\"");
    script(&r.projects.join(P), "escape.sh", "true");
    let e = create(&r, "ana", |_| {}).unwrap_err();
    assert!(e.class == "ValueError" && e.message.contains("outside its checkout"), "{e:?}");

    std::os::unix::fs::symlink("../escape.sh", main.join("link.sh")).unwrap();
    std::fs::write(main.join(hook::FILE), "[scope]\ncreate_hook = \"link.sh\"\n").unwrap();
    assert!(create(&r, "ana", |_| {}).unwrap_err().message.contains("outside its checkout"));

    std::fs::write(main.join(hook::FILE), "[scope]\ncreate_hook = \"gone.sh\"\n").unwrap();
    assert_eq!(create(&r, "ana", |_| {}).unwrap_err().message, format!("social's create hook gone.sh does not exist in {}", main.display()));

    std::fs::write(main.join("plain.sh"), "true").unwrap();
    std::fs::write(main.join(hook::FILE), "[scope]\ncreate_hook = \"plain.sh\"\n").unwrap();
    assert_eq!(create(&r, "ana", |_| {}).unwrap_err().message, "social's create hook plain.sh is not an executable file");
    assert!(!r.projects.join(P).join("ana").exists());
}

#[test]
fn a_file_without_a_hook_changes_nothing_and_a_bad_one_is_named() {
    let r = rig(None);
    let main = project(&r, "");
    assert_eq!(hook::config(&r.op, P).unwrap(), None);
    std::fs::write(main.join(hook::FILE), "[other]\nx = 1\n").unwrap();
    assert_eq!(hook::config(&r.op, P).unwrap(), None);
    std::fs::write(main.join(hook::FILE), "[scope]\ncreate_hook_prefix = \"seats/\"\n").unwrap();
    assert_eq!(hook::config(&r.op, P).unwrap(), None);
    create(&r, "seats/ana", |_| {}).unwrap();
    assert!(r.projects.join(P).join("seats/ana/.git").exists());

    std::fs::write(main.join(hook::FILE), "[scope]\ncreate_hook = 3\n").unwrap();
    let e = hook::config(&r.op, P).unwrap_err();
    assert!(e.class == "ValueError" && e.message.ends_with("[scope] create_hook must be a non-empty string"));
    std::fs::write(main.join(hook::FILE), "[scope\n").unwrap();
    assert_eq!(hook::config(&r.op, P).unwrap_err().class, "ValueError");
}

#[test]
fn the_main_checkout_follows_the_bare_head() {
    let r = rig(None);
    let bare = common::bare_project(&r, P);
    let release = r.projects.join(P).join("rel-checkout");
    git(&bare, &["branch", "release", "main"]);
    git(&bare, &["worktree", "add", "-q", &release.to_string_lossy(), "release"]);
    std::fs::write(release.join(hook::FILE), "[scope]\ncreate_hook = \"h.sh\"\n").unwrap();
    assert_eq!(hook::config(&r.op, P).unwrap(), None, "HEAD is main, which no checkout holds");
    git(&bare, &["symbolic-ref", "HEAD", "refs/heads/release"]);
    let cfg = hook::config(&r.op, P).unwrap().unwrap();
    assert_eq!((cfg.checkout, cfg.hook, cfg.prefix), (release, "h.sh".to_string(), None));
}
