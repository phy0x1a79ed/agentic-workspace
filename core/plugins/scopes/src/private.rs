//! Set and clear this node's private marks on projects and scopes. scopes is the marks
//! file's only writer, through the SDK's `privacy::write_marks`. It checks each target
//! against the projects it knows and refuses a mark whose transcript slug would also cover
//! another project or scope.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use awm_plugin_sdk::privacy;
use serde_json::{json, Value};

use crate::{is_relative_to, path_str, Fail, Op};

static WRITE: Mutex<()> = Mutex::new(());

fn unreadable(e: privacy::MarksUnreadable) -> Fail {
    Fail::new("MarksUnreadable", e.0)
}

impl Op {
    /// Every project with a row or a bare repo.
    pub fn known_projects(&self) -> Result<BTreeSet<String>, Fail> {
        let mut names: BTreeSet<String> = self.read(|c| {
            let mut stmt = c.prepare("SELECT name FROM projects")?;
            let it = stmt.query_map([], |r| r.get(0))?;
            Ok(it.collect::<Result<_, _>>()?)
        })?;
        names.extend(self.bare_projects());
        Ok(names)
    }

    fn scope_worktree(&self, project: &str, scope: &str) -> Result<PathBuf, Fail> {
        if let Some(rec) = self.agent_record(project, scope, false)? {
            return Ok(self.resolve_worktree(project, scope, Some(&rec.worktree)));
        }
        if let Some(main) = self.main_checkouts(Some(project))?.remove(project) {
            if main.scope == scope {
                return Ok(PathBuf::from(main.worktree));
            }
        }
        Err(Fail::not_found(format!("Scope '{project}/{scope}' not found")))
    }

    fn target_path(&self, project: &str, scope: Option<&str>) -> Result<PathBuf, Fail> {
        if !self.known_projects()?.contains(project) {
            return Err(Fail::not_found(format!("Project '{project}' not found")));
        }
        match scope {
            None => Ok(self.paths.projects.join(project)),
            Some(s) => self.scope_worktree(project, s),
        }
    }

    /// Every registered project directory and scope worktree but `project`'s own directory.
    fn other_dirs(&self, project: &str) -> Result<Vec<PathBuf>, Fail> {
        let mut dirs: Vec<PathBuf> = self.known_projects()?.into_iter().filter(|n| n != project).map(|n| self.paths.projects.join(n)).collect();
        let rows: Vec<(String, String, String)> = self.read(|c| {
            let mut stmt = c.prepare("SELECT p.name AS project, a.scope, a.worktree FROM agents a JOIN projects p ON p.id = a.project_id")?;
            let it = stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?;
            Ok(it.collect::<Result<_, _>>()?)
        })?;
        dirs.extend(rows.iter().map(|(p, s, w)| self.resolve_worktree(p, s, Some(w))));
        Ok(dirs)
    }

    /// Directories outside `path` whose transcript slug a mark at `path` would also cover.
    pub fn slug_collisions(&self, project: &str, path: &Path) -> Result<Vec<String>, Fail> {
        let prefix = privacy::slug(&path_str(path));
        let mut clashes = BTreeSet::new();
        for other in self.other_dirs(project)? {
            if other == path || is_relative_to(&other, path) {
                continue;
            }
            if privacy::slug_covers(&prefix, &privacy::slug(&path_str(&other))) {
                clashes.insert(path_str(&other));
            }
        }
        Ok(clashes.into_iter().collect())
    }

    /// Mark or unmark a project, or one scope of it, private. Returns the project's marks.
    pub fn set_private(&self, project: &str, scope: Option<&str>, private: bool) -> Result<Value, Fail> {
        let scope = scope.filter(|s| !s.is_empty());
        {
            let _held = WRITE.lock().unwrap_or_else(|p| p.into_inner());
            let current = privacy::marks().map_err(unreadable)?;
            let mut rest: Vec<Value> = current
                .iter()
                .filter(|m| !(m.project == project && m.scope.as_deref() == scope))
                .map(|m| json!({"project": m.project, "scope": m.scope, "path": m.path}))
                .collect();
            if private {
                let path = self.target_path(project, scope)?;
                let clashes = self.slug_collisions(project, &path)?;
                if !clashes.is_empty() {
                    return Err(Fail::value(format!(
                        "cannot mark {} private: transcripts of {} share its slug prefix and would be hidden too",
                        path.display(),
                        clashes.join(", ")
                    )));
                }
                rest.push(json!({"project": project, "scope": scope, "path": path_str(&path)}));
            } else if rest.len() == current.len() {
                self.target_path(project, scope)?;
            }
            privacy::write_marks(&rest).map_err(unreadable)?;
        }
        let marks: Vec<Value> = privacy::marks()
            .map_err(unreadable)?
            .into_iter()
            .filter(|m| m.project == project)
            .map(|m| json!({"project": m.project, "scope": m.scope, "path": m.path}))
            .collect();
        Ok(json!({"project": project, "scope": scope, "private": privacy::hidden(project, scope), "marks": marks}))
    }
}
