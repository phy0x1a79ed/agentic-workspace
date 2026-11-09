//! What cch refuses to type into a session, and the resume it enforces.
//!
//! The rules are about the agent TUI, not the lane a keystroke takes: a tmux pane and a
//! background job show the same dialogs and lose context to the same commands.

use serde_json::{json, Value};

use crate::termscreen;

/// Slash commands that irreversibly discard context or end the session. Refused without
/// `confirm`.
pub const DESTRUCTIVE: [&str; 3] = ["/clear", "/exit", "/quit"];

/// Slash commands that open a modal or picker. The modal swallows pasted input and Enter, so
/// cch cannot drive it and would leave the session frozen until someone presses Escape.
/// A curated list: extend it as new modal commands appear. Sorted.
pub const INTERACTIVE: [&str; 16] = [
    "/agents",
    "/bashes",
    "/config",
    "/doctor",
    "/hooks",
    "/login",
    "/logout",
    "/mcp",
    "/permissions",
    "/rc",
    "/remote-control",
    "/resume",
    "/status",
    "/statusline",
    "/theme",
    "/vim",
];

/// Commands that act directly with an argument but open a chooser when bare.
const MODAL_WHEN_BARE: [&str; 1] = ["/model"];

/// The `/mcp` forms that act without opening the panel (Claude Code 2.1.318).
pub const MCP_ACTIONS: [&str; 3] = ["reconnect", "enable", "disable"];

/// A bare slash command leaves an autonomous session idle once it completes, so a
/// self-directed one is trailed by this prompt unless the caller gives its own.
pub const DEFAULT_FOLLOWUP: &str = "Continue with what you were doing.";

/// Empty text: a caller bug, not a refusal.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("text is required")]
pub struct EmptyText;

/// True if `text` is a slash command (a TUI control command, not a prompt).
pub fn is_slash(text: &str) -> bool {
    text.trim().starts_with('/')
}

/// The `/mcp` line that runs `action` on `server`, a name or `all`. Errors name the bad input.
pub fn mcp_line(action: &str, server: &str) -> Result<String, String> {
    if !MCP_ACTIONS.contains(&action) {
        return Err(format!("action must be one of {}", MCP_ACTIONS.join(", ")));
    }
    let server = server.trim();
    if server.is_empty() || server.chars().any(|c| c.is_control() || c.is_whitespace()) {
        return Err("server must be one MCP server name, or all".into());
    }
    Ok(format!("/mcp {action} {server}"))
}

/// True if `text` opens a modal that traps pasted input.
pub fn opens_modal(text: &str) -> bool {
    let parts: Vec<&str> = text.split_whitespace().collect();
    let Some(first) = parts.first() else { return false };
    INTERACTIVE.contains(first) || (MODAL_WHEN_BARE.contains(first) && parts.len() == 1)
}

/// The refusal result for `text`, or `None` when it may be typed. `allow_modal` is for a verb
/// that drives the modal it opens (the Remote Control verbs); a free-text caller never sets it.
pub fn refusal(text: &str, confirm: bool, allow_modal: bool) -> Result<Option<Value>, EmptyText> {
    let Some(first) = text.split_whitespace().next() else { return Err(EmptyText) };
    let shown = py_repr(first);
    if DESTRUCTIVE.contains(&first) && !confirm {
        return Ok(Some(json!({
            "ok": false,
            "refused": true,
            "reason": format!("{shown} irreversibly discards context; pass confirm=true to proceed."),
            "guard": DESTRUCTIVE,
        })));
    }
    if first == "/mcp" && !allow_modal {
        return Ok(Some(json!({
            "ok": false,
            "refused": true,
            "kind": "interactive",
            "reason": "bare '/mcp' opens a panel that swallows typed input. Use reflection(verb=\"mcp\", \
                       args={action: reconnect|enable|disable, server: <name>|all, job?}) to reconnect, \
                       enable or disable a session's MCP servers.",
            "guard": ["/mcp"],
        })));
    }
    if !allow_modal && opens_modal(text) {
        return Ok(Some(json!({
            "ok": false,
            "refused": true,
            "kind": "interactive",
            "reason": format!(
                "{shown} opens an interactive modal/picker that swallows pasted input; reflection cannot \
                 navigate it and it would freeze the session (only a hand-typed Esc recovers). Run it by hand."
            ),
            "guard": INTERACTIVE,
        })));
    }
    Ok(None)
}

/// The prompt typed once a slash command completes.
pub fn resume_text(followup: Option<&str>) -> String {
    match followup.map(str::trim) {
        Some(f) if !f.is_empty() => f.to_string(),
        _ => DEFAULT_FOLLOWUP.to_string(),
    }
}

/// Copy a modal or permission prompt paints that the ordinary prompt screen does not. TUI copy
/// moves under CLI updates, and a stale list fails toward "no modal seen", which is why the
/// record's `waiting` status is asked as well before anything is typed.
const MODAL_MARKERS: [&str; 8] = [
    "esc to cancel",
    "enter to confirm",
    "enter to select",
    "to navigate",
    "do you want to",
    "disconnect this session",
    // The confirm dialogs Claude Code 2.1.x opens when a conversation with output changes its
    // effort or model. `control` answers them; every other path treats them as a modal.
    "change effort level?",
    "switch model?",
];
const SCREEN_TAIL_CHARS: usize = 2500;
const PROMPT_BOX_MAX_LINES: usize = 12;

/// The text a terminal would show after `screen`, a pty stream or a capture. A stream holds
/// every dialog opened and closed since the connection began, so it is replayed onto a grid.
pub fn current_screen(screen: &str) -> String {
    if screen.contains('\x1b') {
        termscreen::render(screen)
    } else {
        screen.to_string()
    }
}

fn is_rule(line: &str) -> bool {
    let rest = line.trim_start();
    let rest = rest.strip_prefix(['╭', '╰']).unwrap_or(rest);
    rest.chars().take_while(|&c| c == '─').count() >= 10
}

fn is_prompt_line(line: &str) -> bool {
    let rest = line.trim_start();
    let rest = rest.strip_prefix('│').unwrap_or(rest).trim_start();
    rest.starts_with(['❯', '>'])
}

/// The part of `screen` under its last prompt box, or `None` with no box. Claude Code paints a
/// dialog in the box's place, so a marker above a visible box is transcript text quoting one.
pub fn under_prompt_box(screen: &str) -> Option<String> {
    let lines: Vec<&str> = screen.split('\n').collect();
    if lines.len() < 2 {
        return None;
    }
    for top in (0..lines.len() - 1).rev() {
        if !(is_rule(lines[top]) && is_prompt_line(lines[top + 1])) {
            continue;
        }
        let end = lines.len().min(top + 2 + PROMPT_BOX_MAX_LINES);
        for bottom in top + 2..end {
            if is_rule(lines[bottom]) {
                return Some(lines[bottom + 1..].join("\n"));
            }
        }
    }
    None
}

/// The part of `screen` under its last prompt box, or all of it with no box.
pub fn below_prompt_box(screen: &str) -> String {
    under_prompt_box(screen).unwrap_or_else(|| screen.to_string())
}

/// The marker that shows a modal or permission prompt on `screen`, or `None`.
pub fn modal_on_screen(screen: &str) -> Option<&'static str> {
    let below = below_prompt_box(&current_screen(screen));
    let chars: Vec<char> = below.chars().collect();
    let tail: String = chars[chars.len().saturating_sub(SCREEN_TAIL_CHARS)..].iter().collect();
    let tail = tail.to_lowercase();
    MODAL_MARKERS.iter().copied().find(|m| tail.contains(m))
}

/// Python's `repr` of a short string, as the refusal texts quote the command.
fn py_repr(s: &str) -> String {
    if s.contains('\'') && !s.contains('"') {
        format!("\"{s}\"")
    } else {
        format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn refused(text: &str, confirm: bool) -> Value {
        refusal(text, confirm, false).unwrap().unwrap_or(Value::Null)
    }

    // --- tests/test_cch.py: the guards enforced above both lanes ---

    #[test]
    fn test_a_modal_command_is_refused() {
        // These trap pasted input and Enter; only a hand-typed Esc recovers.
        for cmd in ["/mcp", "/status", "/config", "/resume"] {
            let res = refused(cmd, false);
            assert!(res["refused"] == true && res["kind"] == "interactive", "{cmd}");
        }
    }

    #[test]
    fn test_a_destructive_command_needs_confirmation() {
        for cmd in ["/clear", "/quit", "/exit"] {
            assert_eq!(refused(cmd, false)["refused"], true, "{cmd}");
        }
    }

    #[test]
    fn test_a_destructive_command_goes_through_with_confirmation() {
        assert_eq!(refusal("/clear", true, false), Ok(None));
    }

    // --- tests/test_cch_backends.py ---

    // --- tests/test_cch_control.py ---

    const COMPOSER: &str = "╭────────────╮\n│ >          │\n╰────────────╯\n  ? for shortcuts";
    const ERASE_LINE: &str = "\x1b[2K\x1b[1A";

    fn switch_dialog(title: &str, offer: &str, cursor: usize) -> String {
        let options = [format!("1. Yes, switch to {offer}"), "2. No, go back".to_string()];
        let options: Vec<String> = options
            .iter()
            .enumerate()
            .map(|(i, o)| format!("{}{o}", if i == cursor { "❯ " } else { "  " }))
            .collect();
        format!(
            "{title}\nThis conversation is cached for the current setting. Switching means the next turn \
             re-reads it uncached.\n{}\nEnter to confirm · Esc to cancel",
            options.join("\n")
        )
    }

    fn effort_yes() -> String {
        switch_dialog("Change effort level?", "Low", 0)
    }

    /// Ink's way of painting a region: lines, with the cursor left on the last.
    fn inline_frame(text: &str) -> String {
        text.split('\n').collect::<Vec<_>>().join("\r\n")
    }

    /// Erases the `lines`-line region above the cursor, as Ink does before a repaint.
    fn inline_close(lines: usize) -> String {
        ERASE_LINE.repeat(lines - 1) + "\x1b[2K\x1b[G"
    }

    fn line_count(text: &str) -> usize {
        text.matches('\n').count() + 1
    }

    /// The stream of a session whose effort dialog was answered a moment ago.
    fn stale_stream() -> String {
        let dialog = effort_yes();
        inline_frame(COMPOSER)
            + "\r\n"
            + &inline_close(line_count(COMPOSER) + 1)
            + &inline_frame(&dialog)
            + &inline_close(line_count(&dialog))
            + "Set effort level to low (saved as your default for new sessions)\r\n"
            + &inline_frame(COMPOSER)
    }

    #[test]
    fn test_a_dialog_that_was_answered_is_not_a_modal() {
        let stream = stale_stream();
        assert!(stream.contains("Change effort level?"), "the dialog text is still in the stream");
        assert_eq!(modal_on_screen(&stream), None);
    }

    #[test]
    fn test_a_dialog_still_open_at_the_end_of_the_stream_is_a_modal() {
        let stream = inline_frame(COMPOSER)
            + "\r\n"
            + &inline_close(line_count(COMPOSER) + 1)
            + &inline_frame(&effort_yes());
        assert!(modal_on_screen(&stream).is_some());
        assert!(current_screen(&stream).contains("Change effort level?"));
    }

    #[test]
    fn test_allow_modal_does_not_lift_the_destructive_guard() {
        assert_eq!(refusal("/clear", false, true).unwrap().unwrap()["refused"], true);
    }

    fn rule() -> String {
        "─".repeat(60)
    }

    fn idle_box() -> String {
        let rule = rule();
        format!("{rule}\n❯ \n{rule}\n  ⏵⏵ bypass permissions on (shift+tab to cycle)")
    }

    const QUOTING_REPLY: &str = "● It is stopped on your question \"Do you want to paste the reply yourself, or \
                                 should I ask them?\"\n✻ Cooked for 1m 28s";

    #[test]
    fn test_a_quoted_dialog_above_the_prompt_box_is_not_a_modal() {
        assert_eq!(modal_on_screen(&format!("{QUOTING_REPLY}\n{}", idle_box())), None);
    }

    #[test]
    fn test_a_permission_prompt_in_place_of_the_box_is_a_modal() {
        let dialog = format!(
            "{}\n Bash command\n\n   ls\n\n Do you want to proceed?\n ❯ 1. Yes\n   2. No, and tell Claude what \
             to do differently (esc)",
            rule()
        );
        assert_eq!(modal_on_screen(&format!("{QUOTING_REPLY}\n{dialog}")), Some("do you want to"));
    }

}
