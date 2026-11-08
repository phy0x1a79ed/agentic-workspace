//! What a holder sees: one capped line per message, and a transcript to grep.

use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

/// The most characters of a message a pushed line carries (channels.md R110 item 6).
pub const TEXT_MAX: usize = 1500;

/// Python's `str.splitlines`: every line boundary it knows, and no empty last line.
fn split_lines(text: &str) -> Vec<&str> {
    const BREAKS: [char; 10] = ['\n', '\r', '\u{0b}', '\u{0c}', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}', '\u{2028}', '\u{2029}'];
    let mut out = vec![];
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        if BREAKS.contains(&c) {
            out.push(&text[start..i]);
            start = i + c.len_utf8();
            if c == '\r' && chars.peek().map(|(_, n)| *n) == Some('\n') {
                chars.next();
                start += 1;
            }
        }
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

/// `<kind> <name> [<trust>] from <who>: <text>` on one line. Text past [`TEXT_MAX`] is cut,
/// and the line names the transcript and the message number that hold all of it.
pub fn push_line(kind: &str, name: &str, trust: &str, who: &str, text: &str, seq: i64, transcript: &Path) -> String {
    let mut flat = split_lines(text).join(" ⏎ ");
    if flat.chars().count() > TEXT_MAX {
        flat = flat.chars().take(TEXT_MAX).collect::<String>()
            + &format!(" …(cut at {TEXT_MAX} characters, message {seq} in full: {})", transcript.display());
    }
    format!("{kind} {name} [{trust}] from {who}: {flat}")
}

/// A line from the channel itself, not from the other side.
pub fn notice(kind: &str, name: &str, text: &str) -> String {
    format!("{kind} {name}: {text}")
}

/// One plain-text file per channel. Each message starts with a header line,
/// `--- <seq> <by> <who> <time>`, then its text exactly as it came.
pub struct Transcripts {
    pub root: PathBuf,
}

impl Transcripts {
    pub fn path(&self, name: &str) -> PathBuf {
        self.root.join(format!("{name}.txt"))
    }

    pub fn append(&self, name: &str, seq: i64, by: &str, who: &str, text: &str) -> PathBuf {
        let path = self.path(name);
        let write = || -> std::io::Result<()> {
            std::fs::create_dir_all(&self.root)?;
            std::fs::set_permissions(&self.root, std::fs::Permissions::from_mode(0o700))?;
            let stamp = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ");
            let mut f = std::fs::OpenOptions::new().create(true).append(true).open(&path)?;
            f.write_all(format!("--- {seq} {by} {who} {stamp}\n{text}\n").as_bytes())
        };
        if let Err(e) = write() {
            tracing::warn!("channel {name}: could not write its transcript: {e}");
        }
        path
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_long_message_is_cut_and_names_its_transcript() {
        let path = Path::new("/state/transcripts/acre-anchor-kettle.txt");
        let text = "é".repeat(TEXT_MAX + 10);
        let line = push_line("room", "acre-anchor-kettle", "untrusted", "visitor", &text, 7, path);
        assert!(line.starts_with("room acre-anchor-kettle [untrusted] from visitor: "));
        assert!(line.ends_with(&format!(" …(cut at 1500 characters, message 7 in full: {})", path.display())));
        assert_eq!(line.matches('é').count(), TEXT_MAX);
        let short = push_line("room", "r", "untrusted", "v", "hi", 1, path);
        assert_eq!(short, "room r [untrusted] from v: hi");
    }

}
