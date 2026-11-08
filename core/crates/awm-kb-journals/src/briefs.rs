//! The scope brief: what a scope is for, one in force per scope, on the scope's channel.
//!
//! A brief is a post tagged `brief`. A write appends a new post that replies to the one it
//! replaces, which gets `superseded`. The thread keeps every version; the brief in force is
//! the newest one not superseded.

use serde_json::{json, Value};

use crate::channel::NewPost;
use crate::posts::{and, author_display, eq, has_tag, Post};
use crate::schema::{BRIEF, SUPERSEDED};
use crate::{Fail, Journals, Result};

pub const MAX_CHARS: usize = 400;
pub const MAX_SENTENCES: usize = 2;

#[derive(Debug, Clone, PartialEq)]
pub struct Brief {
    pub id: String,
    pub project: String,
    pub scope: String,
    pub author: String,
    pub text: String,
    pub supersedes: String,
    pub ts: String,
}

impl Brief {
    fn of(p: &Post) -> Brief {
        Brief {
            id: p.id.clone(),
            project: p.project.clone(),
            scope: p.scope.clone(),
            author: author_display(&p.author),
            text: p.body.clone(),
            supersedes: p.reply_to.clone().unwrap_or_default(),
            ts: p.ts(),
        }
    }

    /// The line a session gets in its context.
    pub fn line(&self) -> String {
        format!("The brief of {}/{}: {}", self.project, self.scope, self.text)
    }

    pub fn to_dict(&self) -> Value {
        json!({"id": self.id, "project": self.project, "scope": self.scope, "author": self.author,
               "text": self.text, "line": self.line(), "supersedes": self.supersedes, "ts": self.ts})
    }
}

/// `len(re.split(r"(?<=[.!?])\s+(?=[A-Z\"'`(\[])", text))`: a sentence ends at a stop
/// followed by space and a capital, so a path or a version never splits one.
pub fn sentences(text: &str) -> usize {
    let chars: Vec<char> = text.chars().collect();
    let mut n = 1;
    let mut i = 1;
    while i < chars.len() {
        if matches!(chars[i - 1], '.' | '!' | '?') && chars[i].is_whitespace() {
            let mut j = i;
            while j < chars.len() && awm_pycompat::chars::is_space(chars[j]) {
                j += 1;
            }
            if j < chars.len() && (chars[j].is_ascii_uppercase() || "\"'`([".contains(chars[j])) {
                n += 1;
                i = j;
                continue;
            }
        }
        i += 1;
    }
    n
}

fn checked(text: &str) -> Result<String> {
    let text = awm_pycompat::pystr::strip(text).to_string();
    if text.is_empty() {
        return Err(Fail::brief("a brief needs text"));
    }
    let len = text.chars().count();
    if len > MAX_CHARS {
        return Err(Fail::brief(format!(
            "the brief is {len} characters; the limit is {MAX_CHARS}. Say only what the scope is for, in at most two sentences."
        )));
    }
    let n = sentences(&text);
    if n > MAX_SENTENCES {
        return Err(Fail::brief(format!("the brief has {n} sentences; the limit is {MAX_SENTENCES}. Say only what the scope is for.")));
    }
    Ok(text)
}

impl Journals {
    pub async fn brief_read(&self, project: &str, scope: &str) -> Result<Option<Brief>> {
        let posts = self.scan(&and(&[eq("owner_project", project), eq("owner_scope", scope), has_tag(BRIEF)])).await?;
        Ok(posts.iter().filter(|p| !p.has(SUPERSEDED)).max_by_key(|p| (p.ms, p.seq)).map(Brief::of))
    }

    /// Every version of the scope's brief, oldest first, by walking the reply links up.
    pub async fn brief_history(&self, project: &str, scope: &str) -> Result<Vec<Brief>> {
        let mut out: Vec<Brief> = Vec::new();
        let mut brief = self.brief_read(project, scope).await?;
        while let Some(b) = brief {
            if out.iter().any(|x| x.id == b.id) {
                break;
            }
            let parent = if b.supersedes.is_empty() { None } else { self.get_post(&b.supersedes).await? };
            out.push(b);
            brief = parent.filter(|p| p.has(BRIEF)).map(|p| Brief::of(&p));
        }
        out.reverse();
        Ok(out)
    }

    pub async fn brief_set(&self, project: &str, scope: &str, text: &str, author: &str, origin: Option<&str>) -> Result<Brief> {
        let text = checked(text)?;
        let _w = self.write.lock().await;
        let prior = self.brief_read(project, scope).await?;
        if let Some(p) = prior.as_ref().filter(|p| p.text == text) {
            return Ok(p.clone());
        }
        let post = self
            .post_locked(NewPost {
                project: project.into(),
                scope: scope.into(),
                author: author.into(),
                body: text,
                extra_tags: vec![BRIEF.into()],
                reply_to: prior.iter().map(|p| p.id.clone()).collect(),
                origin: origin.map(str::to_string),
                ..NewPost::default()
            })
            .await?;
        if let Some(p) = &prior {
            self.mark(&p.id, SUPERSEDED).await?;
        }
        Ok(Brief::of(&post))
    }

    /// Replace the one passage `old` of the brief in force with `new`.
    pub async fn brief_edit(&self, project: &str, scope: &str, old: &str, new: &str, author: &str, origin: Option<&str>) -> Result<Brief> {
        let prior = self
            .brief_read(project, scope)
            .await?
            .ok_or_else(|| Fail::brief(format!("{project}/{scope} has no brief to edit; use brief_set")))?;
        if old.is_empty() {
            return Err(Fail::brief("old must name the passage to replace"));
        }
        let count = prior.text.matches(old).count();
        if count != 1 {
            return Err(Fail::brief(format!("old occurs {count} times in the brief; it must occur exactly once")));
        }
        self.brief_set(project, scope, &prior.text.replacen(old, new, 1), author, origin).await
    }
}
