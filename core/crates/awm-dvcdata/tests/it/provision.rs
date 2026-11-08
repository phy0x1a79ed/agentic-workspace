//! The properties the data layer exists for, ported from the Python suite. Each runs in a
//! real bare repo with worktrees, because each is a property of git and the cache together.



use std::fs;
use std::path::Path;

use awm_dvcdata::pin::pin_md5;
use awm_dvcdata::provision::{self as pv, CHECKOUT_FAILED_FILE, MOUNTS_FILE, SHARED_DECLARATION};
use awm_dvcdata::wiring;
use crate::common::*;
use serde_json::json;

// --- degradation ---------------------------------------------------------------------

#[test]
fn degradation_refuses_rather_than_guesses() {
    let w = workspace();
    let rep = w.provision(&w.main);
    assert_eq!(rep["mode"], "none");
    assert!(!w.main.join(".awm").exists());
    fs::create_dir_all(w.main.join(".dvc/tmp")).unwrap();
    assert!(!pv::is_dvc_repo(&w.main), "a leftover .dvc dir is not a conversion");
    assert_eq!(w.provision(&w.main)["mode"], "none");
    let rep = pv::provision_scope_data(&w.layout, "p", "s", Path::new("s"), &w.bin);
    assert_eq!(rep["mode"], "unknown");
    assert!(rep["detail"].as_str().unwrap().contains("relative"));
}

// --- hook state ----------------------------------------------------------------------

fn merge_a_side_branch(wt: &Path) {
    git(wt, &["checkout", "-q", "-b", "side"]);
    fs::write(wt.join("side.txt"), "x\n").unwrap();
    commit_all(wt, "side");
    git(wt, &["checkout", "-q", "main"]);
    git(wt, &["merge", "-q", "--no-ff", "-m", "merge side", "side"]);
}

#[test]
fn a_failed_checkout_leaves_its_sentinel_in_the_git_dir() {
    let w = dvc_project();
    w.provision(&w.main);
    let oid = add_chunk(&w.main, "c", &[("f.txt", "v1\n")]);
    commit_all(&w.main, "chunk");
    let obj = awm_dvcdata::odb::object_path(&w.cache(), &oid);
    fs::remove_file(&obj).unwrap();
    merge_a_side_branch(&w.main);
    let sentinel = pv::state_dir(&w.main).join(CHECKOUT_FAILED_FILE);
    let text = fs::read_to_string(&sentinel).unwrap();
    assert!(text.contains("dvc checkout FAILED after merge") && text.contains("missing"), "{text}");
    assert!(w.main.join("data/c/f.txt").exists(), "a failed checkout keeps the old files");
    assert_eq!(git(&w.main, &["status", "--porcelain"]).trim(), "");
}

#[test]
fn the_hook_reads_the_mount_list_from_the_git_dir() {
    let w = dvc_project();
    w.provision(&w.main);
    add_chunk(&w.main, "hot", &[("f.txt", "hot\n")]);
    add_chunk(&w.main, "cold", &[("f.txt", "cold\n")]);
    commit_all(&w.main, "hot+cold");
    pv::write_mounts(&w.main, Some(&["data/hot".to_string()])).unwrap();
    assert_eq!(pv::read_mounts(&w.main), Some(vec!["data/hot".to_string()]));
    fs::remove_dir_all(w.main.join("data/hot")).unwrap();
    fs::remove_dir_all(w.main.join("data/cold")).unwrap();
    merge_a_side_branch(&w.main);
    assert!(w.main.join("data/hot/f.txt").is_file());
    assert!(!w.main.join("data/cold").exists(), "an unmounted chunk stays off disk");
    assert!(pv::chunk_pins(&w.main).contains(&"data/cold.dvc".to_string()));
    pv::write_mounts(&w.main, None).unwrap();
    assert_eq!(pv::read_mounts(&w.main), None);
}

// --- gates ---------------------------------------------------------------------------

#[test]
fn a_gitignored_pin_or_a_data_symlink_is_refused() {
    let w = dvc_project();
    fs::write(w.main.join(".gitignore"), "/data\n/data/**/*\n").unwrap();
    commit_all(&w.main, "ignore data");
    assert!(pv::pin_would_be_ignored(&w.main).is_some());
    let rep = w.provision(&w.main);
    assert_eq!(rep["mode"], "unknown");
    assert!(rep["detail"].as_str().unwrap().contains("silently never committed"));

    let w = dvc_project();
    std::os::unix::fs::symlink(w.ws.join("data"), w.main.join("data")).unwrap();
    assert!(pv::data_path_conflict(&w.main).is_some());
    assert_eq!(w.provision(&w.main)["mode"], "unknown");
    assert!(w.main.join("data").is_symlink(), "must not delete what a human put there");
}

// --- one lever -----------------------------------------------------------------------

#[test]
fn a_code_merge_carries_the_data() {
    let w = dvc_project();
    w.provision(&w.main);
    add_chunk(&w.main, "alpha", &[("f.txt", "alpha v1\n")]);
    add_chunk(&w.main, "beta", &[("f.txt", "beta v1\n")]);
    commit_all(&w.main, "two chunks");
    let beta_before = fs::read_to_string(w.main.join("data/beta.dvc")).unwrap();

    let s2 = w.scope("s2");
    w.provision(&s2);
    awm_dvcdata::testkit::unprotect(&s2.join("data/alpha")).unwrap();
    fs::write(s2.join("data/alpha/g.txt"), "added by s2\n").unwrap();
    awm_dvcdata::testkit::add(&s2, "data/alpha").unwrap();
    commit_all(&s2, "s2 adds g.txt");

    git(&w.main, &["merge", "--no-edit", "feat/s2"]);
    assert!(w.main.join("data/alpha/g.txt").exists(), "the post-merge hook did not materialise the merged data");
    assert!(w.main.join("data/alpha/f.txt").exists());
    assert_eq!(fs::read_to_string(w.main.join("data/beta.dvc")).unwrap(), beta_before, "an untouched chunk must not move");
}

#[test]
fn the_merge_driver_combines_two_branches_that_each_changed_one_chunk() {
    let w = dvc_project();
    w.provision(&w.main);
    add_chunk(&w.main, "c", &[("base.txt", "base\n")]);
    commit_all(&w.main, "chunk");
    let s = w.scope("s");
    w.provision(&s);
    for (wt, f) in [(&w.main, "ours.txt"), (&s, "theirs.txt")] {
        awm_dvcdata::testkit::unprotect(&wt.join("data/c")).unwrap();
        fs::write(wt.join("data/c").join(f), format!("{f}\n")).unwrap();
        awm_dvcdata::testkit::add(wt, "data/c").unwrap();
        commit_all(wt, f);
    }
    let out = git(&w.main, &["merge", "--no-edit", "feat/s"]);
    assert!(!out.contains("CONFLICT"), "{out}");
    for f in ["base.txt", "ours.txt", "theirs.txt"] {
        assert!(w.main.join("data/c").join(f).is_file(), "{f} missing after the merge: {out}");
    }
    assert!(!pv::state_dir(&w.main).join(CHECKOUT_FAILED_FILE).exists());
}

#[test]
fn an_empty_mount_list_means_nothing_not_everything() {
    let w = dvc_project();
    w.provision(&w.main);
    add_chunk(&w.main, "c", &[("f.txt", "v1\n")]);
    commit_all(&w.main, "chunk");
    pv::write_mounts(&w.main, Some(&[])).unwrap();
    assert_eq!(pv::read_mounts(&w.main), Some(vec![]));
    fs::remove_dir_all(w.main.join("data/c")).unwrap();
    let rep = w.provision(&w.main);
    assert_eq!(rep["checkout"], "skipped");
    assert!(!w.main.join("data/c").exists());
    fs::remove_file(pv::state_dir(&w.main).join(MOUNTS_FILE)).unwrap();
    let rep = w.provision(&w.main);
    assert_eq!(rep["checkout"], "ok");
    assert!(w.main.join("data/c/f.txt").exists());
}

// --- cache safety --------------------------------------------------------------------

#[test]
fn the_cache_survives_chmod_and_worktree_deletion() {
    let w = dvc_project();
    w.provision(&w.main);
    add_chunk(&w.main, "c", &[("f.bin", &"y".repeat(5000))]);
    let f = w.main.join("data/c/f.bin");
    let before = fs::metadata(&f).unwrap().permissions();
    pv::chmod_dirs_writable(&w.main);
    assert_eq!(fs::metadata(&f).unwrap().permissions(), before, "this would unprotect the shared cache object");
    assert!(writable(&w.main.join("data/c")));
    let oid = add_chunk(&w.main, "d", &[("f.bin", &"z".repeat(5000))]);
    let leaf = awm_dvcdata::odb::Manifest::load(&w.cache(), &oid).unwrap().entries[0].md5.clone();
    pv::chmod_dirs_writable(&w.main.join("data"));
    fs::remove_dir_all(w.main.join("data")).unwrap();
    let obj = awm_dvcdata::odb::object_path(&w.cache(), &leaf);
    assert_eq!(fs::read_to_string(obj).unwrap(), "z".repeat(5000), "deleting a worktree cannot harm the cache");
}

// --- wiring --------------------------------------------------------------------------

#[test]
fn hooks_land_in_the_common_dir_and_are_shared() {
    let w = dvc_project();
    let rep = wiring::ensure_repo_wiring(&w.main, &w.bin);
    assert_eq!(rep["result"], "ok", "{rep}");
    let common = Path::new(rep["git_common_dir"].as_str().unwrap()).to_path_buf();
    for name in ["post-merge", "post-commit", "post-checkout"] {
        let hook = common.join("hooks").join(name);
        assert!(writable(&hook) && fs::metadata(&hook).unwrap().permissions().mode_is_exec());
        assert!(fs::read_to_string(&hook).unwrap().contains(w.bin.to_str().unwrap()), "hooks run under a minimal PATH");
    }
    assert!(fs::read_to_string(common.join("info/attributes")).unwrap().contains("*.dvc merge=dvc"));
    let driver = git(&w.main, &["config", "--get", "merge.dvc.driver"]);
    assert_eq!(driver.trim(), wiring::driver_command(&w.bin));
}

trait Exec {
    fn mode_is_exec(&self) -> bool;
}

impl Exec for fs::Permissions {
    fn mode_is_exec(&self) -> bool {
        use std::os::unix::fs::PermissionsExt;
        self.mode() & 0o111 == 0o111
    }
}

#[test]
fn a_foreign_hook_is_never_clobbered() {
    let w = dvc_project();
    let hooks = w.bare.join("hooks");
    fs::create_dir_all(&hooks).unwrap();
    fs::write(hooks.join("post-merge"), "#!/bin/sh\necho someone elses hook\n").unwrap();
    let rep = wiring::ensure_repo_wiring(&w.main, &w.bin);
    assert!(rep["actions"]["post-merge_hook"].as_str().unwrap().contains("conflict"));
    assert!(fs::read_to_string(hooks.join("post-merge")).unwrap().contains("someone elses hook"));
}

#[test]
fn the_first_native_wiring_backs_up_the_python_hooks_and_driver() {
    let w = dvc_project();
    let hooks = w.bare.join("hooks");
    fs::create_dir_all(&hooks).unwrap();
    let py = "#!/usr/bin/env bash\n# AWM-managed. Materialise the data this commit pins, after a merge moved it.\nDVC=/x/envs/dvc/bin/dvc\n";
    fs::write(hooks.join("post-merge"), py).unwrap();
    git(&w.main, &["config", "merge.dvc.driver", "/x/envs/dvc/bin/dvc git-hook merge-driver --ancestor %O --our %A --their %B"]);
    let rep = wiring::ensure_repo_wiring(&w.main, &w.bin);
    assert_eq!(rep["actions"]["post-merge_hook"], "installed");
    assert_eq!(fs::read_to_string(hooks.join("post-merge.awm-py.bak")).unwrap(), py);
    assert!(fs::read_to_string(hooks.join(wiring::DRIVER_BACKUP)).unwrap().contains("git-hook merge-driver"));
    assert!(!hooks.join("post-commit.awm-py.bak").exists(), "no Python hook was there to back up");
    // A later rewiring keeps the first backup.
    fs::write(hooks.join("post-merge"), "#!/bin/sh\n# AWM-managed. old shim\n").unwrap();
    wiring::ensure_repo_wiring(&w.main, &w.bin);
    assert_eq!(fs::read_to_string(hooks.join("post-merge.awm-py.bak")).unwrap(), py);
}

#[test]
fn the_cache_config_is_absolute_untracked_and_keeps_a_local_link_type() {
    let w = dvc_project();
    wiring::ensure_cache_config(&w.main, &w.layout).unwrap();
    let local = fs::read_to_string(w.main.join(".dvc/config.local")).unwrap();
    assert!(local.contains(w.cache().to_str().unwrap()) && w.cache().is_absolute());
    assert!(!git(&w.main, &["check-ignore", ".dvc/config.local"]).trim().is_empty());
    let tracked = fs::read_to_string(w.main.join(".dvc/config")).unwrap();
    assert_eq!(tracked, "[cache]\n    type = \"hardlink,symlink\"\n");
    let local = w.main.join(".dvc/config.local");
    let text = fs::read_to_string(&local).unwrap().replace("[core]", "    type = copy\n[core]");
    fs::write(&local, text.replace("    dir =", "    type = copy\n    dir =")).unwrap();
    wiring::ensure_cache_config(&w.main, &w.layout).unwrap();
    assert_eq!(awm_dvcdata::scope::cache_types(&w.main), vec!["copy"]);
    assert!(fs::read_to_string(&local).unwrap().contains(w.cache().to_str().unwrap()));
    assert_eq!(fs::read_to_string(w.main.join(".dvc/config")).unwrap(), "[cache]\n    type = \"hardlink,symlink\"\n");
}

// --- no remote -----------------------------------------------------------------------

const TAG: &str = "Signature: 8a477f597d28d172789f06886806bc55\n# no remote\n";

#[test]
fn a_tagged_project_keeps_its_own_cache() {
    let w = dvc_project();
    assert!(!pv::no_remote(&w.main));
    assert_eq!(w.layout.cache_dir(Some(&w.main)), w.cache());
    fs::write(w.bare.parent().unwrap().join("CACHEDIR.TAG"), "not a tag\n").unwrap();
    assert!(!pv::no_remote(&w.main), "a tag without the signature declares nothing");
    fs::write(w.bare.parent().unwrap().join("CACHEDIR.TAG"), TAG).unwrap();
    assert!(pv::no_remote(&w.main));
    let own = w.bare.parent().unwrap().join(".dvc_cache");
    assert_eq!(w.layout.cache_dir(Some(&w.main)), own);
    wiring::ensure_cache_config(&w.main, &w.layout).unwrap();
    let local = fs::read_to_string(w.main.join(".dvc/config.local")).unwrap();
    assert!(local.contains(own.to_str().unwrap()) && !local.contains(w.cache().to_str().unwrap()));
}

// --- shared chunks -------------------------------------------------------------------

fn shared_project() -> Ws {
    let w = dvc_project();
    w.provision(&w.main);
    add_chunk(&w.main, "ref/big", &[("f.txt", "ref/big v1\n")]);
    add_chunk(&w.main, "small", &[("f.txt", "small v1\n")]);
    fs::write(w.main.join("data").join(SHARED_DECLARATION), "# shared\nref/big\n").unwrap();
    commit_all(&w.main, "chunks");
    w
}

fn drop_link(wt: &Path) {
    let big = wt.join("data/ref/big");
    if big.is_symlink() {
        fs::remove_file(big).unwrap();
    }
}

/// b re-pins the shared chunk from a private tree, with g.txt added.
fn repin_v2(w: &Ws, b: &Path) {
    fs::remove_file(b.join("data/ref/big")).unwrap();
    let r = awm_dvcdata::checkout::checkout(b, Some(&["data/ref/big".to_string()])).unwrap();
    assert!(r.ok(), "{}", r.failures());
    awm_dvcdata::testkit::unprotect(&b.join("data/ref/big")).unwrap();
    fs::write(b.join("data/ref/big/g.txt"), "v2\n").unwrap();
    awm_dvcdata::testkit::add(b, "data/ref/big").unwrap();
    commit_all(b, "v2");
    let _ = w;
}

#[test]
fn scopes_link_one_shared_copy() {
    let w = shared_project();
    let a = w.scope("a");
    let b = w.scope("g/b");
    let ra = w.provision(&a);
    let rb = w.provision(&b);
    let md5 = pin_md5(&a.join("data/ref/big.dvc")).unwrap();
    let copy = w.store().join("ref/big").join(&md5);
    assert_eq!(ra["shared"], json!({"data/ref/big": "linked"}));
    assert_eq!(rb["shared"], ra["shared"]);
    for wt in [&a, &b] {
        assert!(wt.join("data/ref/big").is_symlink());
        assert_eq!(fs::canonicalize(wt.join("data/ref/big")).unwrap(), fs::canonicalize(&copy).unwrap());
        assert!(wt.join("data/small/f.txt").is_file() && !wt.join("data/small").is_symlink());
    }
    assert_eq!(ino(&a.join("data/ref/big/f.txt")), ino(&b.join("data/ref/big/f.txt")));
    let st = pv::data_status(&w.layout, "p", "a", &a);
    assert_eq!(st["shared"], json!({"data/ref/big": "linked"}));
    assert_eq!(st["in_sync"], true, "{st}");
}

#[test]
fn a_merge_repoints_the_link_or_leaves_the_sentinel() {
    let w = shared_project();
    let a = w.scope("a");
    let b = w.scope("b");
    w.provision(&a);
    w.provision(&b);
    repin_v2(&w, &b);
    git(&a, &["merge", "--no-edit", "feat/b"]);
    let big = a.join("data/ref/big");
    assert!(!big.exists() && !big.is_symlink(), "no copy for the new md5: drop the link, never build a real tree");
    assert!(fs::read_to_string(pv::state_dir(&a).join(CHECKOUT_FAILED_FILE)).unwrap().contains("no shared copy"));

    let rep = w.provision(&b);
    assert_eq!(rep["shared"], json!({"data/ref/big": "linked"}), "b's clean tree is adopted");
    let md5 = pin_md5(&b.join("data/ref/big.dvc")).unwrap();
    assert!(w.store().join("ref/big").join(&md5).join("g.txt").is_file());

    let rep = w.provision(&a);
    assert_eq!(rep["shared"], json!({"data/ref/big": "linked"}));
    assert!(big.join("g.txt").is_file());
    assert!(!pv::state_dir(&a).join(CHECKOUT_FAILED_FILE).exists());
}

#[test]
fn heal_replaces_a_clean_private_copy_and_keeps_a_modified_one() {
    let w = shared_project();
    let a = w.scope("a");
    let b = w.scope("b");
    w.provision(&a);
    drop_link(&b);
    wiring::ensure_cache_config(&b, &w.layout).unwrap();
    assert!(awm_dvcdata::checkout::checkout(&b, Some(&["data/ref/big".to_string()])).unwrap().ok());
    assert!(b.join("data/ref/big/f.txt").is_file() && !b.join("data/ref/big").is_symlink());
    assert_eq!(w.provision(&b)["shared"], json!({"data/ref/big": "linked"}));
    assert!(b.join("data/ref/big").is_symlink());

    let c = w.scope("c");
    drop_link(&c);
    wiring::ensure_cache_config(&c, &w.layout).unwrap();
    assert!(awm_dvcdata::checkout::checkout(&c, Some(&["data/ref/big".to_string()])).unwrap().ok());
    awm_dvcdata::testkit::unprotect(&c.join("data/ref/big")).unwrap();
    fs::write(c.join("data/ref/big/f.txt"), "local edit\n").unwrap();
    assert_eq!(w.provision(&c)["shared"], json!({"data/ref/big": "private:modified"}));
    assert_eq!(fs::read_to_string(c.join("data/ref/big/f.txt")).unwrap(), "local edit\n");
}

#[test]
fn a_checkout_never_writes_through_a_link_to_another_copy() {
    let w = shared_project();
    let a = w.scope("a");
    w.provision(&a);
    let v1 = pin_md5(&a.join("data/ref/big.dvc")).unwrap();
    let b = w.scope("b");
    w.provision(&b);
    repin_v2(&w, &b);
    w.provision(&b);
    let v2 = pin_md5(&b.join("data/ref/big.dvc")).unwrap();

    git(&a, &["merge", "--no-edit", "feat/b"]);
    git(&a, &["checkout", "HEAD~1", "--", "data/ref/big.dvc"]);
    let r = awm_dvcdata::checkout::checkout(&a, None).unwrap();
    assert!(!r.ok() && r.failures().contains("never writes through"), "{}", r.failures());
    let store = w.store().join("ref/big");
    assert_eq!(fs::read_to_string(store.join(&v1).join("f.txt")).unwrap(), "ref/big v1\n");
    assert_eq!(fs::read_to_string(store.join(&v2).join("g.txt")).unwrap(), "v2\n");
    assert!(!writable(&store.join(&v2).join("g.txt")));

    // A branch switch repoints the link with no merge.
    git(&a, &["checkout", "-q", "--", "data/ref/big.dvc"]);
    assert_eq!(fs::canonicalize(a.join("data/ref/big")).unwrap(), fs::canonicalize(store.join(&v2)).unwrap());
    git(&a, &["checkout", "-q", "-b", "sw", "main"]);
    assert_eq!(fs::canonicalize(a.join("data/ref/big")).unwrap(), fs::canonicalize(store.join(&v1)).unwrap());
}


/// `AWM_DATA_DVC` is process-wide, so the switch runs in a child of this binary.
#[test]
fn the_kill_switch_provisions_no_project() {
    let exe = std::env::current_exe().unwrap();
    let out = std::process::Command::new(exe)
        .args(["--exact", "provision::kill_switch_child", "--ignored"])
        .env("AWM_DATA_DVC", "0")
        .output()
        .unwrap();
    assert!(out.status.success(), "{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr));
    assert!(String::from_utf8_lossy(&out.stdout).contains("1 passed"), "the child test did not run");
}

#[test]
#[ignore]
fn kill_switch_child() {
    let w = dvc_project();
    let rep = w.provision(&w.main);
    assert_eq!(rep["mode"], "none");
    assert!(rep["detail"].as_str().unwrap().contains("AWM_DATA_DVC=0"));
    assert!(!w.main.join(".dvc/config.local").exists());
}
