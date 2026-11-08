//! Which kind of computer is on the owner's end. The operator declares it at `invite`,
//! because the two launchers run in different shells and a guess hands a Windows user a line
//! PowerShell cannot run. The owner's client reports its platform on connect, and a
//! disagreement is surfaced, never resolved silently.

use serde_json::{json, Value};

pub struct Choice {
    pub launcher: &'static str,
    pub shell: &'static str,
    pub open: &'static str,
}

pub fn choice(os: &str) -> Option<&'static Choice> {
    const WINDOWS: Choice = Choice {
        launcher: "powershell",
        shell: "PowerShell",
        open: "Open PowerShell (Start menu, type PowerShell), paste this line, press Enter",
    };
    const UNIX: Choice = Choice {
        launcher: "sh",
        shell: "POSIX sh",
        open: "Open a terminal (on WSL, the WSL terminal such as Ubuntu, not PowerShell; on a Mac, Terminal), paste this line, press Enter",
    };
    match os {
        "windows" => Some(&WINDOWS),
        "unix" => Some(&UNIX),
        _ => None,
    }
}

pub const ASK: &str = "Ask the owner explicitly: Windows PowerShell, WSL, Linux or Mac? Offer WSL as its own option, because a WSL user may otherwise answer Windows. Pass `os: \"windows\"` for Windows PowerShell and `os: \"unix\"` for WSL, Linux or Mac. Do not guess: the launcher for one does not run on the other.";

const UNIX_NAMES: [&str; 10] = ["wsl", "linux", "macos", "mac", "osx", "darwin", "posix", "sh", "bash", "zsh"];

/// Why `given` is not a usable declaration, or None when it is.
pub fn refusal(given: Option<&str>) -> Option<String> {
    let given = given.unwrap_or("");
    if given.is_empty() {
        return Some(format!("invite needs `os`. {ASK}"));
    }
    if choice(given).is_some() {
        return None;
    }
    if UNIX_NAMES.contains(&given.trim().to_lowercase().as_str()) {
        return Some(format!("`os` is `windows` or `unix`, not `{given}`. WSL, Linux and macOS all count as `unix`: send `os: \"unix\"`."));
    }
    Some(format!("`os` is `windows` or `unix`, not `{given}`. {ASK}"))
}

/// The one line the owner runs, for the shell they will run it in. The Windows line is built
/// from the daemon's own `curl -fsSL <url> | sh -s <code>`, so the two name one session.
pub fn launcher_line(os: &str, sh_line: &str) -> Result<String, String> {
    if choice(os).map(|c| c.launcher) == Some("sh") {
        return Ok(sh_line.to_string());
    }
    let rest = sh_line.strip_prefix("curl -fsSL ").ok_or_else(|| format!("cannot read the launcher URL from `{sh_line}`"))?;
    let (url, code) = rest.split_once(" | sh -s ").ok_or_else(|| format!("cannot read the launcher URL from `{sh_line}`"))?;
    if url.is_empty() || url.contains(char::is_whitespace) || code.is_empty() {
        return Err(format!("cannot read the launcher URL from `{sh_line}`"));
    }
    Ok(format!("& ([scriptblock]::Create((irm {url}/win))) {code}"))
}

/// What an agent needs to know about a session's machine. `reported` is the platform the
/// owner's client named on connect, Rust's `std::env::consts::OS`: WSL runs the Linux client,
/// so every report but `windows` is a unix machine.
pub fn describe(declared: Option<&str>, reported: Option<&str>) -> Value {
    let mut out = json!({"declared": declared});
    let reported = reported.filter(|r| !r.is_empty());
    if let Some(r) = reported {
        out["reported"] = json!(r);
    }
    let Some(c) = declared.and_then(choice) else {
        let tail = match reported {
            Some(r) => format!("The owner's client reports `{r}`; confirm with the owner before running anything."),
            None => ASK.to_string(),
        };
        out["warning"] = json!(format!("No OS was declared for this session. {tail}"));
        return out;
    };
    out["shell"] = json!(c.shell);
    if let (Some(r), Some(d)) = (reported, declared) {
        if (r == "windows") != (d == "windows") {
            out["mismatch"] = json!(true);
            out["warning"] = json!(format!(
                "Declared `{d}` but the owner's client reports `{r}`. The client is right about the machine it runs on: write commands for `{r}`, and ask the owner which computer this is before going on."
            ));
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refusals_name_the_two_choices() {
        assert!(refusal(None).unwrap().starts_with("invite needs `os`."));
        assert!(refusal(Some("wsl")).unwrap().contains("send `os: \"unix\"`"));
        assert!(refusal(Some("plan9")).unwrap().contains(ASK));
        assert_eq!(refusal(Some("windows")), None);
        assert_eq!(refusal(Some("unix")), None);
    }

    #[test]
    fn a_windows_line_is_built_from_the_sh_line() {
        let sh = "curl -fsSL https://relay.example/tether | sh -s 7 acre-anchor";
        assert_eq!(launcher_line("unix", sh).unwrap(), sh);
        assert_eq!(launcher_line("windows", sh).unwrap(), "& ([scriptblock]::Create((irm https://relay.example/tether/win))) 7 acre-anchor");
        assert!(launcher_line("windows", "something else").is_err());
    }

    #[test]
    fn a_mismatch_is_judged_as_windows_against_the_rest() {
        assert_eq!(describe(Some("unix"), Some("linux"))["shell"], "POSIX sh");
        assert!(describe(Some("unix"), Some("macos")).get("mismatch").is_none());
        assert_eq!(describe(Some("unix"), Some("windows"))["mismatch"], true);
        assert_eq!(describe(Some("windows"), Some("linux"))["mismatch"], true);
        assert!(describe(None, None)["warning"].as_str().unwrap().contains(ASK));
        assert!(describe(None, Some("linux"))["warning"].as_str().unwrap().contains("reports `linux`"));
    }
}
