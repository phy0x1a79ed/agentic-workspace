//! Hands the newest warm session to a terminal: types `/cd <dir>` into it, which reloads project
//! settings, MCP servers, skills and CLAUDE.md as a launch there would, then renames it `claimed
//! <noun>` so it stops advertising itself as a spare.
//!
//! CAUTION: CLAUDE.md accumulates across moves. The destination's file is added and the origin's
//! is not removed, which is why a session is seeded where there is no CLAUDE.md and moved exactly
//! once. A session that already carries an origin directory is refused here in code, not merely
//! by the predicate.

use std::future::Future;
use std::path::{Path, PathBuf};
use std::time::Duration;

use awm_harness::claude::pty::{self, DaemonError, DaemonLane};
use awm_harness::claude::trust;
use serde_json::{json, Value};
use tokio::time::{sleep, Instant};

use crate::lineage::Processes;
use crate::paths::Paths;
use crate::pool::{self, Pool, Terminals};
use crate::sessions::Session;

/// Cap on every beat between pasting and pressing Enter. A warm session is idle with an empty
/// composer, the easy case, and these beats decide whether a claim is one you notice.
pub const SETTLE: Duration = Duration::from_millis(50);

/// How long to wait for a move to land. Generous on purpose: once `/cd` is typed the session is
/// committed, so giving up early costs the warm session and still pays for the cold launch, and
/// the load that makes a move slow makes a cold launch slow too. It must stay below the
/// `pool_claim` verb timeout, which must stay below `CX_WAIT` in `bin/cx`.
pub const MOVE_TIMEOUT: Duration = Duration::from_secs(6);

/// How often the session's record is read while waiting for the move.
pub const POLL: Duration = Duration::from_millis(20);

const CTRL_U: &[u8] = b"\x15";
const ESCAPE: &[u8] = b"\x1b";

/// The keystrokes a claim sends a session. [`DaemonKeys`] is the real one; tests fake it.
pub trait Keys: Send + Sync {
    /// Type one slash command into `s` and press Enter.
    fn type_command(&self, s: &Session, text: &str) -> impl Future<Output = Result<(), DaemonError>> + Send;

    /// Leave a session that would not move as idle as it was found: Escape dismisses whatever
    /// dialog swallowed the move and Ctrl-U clears the composer. Otherwise the next claim's first
    /// keystrokes answer that dialog with its default, and one directory the pool cannot move to
    /// costs every claim after it. Failures are logged, never returned.
    fn abandon(&self, s: &Session) -> impl Future<Output = ()> + Send;
}

/// Types over the daemon's PTY socket, addressed straight from the session's roster entry.
#[derive(Debug, Clone, Copy, Default)]
pub struct DaemonKeys;

/// The session's own PTY. The pool holds the id of a session it created, so the roster's socket
/// and token are the address; the handshake still checks whose REPL answers, which stops a
/// recycled socket path from being typed into.
fn lane_of(s: &Session) -> Result<DaemonLane, DaemonError> {
    DaemonLane::from_job(s).ok_or_else(|| DaemonError(format!("{} has no reachable PTY", s.short)))
}

impl Keys for DaemonKeys {
    async fn type_command(&self, s: &Session, text: &str) -> Result<(), DaemonError> {
        let mut conn = pty::connect(&lane_of(s)?).await?;
        // Ctrl-U first: an attempt that pasted but never committed would leave its text in the
        // composer for this one to concatenate onto.
        let typed = async {
            conn.send_keys(CTRL_U).await?;
            conn.type_text(text).await?;
            sleep(SETTLE).await;
            conn.press_enter().await
        }
        .await;
        conn.close().await;
        typed
    }

    async fn abandon(&self, s: &Session) {
        let cleared = async {
            let mut conn = pty::connect(&lane_of(s)?).await?;
            let keys = async {
                conn.send_keys(ESCAPE).await?;
                conn.send_keys(CTRL_U).await
            }
            .await;
            conn.close().await;
            keys
        }
        .await;
        if let Err(exc) = cleared {
            tracing::warn!("cx: could not clear {} after a failed move: {exc}", s.short);
        }
    }
}

/// The name a spare takes when a terminal claims it: `<warm serval>` becomes `claimed serval`.
///
/// The noun is the only handle the user already has on the session they watched appear. The
/// short id stands in when the name is not shaped the way the pool writes them, which only
/// happens if the prefix is reconfigured mid-life.
pub fn claimed_name(paths: &Paths, name: &str, short: &str) -> String {
    let rest: String = name.chars().skip(paths.name_prefix.chars().count()).collect();
    let noun = rest.trim().trim_end_matches('>').trim();
    format!("{}{}", paths.claimed_prefix, if noun.is_empty() { short } else { noun })
}

fn miss(reason: String) -> Value {
    tracing::info!("cx: no session handed out — {reason}");
    json!({"session": null, "reason": reason})
}

fn realpath_sync(p: &Path) -> PathBuf {
    std::fs::canonicalize(p).unwrap_or_else(|_| std::path::absolute(p).unwrap_or_else(|_| p.to_path_buf()))
}

async fn realpath(cwd: &str) -> PathBuf {
    let p = if cwd.is_empty() { std::env::current_dir().unwrap_or_default() } else { PathBuf::from(cwd) };
    match tokio::fs::canonicalize(&p).await {
        Ok(real) => real,
        Err(_) => std::path::absolute(&p).unwrap_or(p),
    }
}

/// Move the newest warm session to `cwd` and answer `{session, cwd}`, or `{session: null,
/// reason}` when there is nothing to hand out, which the caller reads as "launch cold". Nothing
/// here fails at the caller.
pub async fn claim<P, T>(pool: &Pool<P, T>, keys: &impl Keys, cwd: &str) -> Value
where
    P: Processes + Clone + Send + Sync + 'static,
    T: Terminals + Clone + Send + Sync + 'static,
{
    claim_within(pool, keys, cwd, MOVE_TIMEOUT).await
}

/// [`claim`], waiting `move_timeout` for the move to land.
pub async fn claim_within<P, T>(pool: &Pool<P, T>, keys: &impl Keys, cwd: &str, move_timeout: Duration) -> Value
where
    P: Processes + Clone + Send + Sync + 'static,
    T: Terminals + Clone + Send + Sync + 'static,
{
    let want_path = realpath(cwd).await;
    let want = want_path.to_string_lossy().into_owned();
    if !tokio::fs::metadata(&want_path).await.is_ok_and(|m| m.is_dir()) {
        return miss(format!("{want} is not a directory"));
    }
    let _held = pool::LOCK.lock().await;
    let newest = pool
        .read(|p| {
            let s = p.sessions();
            let version = s.binary_version();
            pool::warm(s.load(), s, version.as_deref(), None).into_iter().next()
        })
        .await;
    let Some(s) = newest else { return miss("no warm session".into()) };
    let short = s.short.clone();
    if s.origin_cwd.is_some() {
        // The predicate says so too; say it twice.
        return miss(format!("{short} has already been moved once"));
    }
    if !trust::trusted(&want_path, &pool.paths.home).await {
        return miss(format!("{want} would ask the user to trust it first"));
    }
    let started = Instant::now();
    if let Err(exc) = keys.type_command(&s, &format!("/cd {want}")).await {
        return miss(format!("{short} would not take the move: {exc}"));
    }
    let typed = Instant::now();
    if !arrived(pool, &short, &want_path, move_timeout).await {
        keys.abandon(&s).await;
        return miss(format!("{short} did not reach {want} in time"));
    }
    let check = short.clone();
    let Some(settled) = pool.read(move |p| recheck(p, &check)).await else {
        return miss(format!("{short} was taken while we were moving it"));
    };
    // After the recheck, never before it: the recheck asks whether the name still carries the
    // pool's prefix, and this is what takes it away. Nothing waits for the rename to land; it is
    // the session's own next turn, and `cx` is already attaching by then.
    rename(pool, keys, &settled, &short).await;
    let ms = |d: Duration| d.as_secs_f64() * 1000.0;
    tracing::info!(
        "cx: {want} claimed {short} in {:.0}ms (typing {:.0}ms, landing {:.0}ms)",
        ms(started.elapsed()),
        ms(typed - started),
        ms(typed.elapsed())
    );
    json!({"session": short, "cwd": want})
}

/// Take the pool's name off a session that now belongs to somebody, typed as `/rename` so the
/// vendor updates the record and the session registry together. A failure is logged and
/// swallowed: refusing to hand over a working session over its name would be the worse outcome.
async fn rename<P: Processes, T: Terminals>(pool: &Pool<P, T>, keys: &impl Keys, s: &Session, short: &str) {
    let name = claimed_name(&pool.paths, &s.name, short);
    if let Err(exc) = keys.type_command(s, &format!("/rename {name}")).await {
        tracing::warn!("cx: {short} kept its pool name — {exc}");
    }
}

/// Wait for the session's own record to say it is where we sent it: the only positive evidence
/// that the move ran, since a slash command runs on the session's own turn.
async fn arrived<P, T>(pool: &Pool<P, T>, short: &str, want: &Path, within: Duration) -> bool
where
    P: Processes + Clone + Send + Sync + 'static,
    T: Terminals + Clone + Send + Sync + 'static,
{
    let deadline = Instant::now() + within;
    while Instant::now() < deadline {
        let (short, want) = (short.to_string(), want.to_path_buf());
        let landed = pool
            .read(move |p| {
                p.sessions().load().iter().any(|s| {
                    s.short == short && s.cwd.as_deref().is_some_and(|c| !c.is_empty() && realpath_sync(Path::new(c)) == want)
                })
            })
            .await;
        if landed {
            return true;
        }
        sleep(POLL).await;
    }
    false
}

/// The session as it stands now, or None if it is no longer ours to give. The move stamps an
/// origin directory, so this cannot re-run `claimable`; it checks nobody else arrived during the
/// move: the name still carries the pool's prefix, nothing has been said to it, and it still runs.
fn recheck<P: Processes, T: Terminals>(pool: &Pool<P, T>, short: &str) -> Option<Session> {
    let s = pool.sessions();
    let found = s.load().into_iter().find(|x| x.short == short)?;
    (s.is_ours(&found) && found.tokens == 0 && s.is_alive(&found)).then_some(found)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pool::testing::{NoTerminals, Table, TestBox};
    use awm_harness::claude::ClaudeHome;
    use std::sync::Mutex;
    use tempfile::TempDir;

    /// What `/cd` and `/rename` do to the session's record, recorded; `moves` false types into
    /// a session that never moves.
    struct FakeKeys {
        home: ClaudeHome,
        moves: bool,
        refuse_rename: bool,
        typed: Mutex<Vec<String>>,
        cleared: Mutex<Vec<String>>,
    }

    impl FakeKeys {
        fn new(b: &TestBox) -> Self {
            Self {
                home: b.paths.home.clone(),
                moves: true,
                refuse_rename: false,
                typed: Mutex::default(),
                cleared: Mutex::default(),
            }
        }

        fn typed(&self) -> Vec<String> {
            self.typed.lock().unwrap().clone()
        }
    }

    impl Keys for FakeKeys {
        async fn type_command(&self, s: &Session, text: &str) -> Result<(), DaemonError> {
            if self.refuse_rename && text.starts_with("/rename ") {
                return Err(DaemonError("no reachable PTY".into()));
            }
            self.typed.lock().unwrap().push(text.to_string());
            if !self.moves {
                return Ok(());
            }
            let path = self.home.job_state(&s.short);
            let mut data: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
            if let Some(dir) = text.strip_prefix("/cd ") {
                data["cwd"] = json!(dir);
                data["originCwd"] = json!(dir);
            } else if let Some(name) = text.strip_prefix("/rename ") {
                data["name"] = json!(name);
            }
            std::fs::write(&path, data.to_string()).unwrap();
            Ok(())
        }

        async fn abandon(&self, s: &Session) {
            self.cleared.lock().unwrap().push(s.short.clone());
        }
    }

    const HOME_TRUSTED: &str = "/nonexistent/home/user";

    fn write_trust(path: &Path, dirs: &[&str]) {
        let projects: serde_json::Map<String, Value> =
            dirs.iter().map(|d| (d.to_string(), json!({"hasTrustDialogAccepted": true}))).collect();
        std::fs::write(path, json!({"projects": projects}).to_string()).unwrap();
    }

    /// A trust file that trusts only a home directory elsewhere.
    fn trust_file(b: &TestBox) {
        write_trust(&b.paths.home.trust_file, &[HOME_TRUSTED]);
    }

    /// A real directory the trust file trusts, as a claim needs both.
    fn trusted(b: &TestBox) -> String {
        let scope = b.root.join("projects").join("awm").join("svc-cx");
        std::fs::create_dir_all(&scope).unwrap();
        write_trust(&b.paths.home.trust_file, &[HOME_TRUSTED, &b.root.to_string_lossy()]);
        scope.to_string_lossy().into_owned()
    }

    fn pool(b: &TestBox) -> Pool<Table, NoTerminals> {
        b.pool_with(|p| p.rotate_age_s = 999_999.0)
    }

    fn home_with_trust(dir: &Path, trust: &Path) -> ClaudeHome {
        ClaudeHome::at(dir.join(".claude"), trust)
    }

    #[tokio::test]
    async fn test_trust_is_inherited_from_an_ancestor() {
        let dir = TempDir::new().unwrap();
        let file = dir.path().join("claude.json");
        let home = dir.path().canonicalize().unwrap().join("home");
        std::fs::create_dir_all(&home).unwrap();
        write_trust(&file, &[&home.to_string_lossy()]);
        let h = home_with_trust(dir.path(), &file);
        assert!(trust::trusted(&home, &h).await);
        assert!(trust::trusted(home.join("anything/at/all"), &h).await);
    }

    #[tokio::test]
    async fn test_a_directory_with_no_trusted_ancestor_is_untrusted() {
        let dir = TempDir::new().unwrap();
        let file = dir.path().join("claude.json");
        write_trust(&file, &[HOME_TRUSTED]);
        let elsewhere = TempDir::new().unwrap();
        assert!(!trust::trusted(elsewhere.path(), &home_with_trust(dir.path(), &file)).await);
    }

    #[tokio::test]
    async fn test_an_unreadable_trust_file_reads_as_untrusted() {
        // Fails closed: the cost is a cold launch, never a dialog nobody answers.
        let dir = TempDir::new().unwrap();
        let home = dir.path().canonicalize().unwrap();
        let h = home_with_trust(dir.path(), &dir.path().join("absent.json"));
        assert!(!trust::trusted(&home, &h).await);
    }

    #[tokio::test]
    async fn test_a_start_in_a_git_repo_needs_the_repos_own_trust_entry() {
        // `claude --bg` refuses with "Workspace not trusted" for a repo whose only trusted parent
        // is an ancestor, so the start must refuse first.
        let dir = TempDir::new().unwrap();
        let root = dir.path().canonicalize().unwrap();
        let repo = root.join("repo");
        std::fs::create_dir_all(&repo).unwrap();
        let init = std::process::Command::new("git").args(["init", "-q"]).current_dir(&repo).status().unwrap();
        assert!(init.success());
        let ancestor = root.join("ancestor.json");
        write_trust(&ancestor, &[&root.to_string_lossy()]);
        let h = home_with_trust(&root, &ancestor);
        assert!(!trust::trusted(&repo, &h).await);
        let why = trust::start_refusal(&repo, &h).await.unwrap();
        assert!(why.contains(&*repo.to_string_lossy()) && why.contains("claude"));

        let own = root.join("own.json");
        write_trust(&own, &[&repo.to_string_lossy()]);
        assert_eq!(trust::start_refusal(&repo, &home_with_trust(&root, &own)).await, None);
    }

    #[tokio::test]
    async fn test_an_untrusted_directory_is_refused_without_touching_a_session() {
        // The dialog default is "No, stay put", and a session sitting on it answers the *next*
        // claim's keystrokes instead of moving.
        let b = TestBox::new();
        trust_file(&b);
        let keys = FakeKeys { moves: false, ..FakeKeys::new(&b) };
        let elsewhere = TempDir::new().unwrap();
        let out = claim(&pool(&b), &keys, &elsewhere.path().to_string_lossy()).await;
        assert_eq!(out["session"], Value::Null);
        assert!(out["reason"].as_str().unwrap().contains("trust"));
        assert!(keys.typed().is_empty());
    }

    #[tokio::test]
    async fn test_a_claim_moves_the_newest_session_and_returns_it() {
        let b = TestBox::new();
        let dir = trusted(&b);
        let out = claim(&pool(&b), &FakeKeys::new(&b), &dir).await;
        assert_eq!(out, json!({"session": "warmfresh", "cwd": dir}));
    }

    #[tokio::test]
    async fn test_two_claims_yield_one_session_and_one_miss() {
        let b = TestBox::new();
        let dir = trusted(&b);
        let (pool, keys) = (pool(&b), FakeKeys::new(&b));
        let first = claim(&pool, &keys, &dir).await;
        let second = claim(&pool, &keys, &dir).await;
        assert_eq!(first["session"], "warmfresh");
        assert_eq!(second["session"], Value::Null);
    }

    #[tokio::test]
    async fn test_a_session_that_would_not_move_is_left_idle() {
        let b = TestBox::new();
        let dir = trusted(&b);
        let keys = FakeKeys { moves: false, ..FakeKeys::new(&b) };
        let out = claim_within(&pool(&b), &keys, &dir, Duration::from_millis(50)).await;
        assert_eq!(out["session"], Value::Null);
        assert_eq!(*keys.cleared.lock().unwrap(), ["warmfresh"]);
    }

    #[tokio::test]
    async fn test_a_claim_renames_the_session_out_of_the_pool() {
        // The whole point: the session stops advertising itself as a spare.
        let b = TestBox::new();
        let dir = trusted(&b);
        let (pool, keys) = (pool(&b), FakeKeys::new(&b));
        let out = claim(&pool, &keys, &dir).await;
        assert_eq!(out["session"], "warmfresh");
        assert_eq!(keys.typed(), [format!("/cd {dir}"), "/rename claimed dunlin".into()]);
        let s = pool.sessions();
        let fresh = s.load().into_iter().find(|x| x.short == "warmfresh").unwrap();
        assert_eq!(fresh.name, "claimed dunlin");
        assert!(!s.is_ours(&fresh));
        assert!(s.was_ours(&fresh));
    }

    #[tokio::test]
    async fn test_the_rename_comes_after_the_move_has_landed() {
        // The recheck between them asks whether the name still carries the pool's prefix, and
        // the rename is what takes it away.
        let b = TestBox::new();
        let dir = trusted(&b);
        let keys = FakeKeys::new(&b);
        claim(&pool(&b), &keys, &dir).await;
        let typed = keys.typed();
        let at = |t: &str| typed.iter().position(|x| x == t).unwrap();
        assert!(at(&format!("/cd {dir}")) < at("/rename claimed dunlin"));
    }

    #[tokio::test]
    async fn test_a_rename_that_will_not_type_still_hands_the_session_over() {
        // A session under the wrong name beats no session at all.
        let b = TestBox::new();
        let dir = trusted(&b);
        let keys = FakeKeys { refuse_rename: true, ..FakeKeys::new(&b) };
        let out = claim(&pool(&b), &keys, &dir).await;
        assert_eq!(out, json!({"session": "warmfresh", "cwd": dir}));
    }

    #[test]
    fn test_the_claimed_name_keeps_the_pool_noun() {
        let p = Paths::under(Path::new("/nonexistent/cch-test"));
        assert_eq!(claimed_name(&p, "<warm dunlin>", "abc123"), "claimed dunlin");
        assert_eq!(claimed_name(&p, "<warm >", "abc123"), "claimed abc123");
    }

    #[test]
    fn the_claim_timing_ladder_holds() {
        // MOVE_TIMEOUT < the pool_claim verb timeout < CX_WAIT in bin/cx: cutting the read off
        // early turns a slow warm start into a cold one.
        let verb = crate::verbs::specs().into_iter().find(|s| s.verb == "pool_claim").unwrap();
        let timeout = Duration::from_secs(verb.timeout.unwrap());
        let src = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/bin/cx")).unwrap();
        let wait: u64 = src.split("CX_WAIT=${CX_WAIT:-").nth(1).unwrap().split('}').next().unwrap().parse().unwrap();
        assert!(MOVE_TIMEOUT < timeout, "{MOVE_TIMEOUT:?} !< {timeout:?}");
        assert!(timeout < Duration::from_secs(wait), "{timeout:?} !< CX_WAIT {wait}s");
    }

    #[test]
    fn the_claim_future_is_send() {
        fn send<T: Send>(_: T) {}
        let b = TestBox::new();
        let pool = pool(&b);
        send(claim(&pool, &DaemonKeys, "/"));
    }
}
