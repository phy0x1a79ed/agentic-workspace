//! Where a provider's work dirs and instances live. Every file operation on them is a shell
//! script run there: `sh -s` on this host, or one `ssh -o BatchMode=yes` riding the host's
//! ControlMaster. BatchMode turns a dead master into a failed call instead of a login prompt.

use crate::jobs::q;
use anyhow::bail;
use std::path::Path;
use std::process::Stdio;
use tokio::io::AsyncWriteExt;
use tokio::process::Command;

#[derive(Debug, Clone, Default)]
pub struct Host {
    ssh: Option<String>,
}

impl Host {
    /// `None` is this host; `Some` is an ssh destination, e.g. `fir`.
    pub fn new(ssh: Option<String>) -> Host {
        Host { ssh }
    }

    pub fn is_local(&self) -> bool {
        self.ssh.is_none()
    }

    fn command(&self) -> Command {
        match &self.ssh {
            None => {
                let mut c = Command::new("sh");
                c.arg("-s");
                c
            }
            Some(dest) => {
                let mut c = Command::new("ssh");
                c.args(["-o", "BatchMode=yes", dest, "sh -s"]);
                c
            }
        }
    }

    /// Runs `script` there and returns its stdout. Dropping the future kills the run.
    pub async fn sh(&self, script: &str) -> anyhow::Result<String> {
        let mut child = self
            .command()
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()?;
        let mut stdin = child.stdin.take().expect("piped stdin");
        let feed = async move { stdin.write_all(script.as_bytes()).await };
        let (fed, out) = tokio::join!(feed, child.wait_with_output());
        let out = out?;
        if !out.status.success() {
            bail!("{}", String::from_utf8_lossy(&out.stderr).trim());
        }
        fed?;
        Ok(String::from_utf8_lossy(&out.stdout).into_owned())
    }

    /// The regular files under `path` (a symlink at `path` itself is followed) as relative path
    /// and bytes; a file lists itself as `""`. Fails when `path` is missing.
    pub async fn files(&self, path: &Path) -> anyhow::Result<Vec<(String, u64)>> {
        let out = self.sh(&format!("find -H {} -type f -printf '%P\\t%s\\n'", q(&path.to_string_lossy()))).await?;
        let mut files: Vec<(String, u64)> = out
            .lines()
            .filter_map(|l| l.rsplit_once('\t'))
            .map(|(p, s)| (p.to_string(), s.parse().unwrap_or(0)))
            .collect();
        files.sort();
        Ok(files)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_remote_host_never_prompts() {
        let c = Host::new(Some("fir".into())).command();
        let argv: Vec<_> = c.as_std().get_args().map(|a| a.to_string_lossy().into_owned()).collect();
        assert_eq!(c.as_std().get_program(), "ssh");
        assert_eq!(argv, ["-o", "BatchMode=yes", "fir", "sh -s"]);
    }
}
