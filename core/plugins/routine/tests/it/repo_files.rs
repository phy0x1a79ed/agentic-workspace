//! awm's own deploy.toml and the scripts the land rollout runs: refresh-checkout.sh,
//! full-tests.sh, move-canonical.sh, awm-deploy.sh, build.sh, install.sh and refresh-pins.sh.


use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use crate::common::git;
use sha2::{Digest, Sha256};

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..").canonicalize().unwrap()
}

fn path_env() -> String {
    std::env::var("PATH").unwrap_or_default()
}

fn home() -> String {
    std::env::var("HOME").unwrap_or_default()
}

fn sh(cmd: &mut Command) -> Output {
    cmd.output().unwrap()
}

fn text(b: &[u8]) -> String {
    String::from_utf8_lossy(b).into_owned()
}

fn write(p: &Path, s: &str) {
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    std::fs::write(p, s).unwrap();
}

fn exe(p: &Path, s: &str) {
    write(p, s);
    std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o755)).unwrap();
}

fn copy(from: &str, to: &Path) {
    std::fs::create_dir_all(to.parent().unwrap()).unwrap();
    std::fs::copy(repo().join(from), to).unwrap();
}

fn commit(dir: &Path, msg: &str) {
    git(dir, &["add", "-A"]);
    git(dir, &["commit", "-q", "-m", msg]);
}

fn have(tool: &str) -> bool {
    Command::new("sh").args(["-c", &format!("command -v {tool}")]).output().is_ok_and(|o| o.status.success())
}

/// A canonical bare repo with refs/mizar/release, and a node checkout of it.
fn node_repo(tmp: &Path) -> (PathBuf, String) {
    let seed = tmp.join("seed");
    git(tmp, &["init", "-q", "-b", "release", &seed.to_string_lossy()]);
    write(&seed.join("keep.txt"), "1\n");
    write(&seed.join("gone.txt"), "1\n");
    commit(&seed, "one");
    let bare = tmp.join("bare");
    git(tmp, &["clone", "-q", "--bare", &seed.to_string_lossy(), &bare.to_string_lossy()]);
    let node = tmp.join("node");
    git(tmp, &["clone", "-q", "-o", "awm", "-b", "release", &bare.to_string_lossy(), &node.to_string_lossy()]);
    std::fs::remove_file(seed.join("gone.txt")).unwrap();
    write(&seed.join("keep.txt"), "2\n");
    commit(&seed, "two");
    let to = git(&seed, &["rev-parse", "HEAD"]);
    git(&seed, &["push", "-q", &bare.to_string_lossy(), "HEAD:refs/mizar/release"]);
    write(&node.join(".awm/state.db"), "x");
    write(&node.join("stray.txt"), "x");
    (node, to)
}

fn refresh_checkout(node: &Path, to: &str) -> Output {
    sh(Command::new("sh").arg(repo().join("scripts/refresh-checkout.sh")).current_dir(node).env_clear().env("PATH", path_env()).env("HOME", home()).env("AWM_REFRESH_TO", to))
}

#[test]
fn refresh_checkout_takes_a_pushed_release() {
    let tmp = tempfile::tempdir().unwrap();
    let (node, to) = node_repo(tmp.path());
    write(&node.join(".gitignore"), ".awm/\n");
    let r = refresh_checkout(&node, &to);
    assert!(r.status.success(), "{}", text(&r.stderr));
    assert_eq!(git(&node, &["rev-parse", "HEAD"]), to);
    assert_eq!(std::fs::read_to_string(node.join("keep.txt")).unwrap(), "2\n");
    assert!(!node.join("gone.txt").exists() && !node.join("stray.txt").exists() && node.join(".awm/state.db").exists());
}

#[test]
fn refresh_checkout_keeps_a_local_commit() {
    let tmp = tempfile::tempdir().unwrap();
    let (node, to) = node_repo(tmp.path());
    git(&node, &["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "hand patch"]);
    let head = git(&node, &["rev-parse", "HEAD"]);
    let r = refresh_checkout(&node, &to);
    assert_eq!(r.status.code(), Some(1));
    assert!(text(&r.stderr).contains("does not contain"));
    assert_eq!(git(&node, &["rev-parse", "HEAD"]), head);
}

const FAKE_RUN_TESTS: &str = "#!/usr/bin/env bash\nwhile [ $# -gt 0 ]; do\n  case \"$1\" in --summary) out=\"$2\"; shift 2 ;; *) shift ;; esac\ndone\ncat \"$(git rev-parse --show-toplevel)/results\" > \"$out\"\n";

#[test]
fn full_tests_flags_only_new_failures() {
    let tmp = tempfile::tempdir().unwrap();
    let r = tmp.path().join("r");
    copy("scripts/full-tests.sh", &r.join("scripts/full-tests.sh"));
    exe(&r.join("scripts/run-tests.sh"), FAKE_RUN_TESTS);
    git(&r, &["init", "-q"]);
    write(&r.join("results"), "a PASS\nb FAIL (rc=1)\n");
    commit(&r, "one");
    let run = || sh(Command::new("bash").arg("scripts/full-tests.sh").current_dir(&r));
    let first = run();
    assert_eq!(first.status.code(), Some(1));
    assert!(text(&first.stdout).contains("new failures: b"));
    let head = git(&r, &["rev-parse", "HEAD"]);
    let known = r.join(".awm/full-tests/known-failures");
    assert_eq!(std::fs::read_to_string(&known).unwrap().split_whitespace().collect::<Vec<_>>(), ["b", head.as_str()]);
    // The same failure the next night is known, not new.
    let second = run();
    assert!(second.status.success() && text(&second.stdout).contains("no new failures"));
    // b recovers, a breaks: only a is new, and b leaves the known list.
    write(&r.join("results"), "a FAIL (rc=1)\nb PASS\n");
    let third = run();
    assert_eq!(third.status.code(), Some(1));
    assert!(text(&third.stdout).contains("new failures: a"));
    assert_eq!(std::fs::read_to_string(&known).unwrap().split_whitespace().next(), Some("a"));
}

fn move_canonical(ws: &Path, args: &[&str]) -> Output {
    sh(Command::new("bash").arg(repo().join("scripts/move-canonical.sh")).args(args).env_clear().env("PATH", path_env()).env("HOME", home()).env("AWM_WORKSPACE", ws))
}

#[test]
fn move_canonical_repoints_the_checkout_and_adds_github() {
    let tmp = tempfile::tempdir().unwrap();
    let t = tmp.path();
    let (old, seed, ws) = (t.join("old.git"), t.join("seed"), t.join("ws"));
    let s = |p: &Path| p.to_string_lossy().into_owned();
    git(t, &["init", "-q", "-b", "release", &s(&seed)]);
    git(&seed, &["commit", "-q", "--allow-empty", "-m", "one"]);
    git(t, &["clone", "-q", "--bare", &s(&seed), &s(&old)]);
    git(t, &["clone", "-q", "-o", "awm", &s(&old), &s(&ws)]);
    git(&ws, &["remote", "add", "origin", &s(&old)]);
    git(&ws, &["remote", "add", "other", &s(&seed)]);
    let bare = ws.join("projects/awm/.bare");
    git(t, &["clone", "-q", "--bare", &s(&old), &s(&bare)]);
    let r = move_canonical(&ws, &["repoint", &s(&old), "local"]);
    assert!(r.status.success(), "{}", text(&r.stderr));
    assert_eq!(git(&ws, &["remote", "get-url", "awm"]), s(&bare));
    assert_eq!(git(&ws, &["remote", "get-url", "origin"]), s(&bare));
    assert_eq!(git(&ws, &["remote", "get-url", "other"]), s(&seed));
    let gh = t.join("gh.git");
    git(t, &["init", "-q", "--bare", &s(&gh)]);
    let r = move_canonical(&ws, &["github", &s(&gh)]);
    assert!(r.status.success() && text(&r.stdout).contains("first deploy"));
    git(&bare, &["push", "-q", "github", "release"]);
    git(&seed, &["commit", "-q", "--allow-empty", "-m", "made on github only"]);
    git(&seed, &["push", "-q", &s(&gh), "release"]);
    let r = move_canonical(&ws, &["github", &s(&gh)]);
    assert_eq!(r.status.code(), Some(1));
    assert!(text(&r.stderr).contains("stops at backup"));
    assert!(text(&move_canonical(&ws, &["order"]).stdout).contains("canonical_repos"));
}

// -- the core binary pin: scripts/core.pin and scripts/awm-deploy.sh ------------

const FAKE_BIN: &str = "#!/bin/sh\necho \"fake awm $*\"\n";
const PAGE: &str = "awm/services/svc/pages/ui/dist";

fn sha_hex(b: &[u8]) -> String {
    Sha256::digest(b).iter().map(|x| format!("{x:02x}")).collect()
}

fn pin(node: &Path, name: &str, version: &str, file: &str, blob: &[u8], sha: Option<&str>) {
    let sha = sha.map(str::to_string).unwrap_or_else(|| sha_hex(blob));
    write(&node.join("scripts").join(name), &format!("version={version}\nfile={file}\nsha256={sha}\n"));
}

fn pages_tar(tmp: &Path, body: &str) -> Vec<u8> {
    let src = tmp.join("pages-src");
    write(&src.join(PAGE).join("index.html"), body);
    let out = tmp.join("pages.tar.gz");
    let r = sh(Command::new("tar").arg("czf").arg(&out).arg("-C").arg(&src).arg("awm"));
    assert!(r.status.success(), "{}", text(&r.stderr));
    std::fs::read(out).unwrap()
}

/// The crate core_version starts its closure from (scripts/pins.sh).
fn awm_cli(tree: &Path) {
    write(&tree.join("core/crates/awm-cli/Cargo.toml"), "[package]\nname = \"awm-cli\"\n");
}

#[derive(Default)]
struct DeployOpts {
    core_sha: Option<&'static str>,
    pages_sha: Option<&'static str>,
    core_changed: bool,
    pages_changed: bool,
}

fn pins_sh(node: &Path, f: &str) -> String {
    let r = sh(Command::new("bash").args(["-c", &format!(". scripts/pins.sh; {f} .")]).current_dir(node));
    assert!(r.status.success(), "{}", text(&r.stderr));
    text(&r.stdout).trim().to_string()
}

/// A checkout holding awm-deploy.sh and both pins, and the artifact store its node holds.
fn deploy_node(tmp: &Path, o: DeployOpts) -> PathBuf {
    let node = tmp.join("node");
    for f in ["awm-deploy.sh", "pins.sh", "marks-dir.sh"] {
        copy(&format!("scripts/{f}"), &node.join("scripts").join(f));
    }
    write(&node.join("core/Cargo.toml"), "x\n");
    awm_cli(&node);
    write(&node.join(".gitignore"), "awm/*/*/pages/*/dist/\n");
    write(&node.join("awm/services/svc/pages/ui/index.html"), "src\n");
    git(tmp, &["init", "-q", &node.to_string_lossy()]);
    commit(&node, "one");
    let store = node.join(".awm/artifacts");
    std::fs::create_dir_all(&store).unwrap();
    let (cv, pv) = (pins_sh(&node, "core_version"), pins_sh(&node, "pages_version"));
    let pages = pages_tar(tmp, "built\n");
    pin(&node, "core.pin", &cv, &format!("awm-{cv}"), FAKE_BIN.as_bytes(), o.core_sha);
    pin(&node, "pages.pin", &pv, &format!("awm-{pv}.tar.gz"), &pages, o.pages_sha);
    std::fs::write(store.join(format!("awm-{cv}")), FAKE_BIN).unwrap();
    std::fs::write(store.join(format!("awm-{pv}.tar.gz")), &pages).unwrap();
    write(&node.join(PAGE).join("old.js"), "old\n");
    if o.core_changed {
        write(&node.join("core/Cargo.toml"), "y\n");
    }
    if o.pages_changed {
        write(&node.join("awm/services/svc/pages/ui/index.html"), "changed\n");
    }
    commit(&node, "two");
    node
}

fn run_deploy(node: &Path, args: &[&str]) -> Output {
    sh(Command::new("bash").arg(node.join("scripts/awm-deploy.sh")).args(args).current_dir(node).env_clear().env("PATH", path_env()).env("HOME", home()))
}

#[test]
fn awm_deploy_runs_the_pinned_binary_and_unpacks_the_pages() {
    let tmp = tempfile::tempdir().unwrap();
    let node = deploy_node(tmp.path(), DeployOpts::default());
    let r = run_deploy(&node, &["--dry-run"]);
    assert!(r.status.success(), "{}", text(&r.stderr));
    let pin = std::fs::read_to_string(node.join("scripts/core.pin")).unwrap();
    let file = pin.split("file=").nth(1).unwrap().lines().next().unwrap();
    let n = node.display();
    assert_eq!(text(&r.stdout).trim(), format!("fake awm deploy --root {n} --bin {n}/.awm/artifacts/{file} --no-build --dry-run"));
    assert_eq!(std::fs::read_to_string(node.join(PAGE).join("index.html")).unwrap(), "built\n");
    assert!(!node.join(PAGE).join("old.js").exists());
}

#[test]
fn awm_deploy_stops_on_a_bad_pin_and_changes_nothing() {
    let zero = "0000000000000000000000000000000000000000000000000000000000000000";
    for (o, msg) in [
        (DeployOpts { core_sha: Some(zero), ..Default::default() }, "has sha256"),
        (DeployOpts { pages_sha: Some(zero), ..Default::default() }, "has sha256"),
        (DeployOpts { core_changed: true, ..Default::default() }, "refresh-pins.sh"),
        (DeployOpts { pages_changed: true, ..Default::default() }, "refresh-pins.sh"),
    ] {
        let tmp = tempfile::tempdir().unwrap();
        let node = deploy_node(tmp.path(), o);
        let r = run_deploy(&node, &[]);
        assert_eq!(r.status.code(), Some(1));
        assert!(text(&r.stderr).contains(msg), "{msg}: {}", text(&r.stderr));
        assert!(r.stdout.is_empty() && node.join(PAGE).join("old.js").exists());
    }
}

// -- awm/scripts/build.sh runs npm ci on a changed lock; awm/install.sh asks `awm wants` ---

/// A checkout holding copies of the two scripts, for them to run against.
fn script_tree(tmp: &Path) -> PathBuf {
    let tree = tmp.join("tree");
    std::fs::create_dir_all(tree.join("awm/scripts")).unwrap();
    git(tmp, &["init", "-q", &tree.to_string_lossy()]);
    for rel in ["awm/scripts/build.sh", "awm/install.sh"] {
        copy(rel, &tree.join(rel));
    }
    tree
}

#[test]
fn build_runs_npm_ci_only_when_the_lock_changed() {
    // build.sh checks for node 18 or later before it looks at the lock.
    if !have("node") {
        eprintln!("no node: skipped");
        return;
    }
    let tmp = tempfile::tempdir().unwrap();
    let tree = script_tree(tmp.path());
    let log = tmp.path().join("npm.log");
    let bin = tmp.path().join("fakebin");
    exe(
        &bin.join("npm"),
        "#!/bin/sh\necho \"npm $*\" >> \"$NPM_LOG\"\nrm -rf node_modules\nmkdir -p node_modules/.bin\nprintf \"#!/bin/sh\\n\" > node_modules/.bin/vite\nchmod +x node_modules/.bin/vite\n",
    );
    let build = || {
        let r = sh(Command::new("bash")
            .arg(tree.join("awm/scripts/build.sh"))
            .current_dir(&tree)
            .env_clear()
            .env("PATH", format!("{}:{}", bin.display(), path_env()))
            .env("HOME", home())
            .env("NPM_LOG", &log));
        assert!(r.status.success(), "{}", text(&r.stderr));
    };
    write(&tree.join("awm/package-lock.json"), "one\n");
    build();
    assert_eq!(std::fs::read_to_string(&log).unwrap(), "npm ci\n");
    build();
    assert_eq!(std::fs::read_to_string(&log).unwrap(), "npm ci\n");
    write(&tree.join("awm/package-lock.json"), "two\n");
    build();
    assert_eq!(std::fs::read_to_string(&log).unwrap(), "npm ci\nnpm ci\n");
}

fn install(tree: &Path, core: &Path, state: &Path) -> Output {
    sh(Command::new("bash").arg(tree.join("awm/install.sh")).current_dir(tree).env_clear().env("PATH", path_env()).env("HOME", home()).env("AWM_CORE", core).env("AWM_DIR", state))
}

#[test]
fn install_runs_the_plugins_the_config_enables() {
    if !have("socat") {
        eprintln!("no socat: skipped");
        return;
    }
    let tmp = tempfile::tempdir().unwrap();
    let (tree, state) = (script_tree(tmp.path()), tmp.path().join("state"));
    write(&state.join("config.toml"), "plugins = [\"*\"]\nexclude = [\"off\"]\n");
    for name in ["on", "off"] {
        write(&tree.join("awm/services").join(name).join("install.sh"), &format!("echo ran >> \"{}/{name}.ran\"\n", tmp.path().display()));
    }
    // A core whose `wants` follows config.toml's exclude line, as Config::wants does.
    let core = tmp.path().join("core");
    exe(&core, "#!/bin/sh\n[ \"$1\" = wants ] || exit 2\ngrep -q \"\\\"$2\\\"\" \"$AWM_DIR/config.toml\" && exit 1\nexit 0\n");
    let r = install(&tree, &core, &state);
    assert!(r.status.success(), "{}", text(&r.stderr));
    assert!(tmp.path().join("on.ran").exists() && !tmp.path().join("off.ran").exists());
    assert!(text(&r.stdout).contains("does not enable it): off"));
}

// -- scripts/refresh-pins.sh: rebuild only the stale pin, with the caches kept ----------

const STUB: &str = r#"set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
. "$root/scripts/pins.sh"
mkdir -p "$AWM_DIR"; echo "NAME target=${AWM_CARGO_TARGET_DIR:-} nm=$(ls "$root/awm/node_modules" 2>/dev/null | tr '\n' ' ')" >> "$AWM_DIR/calls.log"
version="$(VER "$root")"
mkdir -p "$AWM_DIR/artifacts"; echo "NAME" > "$AWM_DIR/artifacts/$version"
EXTRA
write_pin "$root/scripts/NAME.pin" d "$version" "$version" "$AWM_DIR/artifacts"
"#;

/// A checkout holding the real pins.sh and refresh-pins.sh, and stub release scripts that log
/// their call and write a real pin and artifact.
fn refresh_tree(tmp: &Path) -> PathBuf {
    let tree = tmp.join("tree");
    for f in ["refresh-pins.sh", "pins.sh"] {
        copy(&format!("scripts/{f}"), &tree.join("scripts").join(f));
    }
    let stub = |name: &str, ver: &str, extra: &str| STUB.replace("NAME", name).replace("VER", ver).replace("EXTRA", extra);
    exe(&tree.join("scripts/release-core.sh"), &stub("core", "core_version", ""));
    exe(
        &tree.join("scripts/release-pages.sh"),
        &stub("pages", "pages_version", "mkdir -p \"$root/awm/node_modules/pkg\"; echo ok > \"$root/awm/node_modules/pkg/index.js\""),
    );
    write(&tree.join("core/Cargo.toml"), "1\n");
    awm_cli(&tree);
    write(&tree.join("awm/package-lock.json"), "{}\n");
    write(&tree.join("awm/services/svc/pages/ui/index.html"), "1\n");
    write(&tree.join(".gitignore"), "node_modules\n.awm/\n");
    git(tmp, &["init", "-q", &tree.to_string_lossy()]);
    commit(&tree, "one");
    tree
}

fn run_pins(tree: &Path, awm_dir: &Path) -> Vec<String> {
    let r = sh(Command::new("bash").arg(tree.join("scripts/refresh-pins.sh")).current_dir(tree).env("AWM_DIR", awm_dir).env_remove("AWM_CARGO_TARGET_DIR"));
    assert!(r.status.success(), "{}", text(&r.stderr));
    std::fs::read_to_string(awm_dir.join("calls.log")).map(|t| t.lines().map(str::to_string).collect()).unwrap_or_default()
}

fn first_words(lines: &[String]) -> Vec<&str> {
    lines.iter().map(|l| l.split_whitespace().next().unwrap_or("")).collect()
}

#[test]
fn refresh_pins_builds_what_is_stale_and_nothing_else() {
    let tmp = tempfile::tempdir().unwrap();
    let (tree, awm_dir) = (refresh_tree(tmp.path()), tmp.path().join("awm-dir"));
    let first = run_pins(&tree, &awm_dir);
    assert_eq!(first_words(&first), ["core", "pages"]);
    assert!(first[0].contains(&format!("target={}/build-cache/cargo-target", awm_dir.display())), "{}", first[0]);
    assert_eq!(std::fs::read_to_string(tree.join("scripts/core.pin")).unwrap().matches("version=core-").count(), 1);
    // Nothing changed, so nothing builds.
    assert_eq!(run_pins(&tree, &awm_dir), first);
    // A core change rebuilds the core only.
    write(&tree.join("core/Cargo.toml"), "2\n");
    commit(&tree, "core");
    assert_eq!(first_words(&run_pins(&tree, &awm_dir)[2..]), ["core"]);
    // A page change rebuilds the pages only, and a missing artifact rebuilds its pin.
    write(&tree.join("awm/services/svc/pages/ui/index.html"), "2\n");
    commit(&tree, "page");
    assert_eq!(first_words(&run_pins(&tree, &awm_dir)[3..]), ["pages"]);
    for e in std::fs::read_dir(awm_dir.join("artifacts")).unwrap() {
        std::fs::remove_file(e.unwrap().path()).unwrap();
    }
    assert_eq!(first_words(&run_pins(&tree, &awm_dir)[4..]), ["core", "pages"]);
}

