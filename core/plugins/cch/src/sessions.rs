//! What the warm pool knows about a background Claude Code session, and the predicates that
//! decide whether one may be handed out or thrown away. Pure reads: nothing here starts, moves or
//! deletes a session.
//!
//! Two files describe a session and they do not agree. The roster's `dispatch.seed.name` is what
//! a session was called at creation and never changes; the job's own `state.json` is the only
//! thing that follows a rename. *Who does this session belong to now* is the state record's: a
//! pool session renamed and talked to for thousands of tokens still reads `<warm ...>` in the
//! roster, and reading the name from there would hand that conversation to the next terminal.
//! *Did the pool make this session* is the roster's, because the pool renames a session itself
//! when it hands it out. So [`Sessions::is_ours`] decides who may be handed a session and
//! [`Sessions::was_ours`] decides what the pool may collect. Confusing the two is how a claimed
//! session either gets handed out twice or leaks forever.

use std::collections::BTreeSet;
use std::time::{SystemTime, UNIX_EPOCH};

use awm_harness::claude::roster::{self, Job};

pub use awm_harness::claude::detect::attached_shorts;

use crate::lineage::Processes;
use crate::paths::Paths;

/// A background session as the roster and its own record describe it.
pub type Session = Job;

/// What a never-prompted session's record says it needs.
const PRISTINE_NEEDS: &str = "send a prompt to start";

/// The pool's view of its sessions, bound to cch's paths and the process table.
#[derive(Clone, Copy)]
pub struct Sessions<'a> {
    pub paths: &'a Paths,
    pub procs: &'a dyn Processes,
}

fn epoch_now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
}

/// Has nobody prompted this session or taken it anywhere?
///
/// `tokens` and `origin_cwd` are the load-bearing witnesses: they survive a stop, a retire and a
/// respawn. `needs` and `intent` are cleared or rewritten by a stop, so they join the test as an
/// AND and do not carry it. `origin_cwd` appears the first time a session is moved, and a moved
/// session already carries the CLAUDE.md of where it was taken.
///
/// CAUTION: `first_terminal_at` is not a witness of a terminal. It is stamped when the job first
/// reaches a terminal *state*, done or failed, and stays null for a session nobody prompts.
/// Attachment has its own answer in [`attached_shorts`].
pub fn is_untouched(s: &Session) -> bool {
    s.tokens == 0
        && s.origin_cwd.is_none()
        && s.intent.is_empty()
        && s.needs.as_deref() == Some(PRISTINE_NEEDS)
        && s.first_terminal_at.is_none()
}

/// Seconds since the daemon started this session at `now` (default: the wall clock), or 0 if it
/// never said.
pub fn age_s(s: &Session, now: Option<f64>) -> f64 {
    match s.started_at_ms {
        None | Some(0) => 0.0,
        Some(ms) => (now.unwrap_or_else(epoch_now) - ms as f64 / 1000.0).max(0.0),
    }
}

impl<'a> Sessions<'a> {
    pub fn new(paths: &'a Paths, procs: &'a dyn Processes) -> Self {
        Self { paths, procs }
    }

    /// Is the process the roster recorded still the one running under that pid? The start time
    /// pins the identity: without it a recycled pid passes for a session that is gone, and the
    /// pool hands out a dead id.
    pub fn is_alive(&self, s: &Session) -> bool {
        let Some(recorded) = s.repl_proc_start.as_deref().filter(|p| !p.is_empty()) else { return false };
        s.repl_pid.and_then(|pid| self.procs.start(pid)).as_deref() == Some(recorded)
    }

    /// Does this session's own record still carry the pool's name prefix? A rename takes a
    /// session out of the pool by this alone. This is the claimability test: a session carrying
    /// any other name must never be handed to a second terminal.
    pub fn is_ours(&self, s: &Session) -> bool {
        s.name.starts_with(&self.paths.name_prefix)
    }

    /// Did the pool create this session, whatever it is called now? Read from the roster's seed
    /// name, which still answers after the pool's own rename on hand-out. Use it only to decide
    /// what may be collected, never what may be handed out.
    pub fn was_ours(&self, s: &Session) -> bool {
        s.seed_name.as_deref().unwrap_or("").starts_with(&self.paths.name_prefix)
    }

    /// May this session be handed to a terminal? Fails closed when the binary's `version` cannot
    /// be resolved: a session seeded by a binary that is no longer installed will not attach.
    pub fn claimable(&self, s: &Session, version: Option<&str>, now: Option<f64>) -> bool {
        let Some(version) = version else { return false };
        self.is_ours(s)
            && is_untouched(s)
            && self.is_alive(s)
            && s.cli_version.as_deref() == Some(version)
            && age_s(s, now) < self.paths.rotate_age_s
    }

    /// May this session be deleted? `attached` defaults to [`attached_shorts`].
    ///
    /// Deliberately not the negation of [`Self::claimable`]: "delete whatever cannot be claimed"
    /// would delete the session somebody is attached to and typing into. Keyed on `was_ours`, and
    /// nothing else widens with it: a session carrying tokens, an intent or a terminal is refused
    /// whoever named it.
    ///
    /// Three ways to qualify: the process is gone; the session was never moved and is stale by
    /// version or by age, which is ordinary rotation; or it was claimed, never spoken to, and has
    /// outlived the rotate age.
    ///
    /// An unresolvable `version` drops the version clause. Failing closed on it would make every
    /// session look stale the moment the binary's symlink changed shape, and the pool would delete
    /// itself on a loop.
    pub fn removable(
        &self,
        s: &Session,
        version: Option<&str>,
        now: Option<f64>,
        attached: Option<&BTreeSet<String>>,
    ) -> bool {
        if !self.was_ours(s) || s.tokens != 0 || !s.intent.is_empty() {
            return false;
        }
        if !self.is_alive(s) {
            return true;
        }
        let attached_now;
        let attached = match attached {
            Some(a) => a,
            None => {
                attached_now = attached_shorts();
                &attached_now
            }
        };
        if attached.contains(&s.short) {
            return false;
        }
        if age_s(s, now) >= self.paths.rotate_age_s {
            return true;
        }
        let stale_version = version.is_some_and(|v| s.cli_version.as_deref() != Some(v));
        s.origin_cwd.is_none() && stale_version
    }

    /// The version directory the configured `claude` resolves to, such as `2.1.268`.
    pub fn binary_version(&self) -> Option<String> {
        roster::binary_version(&self.paths.claude_bin)
    }

    /// The pid of the running Claude Code daemon, or None if there is none.
    pub fn daemon_pid(&self) -> Option<u32> {
        roster::daemon_pid(&self.paths.home)
    }

    /// Every background session the daemon knows about, newest last.
    pub fn load(&self) -> Vec<Session> {
        roster::load(&self.paths.home)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;
    use tempfile::TempDir;

    const VERSION: &str = "2.1.268";
    const ALIVE_PID: u32 = 4242;
    const ALIVE_START: &str = "777";
    const BASE_MS: i64 = 1_700_000_000_000;

    /// One live process, `ALIVE_PID`; every other pid is gone.
    struct Table;

    impl Processes for Table {
        fn start(&self, pid: u32) -> Option<String> {
            (pid == ALIVE_PID).then(|| ALIVE_START.to_string())
        }
        fn ppid(&self, _: u32) -> Option<u32> {
            None
        }
        fn may_be_a_session(&self, _: u32) -> bool {
            false
        }
    }

    fn pristine(short: &str, name: &str, seed: &str, cli: &str, offset_ms: i64) -> Session {
        Session {
            short: short.into(),
            has_record: true,
            name: name.into(),
            tokens: 0,
            intent: String::new(),
            needs: Some(PRISTINE_NEEDS.into()),
            origin_cwd: None,
            cwd: Some("/home/user".into()),
            first_terminal_at: None,
            repl_pid: Some(ALIVE_PID),
            repl_proc_start: Some(ALIVE_START.into()),
            started_at_ms: Some(BASE_MS + offset_ms),
            cli_version: Some(cli.into()),
            pty_sock: Some("/tmp/pty-0.sock".into()),
            pty_auth: Some("not-a-secret".into()),
            dec_modes: Vec::new(),
            session_id: Some(format!("00000000-0000-4000-8000-00000000000{}", offset_ms / 1000)),
            source: Some("cli".into()),
            seed_name: Some(seed.into()),
            bridge_session_id: None,
        }
    }

    /// The five sessions of the captured box, synthesized: a fresh spare, a spare somebody
    /// adopted and prompted, a claim abandoned before its first prompt, the same claim after its
    /// process died, and a claim that has just been made.
    fn by_short(short: &str) -> Session {
        match short {
            "warmfresh" => pristine("warmfresh", "<warm aardvark>", "<warm aardvark>", VERSION, 4000),
            "adopted" => Session {
                tokens: 31_000,
                needs: None,
                origin_cwd: Some("/work/alpha".into()),
                first_terminal_at: Some("2026-01-01T00:00:00.000Z".into()),
                ..pristine("adopted", "pair session", "<warm zebu>", VERSION, 1000)
            },
            "stranded" => Session {
                origin_cwd: Some("/work/beta".into()),
                ..pristine("stranded", "claimed newt", "<warm newt>", "2.1.267", 0)
            },
            "strandeddead" => Session {
                origin_cwd: Some("/work/beta".into()),
                repl_pid: Some(2),
                repl_proc_start: Some("1".into()),
                ..pristine("strandeddead", "claimed ibex", "<warm ibex>", "2.1.267", 2000)
            },
            "claimedfresh" => Session {
                origin_cwd: Some("/work/gamma".into()),
                ..pristine("claimedfresh", "claimed tapir", "<warm tapir>", VERSION, 5000)
            },
            other => panic!("no session {other}"),
        }
    }

    /// A clock reading a second after the session started.
    fn just_after(s: &Session) -> f64 {
        s.started_at_ms.unwrap_or(0) as f64 / 1000.0 + 1.0
    }

    fn paths() -> Paths {
        Paths::under(Path::new("/nonexistent/cch-test"))
    }

    #[test]
    fn test_identity_comes_from_the_state_record_not_the_roster() {
        let p = paths();
        let pool = Sessions::new(&p, &Table);
        let adopted = by_short("adopted");
        assert_eq!(adopted.seed_name.as_deref(), Some("<warm zebu>"));
        assert_eq!(adopted.name, "pair session");
        assert!(!pool.is_ours(&adopted));
    }

    #[test]
    fn test_a_session_the_pool_never_made_is_never_collected() {
        let p = paths();
        let pool = Sessions::new(&p, &Table);
        let s = Session { seed_name: None, name: "mine".into(), ..by_short("warmfresh") };
        assert!(!pool.was_ours(&s));
        assert!(!pool.removable(&s, Some(VERSION), Some(just_after(&s) + 1e6), None));
    }

    #[test]
    fn test_an_adopted_session_is_never_claimed_and_never_deleted() {
        let p = paths();
        let pool = Sessions::new(&p, &Table);
        let s = by_short("adopted");
        let now = Some(just_after(&s));
        assert!(!pool.claimable(&s, Some(VERSION), now));
        assert!(!pool.removable(&s, Some(VERSION), now, None));
    }

    #[test]
    fn test_a_live_session_that_was_just_claimed_is_left_alone() {
        let p = paths();
        let pool = Sessions::new(&p, &Table);
        let s = by_short("claimedfresh");
        let now = Some(just_after(&s));
        assert!(s.origin_cwd.is_some());
        assert!(!pool.claimable(&s, Some(VERSION), now));
        assert!(!pool.removable(&s, Some(VERSION), now, None));
    }

    #[test]
    fn test_a_claim_with_a_terminal_on_it_is_never_collected() {
        // Somebody who attached, walked away, and came back to an empty composer.
        let p = paths();
        let pool = Sessions::new(&p, &Table);
        let s = by_short("stranded");
        let old = just_after(&s) + p.rotate_age_s;
        let attached = BTreeSet::from(["stranded".to_string()]);
        assert!(!pool.removable(&s, Some(VERSION), Some(old), Some(&attached)));
    }

    #[test]
    fn test_an_unresolvable_binary_does_not_make_everything_removable() {
        // Failing closed both ways would have the pool delete itself on a loop.
        let p = paths();
        let pool = Sessions::new(&p, &Table);
        let s = by_short("warmfresh");
        assert!(!pool.removable(&s, None, Some(just_after(&s)), None));
    }

    #[test]
    fn test_a_recycled_pid_does_not_pass_for_a_live_session() {
        let p = paths();
        let pool = Sessions::new(&p, &Table);
        let s = Session { repl_proc_start: Some("1".into()), ..by_short("warmfresh") };
        assert!(!pool.is_alive(&s));
    }

    #[test]
    fn test_no_roster_means_no_daemon_and_no_sessions() {
        let dir = TempDir::new().unwrap();
        let p = Paths::under(dir.path());
        let pool = Sessions::new(&p, &Table);
        assert_eq!(pool.daemon_pid(), None);
        assert!(pool.load().is_empty());
    }

}
