//! Recognising Claude Code processes on the process table.

use std::collections::BTreeSet;

use awm_plugin_sdk::procfs;

const NODE_LIKE: [&str; 3] = ["node", "nodejs", "bun"];
const CLAUDE_DIRS: [&str; 2] = ["claude", "claude-code"];

fn claude_path(path: &str) -> bool {
    let parts: Vec<&str> = path.split('/').filter(|p| !p.is_empty()).collect();
    let Some(last) = parts.last() else { return false };
    (last.starts_with("claude") && *last != "claudedaemon") || parts.iter().any(|p| CLAUDE_DIRS.contains(p))
}

/// Whether a command line or an executable path is Claude Code's.
///
/// Judged on `argv[0]` (and `argv[1]` behind a JavaScript runtime) and on the executable, never on
/// the arguments: a path argument such as `~/.claude` says nothing about the process.
pub fn looks_like_claude<S: AsRef<str>>(argv: &[S], exe: Option<&str>) -> bool {
    if exe.is_some_and(claude_path) {
        return true;
    }
    let Some(first) = argv.first().map(AsRef::as_ref) else { return false };
    if claude_path(first) {
        return true;
    }
    let runtime = first.rsplit('/').next().unwrap_or(first);
    NODE_LIKE.contains(&runtime) && argv.get(1).is_some_and(|a| claude_path(a.as_ref()))
}

fn judge(argv: Option<Vec<String>>, exe: Option<String>) -> bool {
    let argv = argv.unwrap_or_default();
    if argv.is_empty() && exe.is_none() {
        return true;
    }
    looks_like_claude(&argv, exe.as_deref())
}

/// Whether process `pid` could be a Claude Code REPL.
///
/// The command line and the executable are read separately and either one that answers decides,
/// because the executable link is refused for another user's and non-dumpable processes (sshd,
/// sudo, login) while the command line is not. Only when neither can be read is the process
/// assumed to be a session.
pub fn may_be_a_session(pid: u32) -> bool {
    judge(procfs::cmdline(pid), procfs::exe(pid))
}

/// The job short ids a terminal is attached to, read off the process table.
///
/// No file records an attach: the daemon multiplexes every attach over its one control socket.
/// What is left is the attaching process, `claude attach <short>`, whose argv names the job for as
/// long as the terminal is open. A terminal that arrived another way is invisible here, and an
/// unreadable `/proc` makes every job look unattached, so this is never the only guard before a
/// deletion.
pub fn attached_shorts() -> BTreeSet<String> {
    procfs::all_cmdlines()
        .into_iter()
        .filter_map(|(_, argv)| {
            let at = argv.iter().position(|a| a == "attach")?;
            argv.get(at + 1).filter(|s| !s.is_empty()).cloned()
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    const POSITIVE: [(&[&str], Option<&str>); 8] = [
        (&["claude", "bg-spare"], None),
        (&["/home/user/.local/bin/claude", "--bg"], Some("/usr/bin/other")),
        (&["/home/user/.local/share/claude/versions/2.1.296"], None),
        (&["/home/user/.local/share/claude/versions/2.1.296"], Some("/usr/bin/x")),
        (&["something"], Some("/home/user/.local/share/claude/versions/2.1.295")),
        (&["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"], Some("/usr/bin/node")),
        (&["bun", "/x/claude-code/cli.js"], None),
        (&["/usr/bin/node", "/opt/claude/cli.js", "--bg"], Some("/usr/bin/node")),
    ];

    const NEGATIVE: [(&[&str], Option<&str>); 8] = [
        (&["bash"], Some("/usr/bin/bash")),
        (&["sshd: user@pts/2"], None),
        (&["tmux: server"], Some("/usr/bin/tmux")),
        (&["-bash"], Some("/usr/bin/bash")),
        (&["python", "/home/user/x/awm/claudedaemon/run.py", "--dir", "/home/user/.claude"], Some("/usr/bin/python3")),
        (&["python", "-m", "pytest", "/home/user/claudedaemon/tests"], Some("/envs/awm/bin/python")),
        (&["node", "/srv/app/server.js", "/home/user/.claude/x"], Some("/usr/bin/node")),
        (&["less", "/home/user/.claude/CLAUDE.md"], Some("/usr/bin/less")),
    ];

    fn owned(argv: &[&str]) -> Option<Vec<String>> {
        Some(argv.iter().map(|s| s.to_string()).collect())
    }

    #[test]
    fn the_heuristic_recognises_claude_code() {
        for (argv, exe) in POSITIVE {
            assert!(looks_like_claude(argv, exe), "{argv:?} {exe:?}");
        }
    }

    #[test]
    fn the_heuristic_ignores_other_processes_and_claude_looking_arguments() {
        for (argv, exe) in NEGATIVE {
            assert!(!looks_like_claude(argv, exe), "{argv:?} {exe:?}");
        }
    }

    #[test]
    fn an_unreadable_exe_does_not_hide_a_readable_command_line() {
        assert!(!judge(owned(&["sshd: user@pts/2"]), None));
        assert!(judge(owned(&["claude", "--bg"]), None));
    }

    #[test]
    fn an_unreadable_command_line_does_not_hide_a_readable_exe() {
        assert!(!judge(None, Some("/usr/bin/bash".into())));
        assert!(judge(None, Some("/home/user/.local/share/claude/versions/2.1.296".into())));
    }

    #[test]
    fn a_process_with_neither_readable_is_assumed_to_be_a_session() {
        assert!(judge(None, None));
        assert!(may_be_a_session(1_000_000_000));
    }

    #[test]
    fn the_real_test_binary_is_not_a_session() {
        assert!(!may_be_a_session(std::process::id()));
    }

    #[test]
    fn the_attaching_argv_names_the_job() {
        assert!(!attached_shorts().contains("nobody-has-this-short"));
    }
}
