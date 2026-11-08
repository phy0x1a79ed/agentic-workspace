//! Pull this host's pinned tether binaries from the tether repository's release.
//!
//! The tag the binary pins is compared with the `VERSION` stamp beside the binaries. When they
//! differ, the role's assets are fetched into a stage, each is checked against the release's
//! `SHA256SUMS`, and only then are they moved into place, with `VERSION` stamped last. A failed
//! fetch leaves the binaries in place untouched, and `status` reports the version lag.

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use sha2::{Digest, Sha256};

use super::paths::{pinned, Paths, Role};

/// Where the assets come from: the release download base, or a local directory for a test.
pub enum Source {
    Release(String),
    Dir(PathBuf),
}

impl Source {
    /// `TETHER_RELEASE_DIR`, else `TETHER_GITHUB_BASE`, else the repository's GitHub releases.
    pub fn from_env() -> Self {
        let var = |n: &str| std::env::var(n).ok().map(|v| v.trim().to_string()).filter(|v| !v.is_empty());
        if let Some(dir) = var("TETHER_RELEASE_DIR") {
            return Source::Dir(dir.into());
        }
        let repo = var("TETHER_REPO").unwrap_or_else(|| "phy0x1a79ed/tether".into());
        Source::Release(var("TETHER_GITHUB_BASE").unwrap_or_else(|| format!("https://github.com/{repo}/releases/download")))
    }
}

fn log(paths: &Paths, line: &str) {
    use std::io::Write;
    let _ = std::fs::create_dir_all(&paths.state_dir);
    let stamp = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ");
    let text = format!("{stamp} tether fetch: {line}\n");
    tracing::info!("tether fetch: {line}");
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(paths.fetch_log()) {
        let _ = f.write_all(text.as_bytes());
    }
}

async fn get(source: &Source, client: &reqwest::Client, tag: &str, asset: &str) -> Result<Vec<u8>, String> {
    match source {
        Source::Dir(dir) => std::fs::read(dir.join(asset)).map_err(|e| e.to_string()),
        Source::Release(base) => {
            let resp = client.get(format!("{base}/{tag}/{asset}")).timeout(Duration::from_secs(180)).send().await.map_err(|e| e.to_string())?;
            if !resp.status().is_success() {
                return Err(format!("HTTP {}", resp.status()));
            }
            resp.bytes().await.map(|b| b.to_vec()).map_err(|e| e.to_string())
        }
    }
}

/// The role's assets, by their name in the release, beside where each lands.
fn wanted(paths: &Paths, sums: &[(String, String)]) -> Vec<(String, PathBuf)> {
    match paths.role {
        Role::Operator => vec![("tether-operator-linux-x86_64".into(), paths.bin_dir.join("tether-operator"))],
        Role::Relay => {
            let mut out = vec![
                ("tether-relay-linux-x86_64".into(), paths.bin_dir.join("tether-relay")),
                ("tether".into(), paths.assets_dir.join("tether")),
                ("tether.ps1".into(), paths.assets_dir.join("tether.ps1")),
            ];
            for (_, name) in sums {
                let skip = ["SHA256SUMS", "tether", "tether.ps1"].contains(&name.as_str())
                    || name.starts_with("tether-relay-")
                    || name.starts_with("tether-operator-");
                if !skip && name.starts_with("tether-") {
                    out.push((name.clone(), paths.assets_dir.join("bin").join(name)));
                }
            }
            out
        }
    }
}

fn parse_sums(text: &str) -> Vec<(String, String)> {
    text.lines()
        .filter_map(|l| {
            let mut parts = l.split_whitespace();
            Some((parts.next()?.to_string(), parts.next()?.trim_start_matches('*').to_string()))
        })
        .collect()
}

/// Fetch the pinned release when the installed one differs. Ok(false) means nothing to do.
pub async fn fetch(paths: &Paths, source: &Source, client: &reqwest::Client) -> Result<bool, String> {
    if paths.bin_override.is_some() {
        return Ok(false);
    }
    let tag = pinned();
    if paths.installed().as_deref() == Some(tag) {
        return Ok(false);
    }
    let fail = |why: String| {
        log(paths, &why);
        Err(why)
    };
    log(paths, &format!("pinned {tag}, installed {}; fetching role {}", paths.installed().unwrap_or_else(|| "none".into()), paths.role.as_str()));
    let sums = match get(source, client, tag, "SHA256SUMS").await {
        Ok(b) => parse_sums(&String::from_utf8_lossy(&b)),
        Err(e) => return fail(format!("could not fetch SHA256SUMS for {tag} ({e}); leaving the binaries as they are")),
    };
    let stage = tempfile::Builder::new().prefix("tether-fetch.").tempdir_in(&paths.state_dir).or_else(|_| tempfile::tempdir()).map_err(|e| e.to_string())?;
    let assets = wanted(paths, &sums);
    for (asset, _) in &assets {
        let Some((want, _)) = sums.iter().find(|(_, n)| n == asset) else {
            return fail(format!("{asset} is not listed in SHA256SUMS for {tag}; aborting"));
        };
        let bytes = match get(source, client, tag, asset).await {
            Ok(b) => b,
            Err(e) => return fail(format!("could not fetch {asset} for {tag} ({e}); aborting, binaries unchanged")),
        };
        let got = hex::encode(Sha256::digest(&bytes));
        if &got != want {
            return fail(format!("{asset} does not match SHA256SUMS (want {want}, got {got}); aborting, binaries unchanged"));
        }
        std::fs::write(stage.path().join(asset), &bytes).map_err(|e| e.to_string())?;
    }
    for (asset, dest) in &assets {
        install(&stage.path().join(asset), dest).map_err(|e| format!("could not install {asset}: {e}"))?;
    }
    std::fs::create_dir_all(&paths.bin_dir).map_err(|e| e.to_string())?;
    std::fs::write(paths.version_file(), format!("{tag}\n")).map_err(|e| e.to_string())?;
    log(paths, &format!("installed {tag} for role {}", paths.role.as_str()));
    Ok(true)
}

/// Move `from` over `dest` by rename, so a running binary keeps its own inode and reads as
/// deleted, which is how a replacement is noticed.
fn install(from: &Path, dest: &Path) -> std::io::Result<()> {
    if let Some(dir) = dest.parent() {
        std::fs::create_dir_all(dir)?;
    }
    std::fs::set_permissions(from, std::fs::Permissions::from_mode(0o755))?;
    let next = dest.with_extension("fetching");
    std::fs::copy(from, &next)?;
    std::fs::rename(&next, dest)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn release(dir: &Path, names: &[&str]) {
        let mut sums = String::new();
        for n in names {
            let body = format!("binary {n}");
            std::fs::write(dir.join(n), &body).unwrap();
            sums += &format!("{}  {n}\n", hex::encode(Sha256::digest(body.as_bytes())));
        }
        std::fs::write(dir.join("SHA256SUMS"), sums).unwrap();
    }

    #[tokio::test]
    async fn the_relay_takes_its_binary_and_every_client() {
        let rel = tempfile::tempdir().unwrap();
        release(rel.path(), &["tether-relay-linux-x86_64", "tether-operator-linux-x86_64", "tether", "tether.ps1", "tether-owner-linux-x86_64", "tether-owner-windows-x86_64.exe"]);
        let awm = tempfile::tempdir().unwrap();
        let mut paths = Paths::new(Role::Relay, awm.path());
        (paths.bin_override, paths.bin_dir, paths.assets_dir) = (None, paths.state_dir.join("bin"), paths.state_dir.join("assets"));
        assert!(fetch(&paths, &Source::Dir(rel.path().into()), &reqwest::Client::new()).await.unwrap());
        let st = &paths.state_dir;
        assert!(st.join("bin/tether-relay").is_file() && !st.join("bin/tether-operator").exists());
        assert!(st.join("assets/tether").is_file() && st.join("assets/tether.ps1").is_file());
        assert!(st.join("assets/bin/tether-owner-linux-x86_64").is_file() && st.join("assets/bin/tether-owner-windows-x86_64.exe").is_file());
        assert_eq!(paths.installed().as_deref(), Some(pinned()));
        assert!(!fetch(&paths, &Source::Dir(rel.path().into()), &reqwest::Client::new()).await.unwrap(), "in sync: nothing to do");
    }

    #[tokio::test]
    async fn the_operator_takes_only_the_daemon_and_a_bad_sum_installs_nothing() {
        let rel = tempfile::tempdir().unwrap();
        release(rel.path(), &["tether-relay-linux-x86_64", "tether-operator-linux-x86_64"]);
        let awm = tempfile::tempdir().unwrap();
        let mut paths = Paths::new(Role::Operator, awm.path());
        (paths.bin_override, paths.bin_dir, paths.assets_dir) = (None, paths.state_dir.join("bin"), paths.state_dir.join("assets"));
        std::fs::write(rel.path().join("tether-operator-linux-x86_64"), "tampered").unwrap();
        assert!(fetch(&paths, &Source::Dir(rel.path().into()), &reqwest::Client::new()).await.unwrap_err().contains("does not match"));
        assert!(!paths.state_dir.join("bin/tether-operator").exists() && paths.installed().is_none());
        assert!(paths.release_report()["last_fetch"].as_str().unwrap().contains("does not match"));
        release(rel.path(), &["tether-operator-linux-x86_64"]);
        assert!(fetch(&paths, &Source::Dir(rel.path().into()), &reqwest::Client::new()).await.unwrap());
        assert!(paths.state_dir.join("bin/tether-operator").is_file() && !paths.state_dir.join("bin/tether-relay").exists());
        assert_eq!(paths.release_report()["in_sync"], true);
    }
}
