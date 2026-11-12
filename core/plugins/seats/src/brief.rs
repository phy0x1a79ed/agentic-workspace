//! The first prompt of a seat's agent.

use crate::store::Decl;

/// Everything the brief names besides the queued lines.
pub struct Facts<'a> {
    pub docs: &'a str,
    pub decl: &'a Decl,
    pub project: &'a str,
    /// `(session id, node)` of the agent before, for a start that does not resume it.
    pub previous: Option<(&'a str, &'a str)>,
    pub missed: i64,
}

pub fn brief(f: &Facts, lines: &[String]) -> String {
    let d = f.decl;
    let mut out = format!(
        "You are the agent of one chat seat: {}/{} on {}, at level {}. Your scope is {}/{}.\n\
         Read {}/seat-sop.md, then {}/{}.\n\
         Each new message in the chat is typed into your prompt as one line, \"[<server>/<chat>/<sender>, <level>] \u{21b3}<thread> <text>\". \
         Answer each one with `seats send text=<text>`: with no channel it answers the latest line, in its thread, and a reply waits here if the link to the chat is down. \
         After each result or decision you send, post a journal in your scope tagged `journal`, `seat` and `user:{}`. \
         You are a persistent session: keep your session name, and never end the session. \
         Between messages, wait. The seat stops you after 4 idle hours and resumes this conversation on the next message.\n",
        d.server_label, d.chat_label, d.platform, d.level, f.project, d.scope, f.docs, f.docs, d.brief, d.slug,
    );
    if let Some((session, node)) = f.previous {
        out.push_str(&format!("The agent before you was session {session} on {node}. Read the journals of your scope.\n"));
    }
    out.push_str(&missed(f.missed));
    out.push_str("The messages waiting for you, oldest first:\n");
    for l in lines {
        out.push_str(l);
        out.push('\n');
    }
    out
}

/// The line that says how many messages came while the seats were paused, or nothing.
pub fn missed(n: i64) -> String {
    match n {
        0 => String::new(),
        1 => "1 message came while the seats were paused and got no answer. `social history` shows it.\n".into(),
        n => format!("{n} messages came while the seats were paused and got no answer. `social history` shows them.\n"),
    }
}
