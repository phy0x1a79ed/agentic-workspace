//! The `journal` tool: every verb with the Python's params, descriptions, surfaces and timeouts,
//! and the handlers that apply the caller rules before the journal acts.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use awm_index::{Mark, Veil};
use awm_plugin_sdk::{privacy, Call, Lane, Service};
use serde_json::{json, Value};

use crate::caller::{hides, hides_post, Exclusion, Who};
use crate::channel::{count, nest, reply_targets, NewPost, Query};
use crate::lineage::{fold_cites, stamp};
use crate::posts::Post;
use crate::schema::JOURNAL;
use crate::tags::parse_filter;
use crate::{int_arg, opt_str, py_repr, py_str, truthy, truthy_str, Fail, Journals, Result};

const TOOL: &str = "journal";

/// A param as describe lists it: the type only when it is not a string.
fn p(name: &str, ty: &str, required: bool, description: &str) -> Value {
    let mut v = json!({"name": name, "required": required});
    if ty != "string" {
        v["type"] = json!(ty);
    }
    if !description.is_empty() {
        v["description"] = json!(description);
    }
    v
}

const WRITE_GUIDE: &str = "The scope brief tells an agent what this scope is for within its project. It informs. It does not instruct: write no steps, commands or rules. Write at most two sentences, such as \"for <the work this scope holds>\". Name the scope's role: what its branch is for, the part of the project it works on, or whom it serves. When the work lives in a narrower part of the repo than the whole, give that path relative to the worktree. Do not describe the project, the plan or what done looks like. The repo's docs, AGENTS.md and the journal hold those. This is not a log. Change the brief only when the scope's role changes. A brief over 400 characters is refused.";

const POSTS_EMITTER: &str =
    "Fires once per new scope-channel post. Payload {project, scope, post}; post carries its tags. Subscribers filter by (project, scope).";

/// The edge-stamped peer identity, stored as an author or an editor.
fn edge_origin(who: &Who) -> Option<String> {
    who.peer().then(|| who.as_.clone()).flatten()
}

fn req_str(a: &Value, key: &str) -> String {
    a.get(key).map(py_str).unwrap_or_default()
}

/// The caller's veil for an answer of the search source.
pub fn veil_of(as_: Option<&str>) -> Veil {
    if !privacy::from_peer(as_) {
        return Veil::None;
    }
    match privacy::marks() {
        Err(_) => Veil::Unreadable,
        Ok(m) => Veil::Marks { marks: m.into_iter().map(|m| Mark { project: m.project, scope: m.scope }).collect(), tags: None },
    }
}

impl Journals {
    fn visible_fn<'a>(&'a self, who: &'a Who) -> Result<(impl Fn(&Post) -> bool + 'a, crate::posts::Names)> {
        let bound = self.bound(who)?;
        let names = self.tag_names()?;
        let n2 = names.clone();
        Ok((move |p: &Post| self.visible(&bound, who, p, &n2), names))
    }

    fn note_reads(&self, who: &Who, posts: &[Post]) {
        if let Err(e) = self.record_reads(who.session.as_deref(), posts) {
            tracing::info!("journal reads not recorded: {e}");
        }
    }

    pub async fn h_post(&self, a: &Value, who: &Who) -> Result<Value> {
        let tag_ids = self.post_tag_ids(a.get("tags"), &[])?;
        let stamped = stamp(a.get("meta").unwrap_or(&Value::Null), who.session.as_deref())?;
        let (meta, cited) = fold_cites(&stamped, &tag_ids)?;
        let mut targets = reply_targets(a.get("reply_to"))?;
        for c in cited {
            if !targets.contains(&c) {
                targets.push(c);
            }
        }
        let (visible, _) = self.visible_fn(who)?;
        let mut found = Vec::new();
        for t in &targets {
            match self.get_post(t).await? {
                Some(p) if visible(&p) => found.push(p),
                _ => return Err(Fail::value(format!("no post {t} to reply to"))),
            }
        }
        let (mut project, mut scope) = (opt_str(a, "project").unwrap_or_default(), opt_str(a, "scope").unwrap_or_default());
        if !targets.is_empty() && !tag_ids.iter().any(|t| t == JOURNAL) {
            let parent = &found[0];
            let root = match self.get_post(parent.root()).await? {
                Some(r) => r,
                None => parent.clone(),
            };
            project = root.project;
            scope = root.scope;
        }
        if project.is_empty() || scope.is_empty() {
            return Err(Fail::value("a post needs project and scope, or a reply_to to comment on"));
        }
        self.check_write(&self.bound(who)?, &project, &scope)?;
        if hides(who.as_(), &project, &scope) {
            return Err(Fail::value(format!("no scope {project}/{scope}")));
        }
        let post = self
            .post(NewPost {
                project,
                scope,
                author: req_str(a, "author"),
                body: req_str(a, "body"),
                tags: a.get("tags").cloned(),
                meta,
                reply_to: targets,
                origin: edge_origin(who),
                ..NewPost::default()
            })
            .await?;
        Ok(json!({"post": post.to_dict(&self.tag_names()?)}))
    }

    pub async fn h_fetch(&self, a: &Value, who: &Who) -> Result<Value> {
        self.refuse_clashed(who)?;
        let remote = who.peer();
        let (visible, names) = self.visible_fn(who)?;
        if let Some(post_id) = truthy_str(a, "post_id") {
            let p = self.get_post(&post_id).await?.filter(|p| visible(p));
            let Some(mut p) = p else { return Ok(json!({"posts": [], "total": 0})) };
            if !remote {
                self.note_reads(who, std::slice::from_ref(&p));
            }
            let thread = nest(&p, &self.thread_posts(p.root()).await?, &visible, &names);
            p.comments = Some(count(&thread));
            return Ok(json!({"posts": [p.to_dict(&names)], "total": 1, "thread": thread}));
        }
        let bound = self.bound(who)?;
        let tags = a.get("tags").filter(|v| !v.is_null()).cloned();
        let journal_name = self.tag(JOURNAL)?.map(|t| t.name).unwrap_or_default();
        let journal_first = parse_filter(tags.as_ref())? == vec![(false, journal_name)];
        let scope = opt_str(a, "scope");
        let project = self.channel_project(opt_str(a, "project"), scope.as_deref()).await?;
        let (project, scope) = self.narrow(&bound, project, scope)?;
        let query = truthy_str(a, "query");
        let order = truthy_str(a, "order").or_else(|| (journal_first && query.is_none()).then(|| "desc".to_string()));
        let q = Query {
            project,
            scope,
            query: query.clone(),
            author: opt_str(a, "author"),
            limit: int_arg(a, "limit", 50)?.max(0) as usize,
            offset: int_arg(a, "offset", 0)?.max(0) as usize,
            before_ts: opt_str(a, "before_ts"),
            order,
            exclude: Some(Exclusion::of(who.as_())),
            tags,
        };
        let (mut posts, degraded, semantic) = if query.is_some() {
            let (mut posts, degraded, semantic) = self.search(&q).await?;
            if remote {
                for p in &mut posts {
                    let thread = nest(p, &self.thread_posts(&p.id).await?, &visible, &names);
                    p.comments = Some(count(&thread));
                }
            }
            (posts, degraded, Some(semantic))
        } else {
            (self.fetch(&q).await?, None, None)
        };
        if !remote {
            self.note_reads(who, &posts);
        }
        let shown: Vec<Value> = posts.iter_mut().map(|p| p.to_dict(&names)).collect();
        let mut out = json!({"total": shown.len(), "posts": shown});
        let mut ordered = serde_json::Map::new();
        ordered.insert("posts".into(), out["posts"].take());
        ordered.insert("total".into(), out["total"].take());
        if let Some(s) = semantic {
            ordered.insert("semantic".into(), json!(s));
        }
        if let Some(d) = degraded {
            ordered.insert("degraded".into(), d);
        }
        Ok(Value::Object(ordered))
    }

    pub async fn h_note_read(&self, a: &Value, who: &Who) -> Result<Value> {
        let post = self.get_post(&req_str(a, "post_id")).await?;
        let (visible, _) = self.visible_fn(who)?;
        let Some(post) = post.filter(|p| visible(p)) else { return Ok(json!({"ok": true, "recorded": 0})) };
        let session = match who.lane {
            Lane::Service => truthy_str(a, "session").and_then(|s| awm_plugin_sdk::ids::to_r9(Some(&s))).or(who.session.clone()),
            _ => who.session.clone(),
        };
        Ok(json!({"ok": true, "recorded": self.record_reads(session.as_deref(), &[post])?}))
    }

    pub async fn h_reads(&self, a: &Value, who: &Who) -> Result<Value> {
        if who.peer() {
            return Err(Fail::permission("a session's reads stay on its own node"));
        }
        let named = a.get("session").filter(|v| truthy(v));
        let session = match named {
            Some(n) => n.as_str().and_then(|s| awm_plugin_sdk::ids::to_r9(Some(s))),
            None => who.session.clone(),
        };
        let Some(session) = session else {
            return Err(Fail::value(match named {
                Some(n) => format!("{} is not a session id", py_repr(n)),
                None => "no calling session; pass session".into(),
            }));
        };
        let rows = self.reads(&session, int_arg(a, "limit", 200)?).await?;
        Ok(json!({"session": session, "total": rows.len(), "reads": rows, "cites": self.cites_of(&session).await?}))
            .map(|v: Value| {
                let mut m = serde_json::Map::new();
                for k in ["session", "reads", "total", "cites"] {
                    m.insert(k.into(), v[k].clone());
                }
                Value::Object(m)
            })
    }

    pub async fn h_inbox(&self, a: &Value, who: &Who) -> Result<Value> {
        let (project, scope) = (req_str(a, "project"), req_str(a, "scope"));
        self.check_read(&self.bound(who)?, &project, &scope)?;
        if who.peer() || hides(who.as_(), &project, &scope) {
            return Err(Fail::value(format!("no scope {project}/{scope}")));
        }
        self.require_scope(&project, &scope).await?;
        let limit = match a.get("limit").filter(|v| truthy(v)) {
            Some(_) => int_arg(a, "limit", crate::inbox::DEFAULT_LIMIT)?,
            None => crate::inbox::DEFAULT_LIMIT,
        };
        let all = matches!(a.get("all"), Some(Value::Bool(true))) || matches!(a.get("all"), Some(Value::String(s)) if s == "true" || s == "True" || s == "1")
            || a.get("all").and_then(Value::as_i64) == Some(1);
        self.digest(&project, &scope, a.get("tags"), limit, all, &Exclusion::of(who.as_())).await
    }

    pub async fn h_ack(&self, a: &Value, who: &Who) -> Result<Value> {
        let (project, scope) = (req_str(a, "project"), req_str(a, "scope"));
        self.check_write(&self.bound(who)?, &project, &scope)?;
        if who.peer() || hides(who.as_(), &project, &scope) {
            return Err(Fail::value(format!("no scope {project}/{scope}")));
        }
        self.require_scope(&project, &scope).await?;
        let by = who.session.clone().or(who.as_.clone());
        self.ack(&project, &scope, truthy_str(a, "through").as_deref(), by.as_deref()).await
    }

    async fn post_for_write(&self, a: &Value, who: &Who) -> Result<Post> {
        let id = req_str(a, "post_id");
        let p = self.get_post(&id).await?.filter(|p| !hides(who.as_(), &p.project, &p.scope));
        let p = p.ok_or_else(|| Fail::value(format!("no post {id}")))?;
        self.check_write(&self.bound(who)?, &p.project, &p.scope)?;
        Ok(p)
    }

    fn editor(who: &Who) -> String {
        edge_origin(who).or(who.scope.clone().filter(|s| !s.is_empty())).unwrap_or_else(|| "system".into())
    }

    pub async fn h_edit(&self, a: &Value, who: &Who) -> Result<Value> {
        let p = self.post_for_write(a, who).await?;
        let s = |k: &str| opt_str(a, k);
        self.edit(&p.id, &Self::editor(who), s("body").as_deref(), s("old").as_deref(), s("new").as_deref(), &truthy_str(a, "reason").unwrap_or_default()).await
    }

    pub async fn h_history(&self, a: &Value, who: &Who) -> Result<Value> {
        let id = req_str(a, "post_id");
        let p = self.get_post(&id).await?.filter(|p| !hides(who.as_(), &p.project, &p.scope));
        let p = p.ok_or_else(|| Fail::value(format!("no post {id}")))?;
        self.check_read(&self.bound(who)?, &p.project, &p.scope)?;
        self.history(&id).await
    }

    pub async fn h_addenda(&self, a: &Value, who: &Who) -> Result<Value> {
        let bound = self.bound(who)?;
        if bound.write_scope.is_some() || who.as_.is_some() {
            return Err(Fail::refused("only a trusted session on this node sweeps the addenda"));
        }
        self.sweep(truthy_str(a, "project").as_deref(), truthy_str(a, "scope").as_deref(), a.get("apply").is_some_and(truthy), &Self::editor(who)).await
    }

    fn trusted_only(&self, who: &Who) -> Result<()> {
        if self.bound(who)?.write_scope.is_some() {
            return Err(Fail::permission("only a trusted session edits the tag vocabulary"));
        }
        Ok(())
    }

    pub async fn h_tag_add(&self, a: &Value, who: &Who) -> Result<Value> {
        self.trusted_only(who)?;
        let desc = truthy_str(a, "description").unwrap_or_default();
        let tag = self.tag_add(a.get("name").unwrap_or(&Value::Null), &desc, who.as_().unwrap_or(""), None).await?;
        Ok(json!({"tag": tag.to_dict()}))
    }

    pub async fn h_tag_edit(&self, a: &Value, who: &Who) -> Result<Value> {
        self.trusted_only(who)?;
        let desc = opt_str(a, "description");
        let tag = self.tag_edit(&req_str(a, "tag"), a.get("name"), desc.as_deref(), who.as_().unwrap_or("")).await?;
        Ok(json!({"tag": tag.to_dict()}))
    }

    pub async fn h_tag_list(&self, a: &Value, who: &Who) -> Result<Value> {
        let hidden = if who.peer() { Some(self.hidden_tags().await?) } else { None };
        let hides_id = |id: &str| match &hidden {
            None => false,
            Some(None) => true,
            Some(Some(h)) => h.contains(id),
        };
        if let Some(t) = truthy_str(a, "tag") {
            if self.tag_ref(&t)?.is_some_and(|tag| hides_id(&tag.id)) {
                return Err(Fail::tag(format!("unknown tag {}.", awm_pycompat::repr::str_repr(&t))));
            }
            return Ok(json!({"tag": a["tag"], "versions": self.tag_versions(&t)?}));
        }
        let vocab: Vec<Value> = self.vocabulary()?.into_iter().filter(|t| !hides_id(&t.id)).map(|t| t.to_dict()).collect();
        let clashes: Vec<Value> = self
            .clashes(true)?
            .into_iter()
            .filter(|c| !hides_id(c["older_id"].as_str().unwrap_or("")) && !hides_id(c["newer_id"].as_str().unwrap_or("")))
            .collect();
        Ok(json!({"tags": vocab, "total": vocab.len(), "clashes": clashes}))
    }

    pub async fn h_retag(&self, a: &Value, who: &Who) -> Result<Value> {
        self.trusted_only(who)?;
        let id = req_str(a, "post_id");
        let names = self.tag_names()?;
        let p = self.get_post(&id).await?.filter(|p| !hides_post(who.as_(), p, &names));
        let p = p.ok_or_else(|| Fail::value(format!("no post {id}")))?;
        let after = self.set_tags(&p.id, a.get("add"), a.get("remove")).await?;
        Ok(json!({"post": after.to_dict(&self.tag_names()?)}))
    }

    fn brief_where(&self, a: &Value, who: &Who, write: bool) -> Result<(String, String)> {
        let (project, scope) = (req_str(a, "project"), req_str(a, "scope"));
        let bound = self.bound(who)?;
        if write {
            self.check_write(&bound, &project, &scope)?;
        } else {
            self.check_read(&bound, &project, &scope)?;
        }
        if hides(who.as_(), &project, &scope) {
            return Err(Fail::value(format!("no scope {project}/{scope}")));
        }
        Ok((project, scope))
    }

    fn brief_author(a: &Value, project: &str, scope: &str) -> String {
        truthy_str(a, "author").unwrap_or_else(|| format!("agent:{project}/{scope}"))
    }

    pub async fn h_brief_read(&self, a: &Value, who: &Who) -> Result<Value> {
        let (p, s) = self.brief_where(a, who, false)?;
        Ok(json!({"brief": self.brief_read(&p, &s).await?.map(|b| b.to_dict())}))
    }

    pub async fn h_brief_history(&self, a: &Value, who: &Who) -> Result<Value> {
        let (p, s) = self.brief_where(a, who, false)?;
        let found: Vec<Value> = self.brief_history(&p, &s).await?.iter().map(|b| b.to_dict()).collect();
        Ok(json!({"briefs": found, "total": found.len()}))
    }

    pub async fn h_brief_set(&self, a: &Value, who: &Who) -> Result<Value> {
        let (p, s) = self.brief_where(a, who, true)?;
        let b = self.brief_set(&p, &s, &req_str(a, "text"), &Self::brief_author(a, &p, &s), edge_origin(who).as_deref()).await?;
        Ok(json!({"brief": b.to_dict()}))
    }

    pub async fn h_brief_edit(&self, a: &Value, who: &Who) -> Result<Value> {
        let (p, s) = self.brief_where(a, who, true)?;
        let b = self
            .brief_edit(&p, &s, &req_str(a, "old"), &req_str(a, "new"), &Self::brief_author(a, &p, &s), edge_origin(who).as_deref())
            .await?;
        Ok(json!({"brief": b.to_dict()}))
    }

    pub async fn h_precedent_import(&self, a: &Value) -> Result<Value> {
        let path = truthy_str(a, "path").map(PathBuf::from).unwrap_or_else(|| self.precedence_path());
        self.import_precedents(&path, a.get("dry_run").is_some_and(truthy)).await
    }

    fn local(who: &Who) -> Result<()> {
        if who.peer() {
            return Err(Fail::permission("lineage stays on its own node"));
        }
        Ok(())
    }

    pub async fn h_lineage(&self, a: &Value, who: &Who) -> Result<Value> {
        Self::local(who)?;
        let session = truthy_str(a, "session").or(who.session.clone()).ok_or_else(|| Fail::value("no calling session; pass session"))?;
        self.walk(&session, crate::lineage::MAX_EDGES).await
    }

    pub async fn h_session_parent(&self, a: &Value, who: &Who) -> Result<Value> {
        Self::local(who)?;
        let how = truthy_str(a, "how").unwrap_or_else(|| "start".into());
        let made = self.set_parent(&req_str(a, "session_id"), &req_str(a, "parent"), &how, &truthy_str(a, "node").unwrap_or_default(), None)?;
        Ok(json!({"ok": true, "recorded": made}))
    }

    pub async fn h_lineage_import(&self, a: &Value, who: &Who) -> Result<Value> {
        Self::local(who)?;
        self.import_parents(std::path::Path::new(&req_str(a, "path")))
    }

    pub async fn h_journal_query(&self, a: &Value, who: &Who) -> Result<Value> {
        self.refuse_clashed(who)?;
        self.query_verb(a, &veil_of(who.as_())).await
    }

    pub async fn h_journal_get(&self, a: &Value, who: &Who) -> Result<Value> {
        self.refuse_clashed(who)?;
        let id = req_str(a, "id");
        match self.document(&id, &veil_of(who.as_()), who.session.as_deref()).await? {
            Some(d) => Ok(json!({"document": d, "veiled": true})),
            None => Err(Fail::value(format!("no document journals:{id}"))),
        }
    }

    /// Dispatch one verb by name, for tests and the CLI.
    pub async fn call_verb(&self, verb: &str, a: &Value, who: &Who) -> Result<Value> {
        match verb {
            "post" => self.h_post(a, who).await,
            "fetch" => self.h_fetch(a, who).await,
            "reads" => self.h_reads(a, who).await,
            "note_read" => self.h_note_read(a, who).await,
            "inbox" => self.h_inbox(a, who).await,
            "ack" => self.h_ack(a, who).await,
            "edit" => self.h_edit(a, who).await,
            "history" => self.h_history(a, who).await,
            "addenda" => self.h_addenda(a, who).await,
            "tag_add" => self.h_tag_add(a, who).await,
            "tag_edit" => self.h_tag_edit(a, who).await,
            "tag_list" => self.h_tag_list(a, who).await,
            "retag" => self.h_retag(a, who).await,
            "tag_receive" => self.receive_tags(a.get("tags").unwrap_or(&Value::Null), who.as_()).await,
            "brief_read" => self.h_brief_read(a, who).await,
            "brief_history" => self.h_brief_history(a, who).await,
            "brief_set" => self.h_brief_set(a, who).await,
            "brief_edit" => self.h_brief_edit(a, who).await,
            "render" => self.render(a.get("project").unwrap_or(&Value::Null), a.get("scope").unwrap_or(&Value::Null), who.as_()).await,
            "precedent_import" => self.h_precedent_import(a).await,
            "lineage" => self.h_lineage(a, who).await,
            "session_parent" => self.h_session_parent(a, who).await,
            "lineage_import" => self.h_lineage_import(a, who).await,
            "journal_query" => self.h_journal_query(a, who).await,
            "journal_get" => self.h_journal_get(a, who).await,
            other => Err(Fail::runtime(format!("unknown function {}", awm_pycompat::repr::str_repr(other)))),
        }
    }
}

/// The verbs in the order the Python manifest lists them, each with its description and params.
pub fn specs() -> Vec<(&'static str, String, Value)> {
    let where_ = || vec![p("project", "string", true, ""), p("scope", "string", true, "")];
    let author = p("author", "string", false, "Who writes it. Defaults to agent:<project>/<scope>.");
    vec![
        ("edit", "Correct a post in place. Give old and new to replace one passage, or body for the whole text. The journal keeps every earlier version with its diff; reads show the latest. Use this instead of posting an addendum.".into(),
         json!([p("post_id", "string", true, ""), p("reason", "string", false, ""),
                p("old", "string", false, "A passage that occurs exactly once in the post."), p("new", "string", false, ""),
                p("body", "string", false, "The whole new text. Pass this argument LAST.")])),
        ("history", "Every version of a post, oldest first, each with its diff from the one before.".into(), json!([p("post_id", "string", true, "")])),
        ("addenda", "List the posts that open as an addendum or correction to an earlier post of the same scope, with the post each corrects. With apply, fold each into its target as a new version and retire the addendum from reads.".into(),
         json!([p("project", "string", false, ""), p("scope", "string", false, ""), p("apply", "boolean", false, "")])),
        ("post", "Post to a scope's channel. tags come from the vocabulary (tag_list); a post with none is a message. tags=[journal] is the scope's own debrief; structured fields go in meta. reply_to=[<post id>] makes the post a comment: it lands in the channel of its thread's first post, and that post's author gets it in its inbox. On a journal, reply_to lists the journals it built on. Tag a post precedent when it records a decision Tony made or overrode that holds beyond the session: post it at project='' scope='workspace', with a body that holds the context, the question and the decision. Search precedents before you ask Tony a preference question. Pass all structured params (tags, meta, reply_to) BEFORE the free-text body, which must be the LAST argument \u{2014} see the body param note.".into(),
         json!([p("project", "string", false, "Required unless reply_to is given."), p("scope", "string", false, "Required unless reply_to is given."),
                p("author", "string", true, ""), p("tags", "array", false, "Tag names from the vocabulary. An unknown tag is refused."),
                p("reply_to", "array", false, "Post ids. A comment names the one post it answers. A debrief journal names every earlier journal it built on: its citations, picked from journal reads. A journal stays in its own scope."),
                p("meta", "object", false, "Structured fields. meta.session names the session a journal covers; omit it for your own session."),
                p("body", "string", true, "Free-text post content. Pass this argument LAST \u{2014} emit every other parameter before it.")])),
        ("fetch", "Fetch or search a scope's posts \u{2014} messages and journal entries. Pass project and scope for one channel (a literal scope needs its project: scope names repeat across projects); add query to rank posts by relevance (the search service's meaning and keyword match over each post's full text, best first, each hit with match.snippet \u{2014} a limit of ~10 is plenty; the reply's `semantic` says what ranked them: search, or none for a substring match); omit scope to search across scopes; a query returns each thread's first post with its comment count. tags filters with 'and' and '!' (e.g. tags='journal' for session logs, '!closed and complaint' for open complaints); post_id fetches one with its thread of comments. Without a query, tags='journal' reads newest-first (e.g. last 5 awm session logs: project='awm', tags='journal', limit=5).".into(),
         json!([p("project", "string", false, ""), p("scope", "string", false, ""), p("tags", "string", false, "A tag filter: names joined by 'and', '!' negates one."),
                p("query", "string", false, ""), p("author", "string", false, ""), p("post_id", "string", false, ""),
                p("limit", "integer", false, ""), p("offset", "integer", false, ""), p("before_ts", "string", false, ""),
                p("order", "string", false, "'asc' (oldest-first) or 'desc' (newest-first). Default: 'desc' for tags='journal', else 'asc' within one scope.")])),
        ("reads", "The journals a session read through fetch, newest first: the candidates to cite at debrief. Also lists the citations its own journals already make. session defaults to yours.".into(),
         json!([p("session", "string", false, ""), p("limit", "integer", false, "")])),
        ("note_read", "Record that the calling session read one journal post through another service, such as search get (sessions.md R15). For services, not agents.".into(),
         json!([p("post_id", "string", true, ""), p("session", "string", false, "The session that read it. Honoured only from another service, which reads on a session's behalf.")])),
        ("inbox", "What a scope has not read yet: one line per post, newest first, bounded. Each line has the post id, author, age, a gist (its title, leading heading or first sentence), whether it looks like it asks for action, and its length. It also lists comments on threads the scope started, wherever they live. Read one in full with fetch(post_id=\u{2026}). The read cursor belongs to the scope, so the next session there starts where this one acknowledged. Posts the scope wrote itself are never unread. Mark read with ack(through=<ack_through>).".into(),
         json!([p("project", "string", true, ""), p("scope", "string", true, ""), p("tags", "string", false, "A tag filter. Default 'message'."),
                p("limit", "integer", false, "Posts to list. Default 20, at most 50."), p("all", "boolean", false, "List read posts too, each marked read. Default false.")])),
        ("ack", "Mark a scope's posts read through one post: it and every post before it. Pass the ack_through id that inbox returned, so a post that arrived after you read the digest stays unread. Without through, marks everything in the channel read. The cursor never moves back.".into(),
         json!([p("project", "string", true, ""), p("scope", "string", true, ""), p("through", "string", false, "A post id of this channel. Default: its newest post.")])),
        ("tag_add", "Add a tag to the vocabulary that every node shares. Check tag_list first: add only a concept no tag covers. The description is required and says what a post with this tag is. The add reaches every node by itself.".into(),
         json!([p("name", "string", true, "Lowercase letters, digits and _ . : + / -, at most 64 characters."), p("description", "string", true, "")])),
        ("tag_edit", "Change a tag's name or description. Posts link a tag by its id, so a rename shows on every post. The earlier versions stay; tag_list with tag shows them.".into(),
         json!([p("tag", "string", true, "The tag's id or current name."), p("name", "string", false, ""), p("description", "string", false, "")])),
        ("tag_list", "The tag vocabulary: each tag with its id and description, and any open clash between two nodes that added one name. With tag, that tag's versions.".into(),
         json!([p("tag", "string", false, "A tag's id or name, to list its earlier versions.")])),
        ("retag", "Add and remove several tags on one post at once, such as add=[closed]. A type tag (message, journal, brief, precedent) is set only when the post is made; closed and superseded change freely.".into(),
         json!([p("post_id", "string", true, ""), p("add", "array", false, ""), p("remove", "array", false, "")])),
        ("tag_receive", "A peer node's pushed tags, merged into this node's vocabulary.".into(), json!([p("tags", "array", true, "")])),
        ("brief_read", "The scope's brief: what the scope is for. The harness hands it to a session when the session enters the scope, so read it only to look again. Returns brief null when the scope has none.".into(), json!(where_())),
        ("brief_history", "Every version of the scope's brief, oldest first; the last is in force.".into(), json!(where_())),
        ("brief_set", format!("{WRITE_GUIDE} brief_set replaces the whole brief."),
         json!([where_()[0].clone(), where_()[1].clone(), author.clone(), p("text", "string", true, "The whole brief. Pass this argument last.")])),
        ("brief_edit", format!("{WRITE_GUIDE} brief_edit replaces one passage, old, with new."),
         json!([where_()[0].clone(), where_()[1].clone(), author, p("old", "string", true, "The passage to replace. It must occur exactly once."),
                p("new", "string", true, "Its replacement. Pass this argument last.")])),
        ("render", "A scope's history from the journal, as markdown: its own latest journal entries first, then its project's. The startup ritual reads it.".into(), json!(where_())),
        ("journal_query", "Posts for search's live `journals` source and for other services, in search's sections: `exact` holds the posts that match the text, best first, and `related` those near it in meaning. query is search's text (a string, or {$about} and {$contains}); filter holds the journals fields (project, scope, ts, tag by name, replies_to) and $sort. A filter that names no tag ranks thread first posts tagged journal, message or precedent. Each hit carries its id, title, ts, project and scope. The marks apply for the caller, and the answer says veiled. search query calls it; call that instead.".into(),
         json!([json!({"name": "query", "type": ["string", "object"], "required": false, "description": "The text to rank by: a string, or one of {$about}, {$contains}, {$and}, {$or}. Without it, posts are listed by their fields."}),
                p("filter", "object", false, "The journals fields to filter by, as search's per-source filter."),
                p("max", "integer", false, "Maximum hits per section. Default 10, at most 100.")])),
        ("journal_get", "One post in full, for `search get` on the `journals` source: its title, its thread's text, date, project, scope, tags and citations. The marks apply for the caller.".into(),
         json!([p("id", "string", true, "The post id.")])),
        ("precedent_import", "Import precedence.db once: each decision becomes a workspace post tagged precedent, its notes comments, its supersedes replies. A rerun adds nothing. dry_run reports without writing.".into(),
         json!([p("path", "string", false, "The precedence.db to read. Default: this node's."), p("dry_run", "boolean", false, "")])),
        ("lineage", "A session's lineage, walked forward only: the session that started it (or that it was before a clear), that session's parent and so on, and the journals its debrief cites, then what those sessions came from. session defaults to yours.".into(),
         json!([p("session", "string", false, "")])),
        ("session_parent", "Record who started a session, or which session it was before a clear. For reflection and the harness, not agents. The first link wins.".into(),
         json!([p("session_id", "string", true, ""), p("parent", "string", true, ""), p("how", "string", false, "start or clear. Default start."), p("node", "string", false, "")])),
        ("lineage_import", "Copy the parent links reporting kept (its session_parents table) once.".into(),
         json!([p("path", "string", true, "The reporting work database to read.")])),
    ]
}

/// Surfaces and timeouts that differ from the default: every surface, the gateway's timeout.
fn options(verb: &str) -> (Option<&'static [&'static str]>, Option<u64>) {
    const HIDDEN: &[&str] = &["cli", "http"];
    match verb {
        "fetch" => (None, Some(60)),
        "note_read" | "session_parent" | "lineage_import" => (Some(HIDDEN), None),
        "precedent_import" => (Some(HIDDEN), Some(600)),
        "journal_query" | "journal_get" => (Some(HIDDEN), Some(30)),
        "tag_receive" => (Some(&["http"]), None),
        _ => (None, None),
    }
}

/// Add every verb of the `journal` tool, and its `posts` emitter, to `svc`.
pub fn register(svc: Service, j: Arc<Journals>) -> Service {
    let mut svc = svc.emitter("posts", Some(POSTS_EMITTER));
    for (name, desc, params) in specs() {
        let jj = j.clone();
        svc = svc.verb(TOOL, name, &desc, params, move |call: Call| {
            let j = jj.clone();
            async move {
                let who = Who::from_call(&call);
                j.call_verb(name, &call.args, &who).await.map_err(Fail::into_verb)
            }
        });
        let (surfaces, timeout) = options(name);
        if let Some(s) = surfaces {
            svc = svc.surfaces(s.iter().copied());
        }
        if let Some(t) = timeout {
            svc = svc.timeout(Duration::from_secs(t));
        }
    }
    svc
}
