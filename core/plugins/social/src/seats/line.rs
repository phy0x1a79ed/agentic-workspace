//! What a seat's agent sees: the pushed line, the session name, the scope slug and its brief's name.

use crate::connectors::{ChatKind, Inbound};

pub const TEXT_MAX: usize = 1500;

/// Lowercase, hyphenated: "Hallam Hive" -> "hallam-hive".
pub fn slug(s: &str) -> String {
    let mut out = String::new();
    for c in s.trim().trim_start_matches(['#', '@']).chars() {
        if c.is_alphanumeric() {
            out.extend(c.to_lowercase());
        } else if !out.ends_with('-') {
            out.push('-');
        }
    }
    let out = out.trim_matches('-').to_string();
    if out.is_empty() { "chat".into() } else { out }
}

/// Where a chat sits, as the line and the names show it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Place {
    pub platform: String,
    pub server: String,
    pub kind: ChatKind,
    pub chat_name: String,
    /// For a DM: the person on the other side.
    pub person: String,
}

impl Place {
    pub fn of(m: &Inbound) -> Self {
        Self { platform: m.platform.clone(), server: m.server.clone(), kind: m.kind, chat_name: m.channel_name.clone(), person: if m.kind == ChatKind::Dm { m.sender_name.clone() } else { String::new() } }
    }

    /// `<server>` in a line: the platform for a DM, else the workspace or guild.
    pub fn server_label(&self) -> String {
        if self.kind == ChatKind::Dm || self.server.is_empty() { self.platform.clone() } else { self.server.clone() }
    }

    /// `<chat>` in a line: `DM`, `#name`, or a group's name.
    pub fn chat_label(&self) -> String {
        match self.kind {
            ChatKind::Dm => "DM".into(),
            ChatKind::Group => if self.chat_name.is_empty() { "group".into() } else { self.chat_name.clone() },
            ChatKind::Channel => format!("#{}", self.chat_name.trim_start_matches('#')),
        }
    }

    /// `seat:<app>/<server>/<chat>`, slugged. A DM is `seat:<app>/dm/<person>`.
    pub fn session_name(&self) -> String {
        match self.kind {
            ChatKind::Dm => format!("seat:{}/dm/{}", self.platform, slug(&self.person)),
            _ => format!("seat:{}/{}/{}", self.platform, slug(&self.server_label()), slug(&self.chat_name)),
        }
    }

    /// The `{slug}` of a rule's scope: the person for a DM, else server and chat.
    pub fn scope_slug(&self) -> String {
        match self.kind {
            ChatKind::Dm => slug(&self.person),
            _ => format!("{}-{}", slug(&self.server_label()), slug(&self.chat_name)),
        }
    }
}

/// `[<server>/<chat>/<sender>, <level>] ↳<thread> <text>`, on one line.
pub fn push_line(m: &Inbound, level: &str) -> String {
    let p = Place::of(m);
    let mut who = if m.sender_name.is_empty() { if m.sender_id.is_empty() { "?".to_string() } else { m.sender_id.clone() } } else { m.sender_name.clone() };
    if m.sender_bot {
        who.push_str(" (bot)");
    }
    let thread = if !m.thread_id.is_empty() && m.thread_id != m.message_id { format!(" \u{21b3}{}", m.thread_id) } else { String::new() };
    let mut text = m.text.lines().collect::<Vec<_>>().join(" \u{23ce} ");
    if text.chars().count() > TEXT_MAX {
        text = text.chars().take(TEXT_MAX).collect::<String>() + " \u{2026}(cut at 1500 characters, the whole text is in `social history` or `social fetch`)";
    }
    if !m.attachments.is_empty() {
        let n = m.attachments.len();
        text = format!("{text} [+{n} file{}]", if n > 1 { "s" } else { "" }).trim_start().to_string();
    }
    format!("[{}/{}/{who}, {level}]{thread} {text}", p.server_label(), p.chat_label())
}

/// The seat's brief, relative to the seat docs.
pub fn brief_file(p: &Place, level: &str) -> String {
    if p.kind == ChatKind::Dm && level == "trusted" {
        format!("briefs/{}-dm.md", p.platform)
    } else {
        format!("briefs/{}-{}.md", p.platform, slug(&p.server_label()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::connectors::Attachment;

    fn msg(kind: ChatKind) -> Inbound {
        Inbound { platform: "slack".into(), server: "Test Lab".into(), kind, channel_name: "general".into(), sender_name: "tester".into(), message_id: "1.0".into(), text: "hi\nthere".into(), ..Default::default() }
    }

    #[test]
    fn the_line_names_the_place_the_sender_and_the_level() {
        assert_eq!(push_line(&msg(ChatKind::Dm), "cooperative"), "[slack/DM/tester, cooperative] hi \u{23ce} there");
        let mut m = msg(ChatKind::Channel);
        m.thread_id = "0.5".into();
        m.sender_bot = true;
        m.attachments = vec![Attachment::default(); 2];
        assert_eq!(push_line(&m, "untrusted"), "[Test Lab/#general/tester (bot), untrusted] \u{21b3}0.5 hi \u{23ce} there [+2 files]");
        m.thread_id = "1.0".into();
        assert!(!push_line(&m, "x").contains('\u{21b3}'), "a thread's first message is not marked");
        m.text = "y".repeat(TEXT_MAX + 10);
        let cut = push_line(&m, "x");
        assert!(cut.contains("cut at 1500") && cut.split(" \u{2026}").next().unwrap().matches('y').count() == TEXT_MAX);
    }

}
