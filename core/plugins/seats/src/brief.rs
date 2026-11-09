//! The first prompt of a seat's agent.

use crate::store::Decl;

/// Everything the brief names besides the queued lines.
pub struct Facts<'a> {
    pub docs: &'a str,
    pub decl: &'a Decl,
    pub project: &'a str,
    /// `(job, node, transcript path)` of the agent before, if any.
    pub previous: Option<(&'a str, &'a str, Option<&'a str>)>,
}

pub fn brief(f: &Facts, lines: &[String]) -> String {
    let d = f.decl;
    let mut out = format!(
        "You are the agent of one chat seat: {}/{} on {}, at level {}. Your scope is {}/{}.\n\
         Read {}/seat-sop.md, then {}/{}.\n\
         Each new message in the chat is typed into your prompt as one line, \"[<server>/<chat>/<sender>, <level>] \u{21b3}<thread> <text>\". \
         Answer with `seats send text=<text>`: with no channel it answers the latest line, in its thread, and a reply waits here if the link to the chat is down. \
         Answer the message that started you, on that task only. When the task is done, write your handoff journal, then call `seats mark_done`.\n",
        d.server_label, d.chat_label, d.platform, d.level, f.project, d.scope, f.docs, f.docs, d.brief,
    );
    if let Some((job, node, transcript)) = f.previous {
        out.push_str(&format!("The agent before you was job {job} on {node}. Look for its handoff journal in this scope"));
        match transcript {
            Some(t) => out.push_str(&format!(", and read its transcript at {t} if the journal is not there yet.\n")),
            None => out.push_str(".\n"),
        }
    }
    out.push_str("The messages waiting for you, oldest first:\n");
    for l in lines {
        out.push_str(l);
        out.push('\n');
    }
    out
}
