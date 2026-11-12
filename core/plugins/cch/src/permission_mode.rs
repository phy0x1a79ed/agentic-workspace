//! Put the calling session back into bypass-permissions mode by walking the Shift+Tab cycle.
//!
//! The cycle's length depends on where it starts and on which modes the session offers, so the
//! walk never counts presses: it reads the mode the footer shows, presses once, and reads again.

use std::collections::HashMap;
use std::sync::{Arc, LazyLock, Mutex};
use std::time::Duration;

use serde::Serialize;

use crate::guards;
use crate::lane::{Key, Lane, LaneError, Opener};
use crate::target::{ResolveError, Resolve, Target};

/// How the TUI footer names each permission mode, in the order a tie would be broken. TUI copy:
/// it can move under a CLI update, and an unrecognised footer reads as unknown, which refuses.
/// `default` draws no indicator before Claude Code 2.1.310 and "manual mode on" from it.
const MODE_MARKERS: [(&str, &[&str]); 5] = [
    ("default", &["manual mode on"]),
    ("bypassPermissions", &["bypass permissions on"]),
    ("acceptEdits", &["accept edits on"]),
    ("plan", &["plan mode on"]),
    ("auto", &["auto mode on"]),
];

/// The mode the walk aims for.
pub const TARGET_MODE: &str = "bypassPermissions";

/// What [`classify`] answers when no mode indicator is readable.
pub const UNKNOWN: &str = "unknown";

// The cycle visits at most five modes, so a lap plus one; it only stops a session that cannot
// reach bypass from being cycled forever.
const MAX_STEPS: usize = 6;

// Every step changes the indicator, so no change within this budget means the press did not land.
const REDRAW_POLLS: usize = 10;
const REDRAW_POLL: Duration = Duration::from_millis(100);

/// How long a walk waits for another walk on the same session to finish.
pub const WALK_WAIT: Duration = Duration::from_secs(10);

// The footer is the last thing drawn; a window is cheaper and less likely to match stale copy.
const TAIL_CHARS: usize = 4000;

/// Name the permission mode `screen` shows, or [`UNKNOWN`].
///
/// Only text under the last prompt box counts (above it is transcript, which can quote "bypass
/// permissions"), and the last marker there wins, since a capture still holds earlier footer
/// paints. No prompt box means a dialog is drawn in its place: unknown. Pass a rendered screen
/// (`guards::current_screen`), not a raw pty stream, whose spinner repaints push the footer out of
/// the window.
pub fn classify(screen: &str) -> &'static str {
    let Some(footer) = guards::under_prompt_box(screen) else {
        return UNKNOWN;
    };
    let chars: Vec<char> = footer.chars().collect();
    let tail: String = chars[chars.len().saturating_sub(TAIL_CHARS)..].iter().collect::<String>().to_lowercase();
    let (mut best, mut best_at) = (UNKNOWN, None);
    for (mode, markers) in MODE_MARKERS {
        let at = markers.iter().filter_map(|m| tail.rfind(m)).max();
        if at.is_some() && at > best_at {
            (best, best_at) = (mode, at);
        }
    }
    best
}

/// What a walk did: whether any key was pressed, where the session ended up, and how.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ModeResult {
    pub ok: bool,
    pub changed: bool,
    pub mode: String,
    /// How many times Shift+Tab was sent.
    pub steps: usize,
    pub session: String,
    pub hosting: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub started_as: Option<String>,
    /// Every mode read, the start first.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

impl ModeResult {
    fn new(ok: bool, changed: bool, mode: &str, steps: usize, session: String, target: &Target) -> Self {
        Self {
            ok,
            changed,
            mode: mode.into(),
            steps,
            session,
            hosting: target.hosting().into(),
            started_as: None,
            path: None,
            detail: None,
            error: None,
        }
    }
}

static WALK_LOCKS: LazyLock<Mutex<HashMap<String, Arc<tokio::sync::Mutex<()>>>>> = LazyLock::new(Default::default);

/// The lock that serialises walks on `target`'s session. Two walks on one session must never
/// overlap: each reads the footer the other's press just changed, and between them they step past
/// bypass and each concludes the cycle never offered it.
pub fn walk_lock(target: &Target) -> Arc<tokio::sync::Mutex<()>> {
    let key = match target.session_id() {
        "" => format!("{target:?}"),
        sid => sid.to_string(),
    };
    WALK_LOCKS.lock().unwrap().entry(key).or_default().clone()
}

/// Cycle the calling session's permission mode until it reads as bypass.
///
/// `expect_session` narrows which session the pid may resolve to and never redirects (see
/// [`Resolve::resolve`]). A walk that waited for another one starts from a fresh read, which is
/// usually "already in bypass".
pub async fn ensure_bypass<H: Resolve + Opener>(
    host: &H,
    caller_pid: Option<u32>,
    expect_session: Option<&str>,
) -> Result<ModeResult, LaneError> {
    let Some(pid) = caller_pid.filter(|p| *p != 0) else {
        return Err(ResolveError(
            "could not tell which session is calling, so there is no permission mode to change. Reflection acts \
             on the caller's own session only"
                .into(),
        )
        .into());
    };
    let target = host.resolve(pid, expect_session).await?;
    let lock = walk_lock(&target);
    let Ok(_held) = tokio::time::timeout(WALK_WAIT, lock.lock_owned()).await else {
        let session = target.name().filter(|n| !n.is_empty()).unwrap_or(target.session_id()).to_string();
        let mut res = ModeResult::new(false, false, UNKNOWN, 0, session, &target);
        res.error = Some("another permission-mode change on this session is still running. Nothing was sent.".into());
        return Ok(res);
    };
    let mut sess = host.open_session(&target).await?;
    let res = walk(&mut sess, &target).await;
    sess.close().await;
    res
}

async fn footer(sess: &mut impl Lane) -> Result<&'static str, LaneError> {
    Ok(classify(&guards::current_screen(&sess.glance().await?)))
}

async fn await_change(sess: &mut impl Lane, before: &str) -> Result<&'static str, LaneError> {
    let mut now = UNKNOWN;
    for _ in 0..REDRAW_POLLS {
        tokio::time::sleep(REDRAW_POLL).await;
        now = footer(sess).await?;
        if now != before {
            break;
        }
    }
    Ok(now)
}

async fn walk(sess: &mut impl Lane, target: &Target) -> Result<ModeResult, LaneError> {
    let label = target.label();
    let mut started_as = classify(&guards::current_screen(&sess.read().await?));
    // A session busy for a while painted its footer before anything the daemon replays to us.
    if started_as == UNKNOWN && sess.repaint().await? {
        started_as = classify(&guards::current_screen(&sess.read().await?));
    }
    if started_as == TARGET_MODE {
        let mut res = ModeResult::new(true, false, TARGET_MODE, 0, label, target);
        res.detail = Some("already in bypass-permissions mode".into());
        return Ok(res);
    }
    if started_as == UNKNOWN {
        // No indicator almost always means a modal covers the footer, and a modal navigates on
        // keystrokes: pressing blind could select an arbitrary entry.
        let mut res = ModeResult::new(false, false, UNKNOWN, 0, label, target);
        res.error = Some(
            "cannot see this session's permission-mode indicator, so the mode cannot be changed safely — on a \
             terminal session that means a modal or prompt is covering the footer and would swallow the \
             keystroke; on a background session it usually means the pty stream carries no footer paint to read. \
             Nothing was sent."
                .into(),
        );
        return Ok(res);
    }

    let mut seen = vec![started_as];
    for step in 1..=MAX_STEPS {
        sess.press(Key::ShiftTab).await?;
        let now = await_change(sess, seen[seen.len() - 1]).await?;
        seen.push(now);
        if now == TARGET_MODE {
            tracing::info!("reflection: {label} → bypass permissions in {step} step(s) ({})", seen.join(" → "));
            let mut res = ModeResult::new(true, true, TARGET_MODE, step, label, target);
            res.started_as = Some(started_as.into());
            res.path = Some(seen.iter().map(|m| m.to_string()).collect());
            return Ok(res);
        }
        // Home again without passing bypass: this session does not offer it. Stop where it began.
        if step > 1 && now == started_as {
            break;
        }
    }
    tracing::warn!("reflection: {label} never offered bypass permissions (saw {})", seen.join(" → "));
    let mut res = ModeResult::new(false, true, seen[seen.len() - 1], seen.len() - 1, label, target);
    res.started_as = Some(started_as.into());
    res.path = Some(seen.iter().map(|m| m.to_string()).collect());
    res.error = Some(
        "this session's permission-mode cycle never offers bypass permissions, so it cannot be switched into it. \
         That is usually a session that was not started with --dangerously-skip-permissions, or one where bypass \
         is disabled. Nothing else was changed."
            .into(),
    );
    Ok(res)
}

#[cfg(test)]
mod tests {


    use super::*;
    use crate::target::TmuxLane;

    fn bx() -> String {
        format!("{}\n❯ \n{}", "─".repeat(40), "─".repeat(40))
    }

    #[test]
    fn test_classify_reads_the_most_recent_indicator() {
        let screen = format!("{}\n⏸ plan mode on (shift+tab to cycle)\n⏵⏵ bypass permissions on", bx());
        assert_eq!(classify(&screen), "bypassPermissions");
        let screen = format!("{}\n⏵⏵ bypass permissions on\n⏵⏵ auto mode on (shift+tab to cycle)", bx());
        assert_eq!(classify(&screen), "auto");
    }

    #[test]
    fn test_classify_unknown_when_a_dialog_replaces_the_prompt_box() {
        // A trust dialog is drawn where the box was, under a notice that says "bypass permissions".
        let screen = "● Session was started with bypass permissions, so plan mode won't ask.\n   ❯ No, stay put\n     \
                      Yes, move here\n⏵⏵ bypass permissions on";
        assert_eq!(classify(screen), "unknown");
    }

    // A session whose displayed mode advances through a scripted cycle.
    #[derive(Default)]
    struct ModeState {
        cycle: Vec<&'static str>,
        at: usize,
        lag: usize,
        stale: usize,
        hidden: bool,
        presses: usize,
        repaints: usize,
        closed: bool,
    }

    #[derive(Clone, Default)]
    struct FakeModeSession(Arc<Mutex<ModeState>>);

    fn paint(mode: &str) -> String {
        if mode == "unknown" {
            return "Esc to cancel".into();
        }
        let label = match mode {
            "bypassPermissions" => "⏵⏵ bypass permissions on",
            "acceptEdits" => "⏵⏵ accept edits on",
            "plan" => "⏸ plan mode on",
            "auto" => "⏵⏵ auto mode on",
            "manual" => "⏸ manual mode on",
            // Before 2.1.310 `default` draws no indicator, indistinguishable from an unread footer.
            "default" => "",
            other => panic!("no paint for {other}"),
        };
        format!("{}\n{label}", bx())
    }

    impl FakeModeSession {
        fn new(cycle: &[&'static str], lag: usize, hidden: bool) -> Self {
            Self(Arc::new(Mutex::new(ModeState { cycle: cycle.to_vec(), lag, hidden, ..ModeState::default() })))
        }

        fn state(&self) -> std::sync::MutexGuard<'_, ModeState> {
            self.0.lock().unwrap()
        }

        fn screen(&self) -> String {
            let s = self.state();
            if s.hidden {
                return "✻ Working… (13m 43s)".into();
            }
            paint(s.cycle[s.at])
        }
    }

    impl Lane for FakeModeSession {
        fn label(&self) -> String {
            "fake".into()
        }
        async fn write(&mut self, _text: &str) -> Result<(), LaneError> {
            Ok(())
        }
        async fn clear(&mut self) -> Result<(), LaneError> {
            Ok(())
        }
        async fn check_not_rejected(&mut self) -> Result<(), LaneError> {
            Ok(())
        }
        async fn commit(&mut self) -> Result<(), LaneError> {
            Ok(())
        }
        async fn read(&mut self) -> Result<String, LaneError> {
            Ok(self.screen())
        }
        async fn glance(&mut self) -> Result<String, LaneError> {
            let stale = {
                let mut s = self.state();
                (s.stale > 0).then(|| {
                    s.stale -= 1;
                    let n = s.cycle.len();
                    paint(s.cycle[(s.at + n - 1) % n])
                })
            };
            Ok(stale.unwrap_or_else(|| self.screen()))
        }
        async fn repaint(&mut self) -> Result<bool, LaneError> {
            let mut s = self.state();
            if !s.hidden {
                return Ok(false);
            }
            s.repaints += 1;
            s.hidden = false;
            Ok(true)
        }
        async fn press(&mut self, key: Key) -> Result<(), LaneError> {
            assert_eq!(key, Key::ShiftTab);
            let mut s = self.state();
            s.presses += 1;
            s.at = (s.at + 1) % s.cycle.len();
            s.stale = s.lag;
            Ok(())
        }
        async fn close(self) {
            self.state().closed = true;
        }
    }

    // Resolves every pid to `target` and opens `sess` for it.
    struct FakeHost {
        target: Target,
        sess: FakeModeSession,
    }

    impl Resolve for FakeHost {
        async fn resolve(&self, _pid: u32, _expect: Option<&str>) -> Result<Target, LaneError> {
            Ok(self.target.clone())
        }
    }

    impl Opener for FakeHost {
        type Lane = FakeModeSession;
        async fn open_writer(&self, _t: &Target) -> Result<FakeModeSession, LaneError> {
            Ok(self.sess.clone())
        }
        async fn open_session(&self, _t: &Target) -> Result<FakeModeSession, LaneError> {
            Ok(self.sess.clone())
        }
    }

    // Each test walks its own session id: the lock table is shared by every test in the process.
    fn tmux_target(sid: &str) -> Target {
        Target::Tmux(TmuxLane { pane: "%1".into(), session_id: sid.into(), repl_pid: 1234, name: None })
    }

    async fn run_mode(sid: &str, cycle: &[&'static str], lane: Option<Target>, lag: usize, hidden: bool) -> (ModeResult, FakeModeSession) {
        let sess = FakeModeSession::new(cycle, lag, hidden);
        let host = FakeHost { target: lane.unwrap_or_else(|| tmux_target(sid)), sess: sess.clone() };
        (ensure_bypass(&host, Some(1234), None).await.unwrap(), sess)
    }

    const FROM_AUTO: [&str; 5] = ["auto", "default", "acceptEdits", "plan", "bypassPermissions"];

    #[tokio::test(start_paused = true)]
    async fn test_mode_is_a_noop_when_already_in_bypass() {
        let (res, sess) = run_mode("noop", &["bypassPermissions", "auto"], None, 0, false).await;
        assert!(res.ok && !res.changed && res.steps == 0);
        assert_eq!(sess.state().presses, 0);
    }

    #[tokio::test(start_paused = true)]
    async fn test_mode_cycles_from_auto_to_bypass() {
        // Entered at `auto`, where a plan approved from a phone leaves the session. `default` reads
        // as unknown on the way past, and that must not stop the walk.
        let (res, sess) = run_mode("from-auto", &FROM_AUTO, None, 0, false).await;
        assert!(res.ok && res.mode == "bypassPermissions");
        assert_eq!((sess.state().presses, res.steps), (4, 4));
    }

    #[tokio::test(start_paused = true)]
    async fn test_mode_gives_up_when_bypass_is_not_in_the_cycle() {
        let (res, sess) = run_mode("no-bypass", &["auto", "acceptEdits", "plan"], None, 0, false).await;
        assert!(!res.ok);
        assert!(res.error.as_deref().unwrap().contains("never offers bypass"));
        assert_eq!(res.mode, "auto", "must come back to where it started");
        assert!(sess.state().closed);
    }

    #[tokio::test(start_paused = true)]
    async fn test_mode_refuses_when_the_indicator_is_not_visible() {
        // A modal covers the footer and swallows keystrokes.
        let (res, sess) = run_mode("covered", &["unknown", "auto"], None, 0, false).await;
        assert!(!res.ok && res.mode == "unknown");
        assert_eq!(sess.state().presses, 0, "nothing may be sent when we cannot see the mode");
    }

    #[tokio::test]
    async fn test_mode_refuses_without_a_caller() {
        let host = FakeHost { target: tmux_target("no-caller"), sess: FakeModeSession::new(&["auto"], 0, false) };
        let err = ensure_bypass(&host, None, None).await.unwrap_err();
        assert!(err.to_string().contains("acts on the caller"));
    }

    // A session whose every open is counted, to see whether two walks ever hold it at once.

    #[tokio::test(start_paused = true)]
    async fn test_mode_refuses_when_another_walk_holds_the_session() {
        let target = tmux_target("busy");
        let host = FakeHost { target: target.clone(), sess: FakeModeSession::new(&["auto"], 0, false) };
        let lock = walk_lock(&target);
        let _held = lock.lock().await;
        let res = ensure_bypass(&host, Some(1234), None).await.unwrap();
        assert!(!res.ok && res.error.as_deref().unwrap().contains("still running"));
    }

}
