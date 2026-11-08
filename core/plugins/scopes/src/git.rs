//! The `git` binary, run as the Python ran it: output captured, the process group killed
//! when the caller cancels. libgit2 and gix run no hooks or merge drivers, lack the
//! worktree porcelain and cannot use the `gh` credential helper.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::{Fail, Op};

/// A finished command: Python's `CompletedProcess` with text output.
#[derive(Debug, Clone, Default)]
pub struct Out {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

impl Out {
    pub fn ok(&self) -> bool {
        self.code == 0
    }
}

/// `shutil.which`: the first executable named `name` on `PATH`.
pub fn which(name: &str) -> Option<PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path)
        .map(|d| d.join(name))
        .find(|p| std::fs::metadata(p).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0))
}

impl Op {
    /// Run `argv` to completion. Only a cancelled call or a binary that cannot start fails.
    pub fn run(&self, argv: &[&str], cwd: Option<&Path>) -> Result<Out, Fail> {
        let mut cmd = tokio::process::Command::new(argv[0]);
        cmd.args(&argv[1..]).env("LC_ALL", "C").stdin(std::process::Stdio::null());
        if let Some(d) = cwd {
            cmd.current_dir(d);
        }
        let out = self.rt.block_on(awm_plugin_sdk::cancel::run(&self.cancel, cmd)).map_err(|e| {
            if e.message.starts_with("spawn: ") {
                Fail::not_found(format!("[Errno 2] No such file or directory: {}", awm_pycompat::repr::str_repr(argv[0])))
            } else {
                Fail::new(&e.error_class, e.message)
            }
        })?;
        Ok(Out {
            code: out.status.code().unwrap_or(-1),
            stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
        })
    }

    /// `git <args>`.
    pub fn git(&self, args: &[&str]) -> Result<Out, Fail> {
        let mut argv = vec!["git"];
        argv.extend_from_slice(args);
        self.run(&argv, None)
    }

    /// `git -C <dir> <args>`.
    pub fn git_in(&self, dir: &Path, args: &[&str]) -> Result<Out, Fail> {
        let d = dir.to_string_lossy();
        let mut argv = vec!["git", "-C", &d];
        argv.extend_from_slice(args);
        self.run(&argv, None)
    }

    /// `run(..., check=True)`: a non-zero exit raises `CalledProcessError`.
    pub fn checked(&self, argv: &[&str]) -> Result<Out, Fail> {
        let out = self.run(argv, None)?;
        if !out.ok() {
            let quoted: Vec<String> = argv.iter().map(|a| awm_pycompat::repr::str_repr(a)).collect();
            return Err(Fail::new("CalledProcessError", format!("Command '[{}]' returned non-zero exit status {}.", quoted.join(", "), out.code)));
        }
        Ok(out)
    }

    /// The default branch of a bare repo: its HEAD, else main or master, else origin's HEAD,
    /// else `main`.
    pub fn default_branch(&self, bare: &Path) -> Result<String, Fail> {
        let r = self.git_in(bare, &["symbolic-ref", "HEAD"])?;
        if r.ok() {
            if let Some(b) = r.stdout.trim().strip_prefix("refs/heads/") {
                return Ok(b.to_string());
            }
        }
        for b in ["main", "master"] {
            if self.git_in(bare, &["rev-parse", "--verify", b])?.ok() {
                return Ok(b.to_string());
            }
        }
        let r = self.git_in(bare, &["symbolic-ref", "refs/remotes/origin/HEAD"])?;
        if r.ok() {
            let refname = r.stdout.trim();
            if let Some((_, last)) = refname.rsplit_once('/') {
                return Ok(last.to_string());
            }
        }
        Ok("main".into())
    }

    /// The commit a ref names, or `None`.
    pub fn ref_sha(&self, bare: &Path, refname: &str) -> Result<Option<String>, Fail> {
        let r = self.git_in(bare, &["rev-parse", "--verify", "--quiet", &format!("{refname}^{{commit}}")])?;
        let sha = r.stdout.trim();
        Ok((r.ok() && !sha.is_empty()).then(|| sha.to_string()))
    }

    /// Each worktree of `bare` in git's order, with its checked-out branch, `None` when
    /// detached. The bare repo's own entry is left out.
    pub fn worktree_branches(&self, bare: &Path) -> Result<Vec<(PathBuf, Option<String>)>, Fail> {
        let r = self.git_in(bare, &["worktree", "list", "--porcelain"])?;
        if !r.ok() {
            return Err(Fail::runtime(format!("Could not list worktrees of {}: {}", bare.display(), r.stderr.trim())));
        }
        let mut out: Vec<(PathBuf, Option<String>)> = Vec::new();
        let mut current: Option<usize> = None;
        for line in r.stdout.lines() {
            if let Some(p) = line.strip_prefix("worktree ") {
                let p = PathBuf::from(p);
                out.retain(|(q, _)| *q != p);
                out.push((p, None));
                current = Some(out.len() - 1);
            } else if line.trim() == "bare" {
                if let Some(i) = current.take() {
                    out.remove(i);
                }
            } else if let (Some(b), Some(i)) = (line.strip_prefix("branch refs/heads/"), current) {
                out[i].1 = Some(b.to_string());
            }
        }
        Ok(out)
    }

    /// Each local branch a worktree of `bare` has checked out, with that worktree.
    pub fn branch_checkouts(&self, bare: &Path) -> Result<BTreeMap<String, PathBuf>, Fail> {
        Ok(self.worktree_branches(bare)?.into_iter().filter_map(|(p, b)| b.map(|b| (b, p))).collect())
    }

    /// Every worktree path git lists for `bare`, in its order, the bare entry left out.
    pub fn worktree_paths(&self, bare: &Path) -> Result<Option<Vec<PathBuf>>, Fail> {
        let r = self.git_in(bare, &["worktree", "list", "--porcelain"])?;
        if !r.ok() {
            return Ok(None);
        }
        Ok(Some(r.stdout.lines().filter_map(|l| l.strip_prefix("worktree ")).map(PathBuf::from).collect()))
    }
}

/// The branch the bare repo's HEAD names, read from the file without spawning git.
pub fn head_branch(bare: &Path) -> Option<String> {
    let head = std::fs::read_to_string(bare.join("HEAD")).ok()?;
    head.trim().strip_prefix("ref: refs/heads/").map(str::to_string)
}
