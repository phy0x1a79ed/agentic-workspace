//! A seat's own container, only when the seat asks for one (fleet.md R102 items 5-6).
//!
//! The container's profile is a copy of Tony's or a blank one. Its debug port is published on
//! loopback only, for the singleton.

use std::future::Future;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use awm_cdp::Docker;

use crate::seats::{Cdp, LaunchFuture, Launcher};

pub const NAME_PREFIX: &str = "awm-browser-seat-";

pub struct Containers {
    pub image: String,
    pub seats_dir: PathBuf,
    pub tony_profile: Option<PathBuf>,
    pub docker: Docker,
}

pub fn container_name(seat_id: &str) -> String {
    let short: String = seat_id.chars().take(12).collect();
    format!("{NAME_PREFIX}{}", short.to_lowercase().replace('_', "-"))
}

/// Copy a Chrome profile, keeping symlinks and leaving out its lock files.
fn copy_profile(from: &Path, to: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(to)?;
    for entry in std::fs::read_dir(from)? {
        let entry = entry?;
        let name = entry.file_name();
        let s = name.to_string_lossy();
        if s.starts_with("Singleton") || s.ends_with(".lock") {
            continue;
        }
        let (src, dst) = (entry.path(), to.join(&name));
        let kind = entry.file_type()?;
        if kind.is_symlink() {
            std::os::unix::fs::symlink(std::fs::read_link(&src)?, &dst)?;
        } else if kind.is_dir() {
            copy_profile(&src, &dst)?;
        } else {
            std::fs::copy(&src, &dst)?;
        }
    }
    Ok(())
}

impl Containers {
    fn seat_dir(&self, seat_id: &str) -> PathBuf {
        self.seats_dir.join(seat_id)
    }

    async fn launch_inner(&self, seat_id: String, profile: String) -> anyhow::Result<(String, u16)> {
        let target = self.seat_dir(&seat_id).join("profile");
        let tony = self.tony_profile.clone();
        let t = target.clone();
        tokio::task::spawn_blocking(move || -> anyhow::Result<()> {
            if t.exists() {
                std::fs::remove_dir_all(&t)?;
            }
            if profile == "tony" {
                let tony = tony.filter(|p| p.is_dir()).ok_or_else(|| anyhow::anyhow!("no copy of Tony's profile is configured on this browser"))?;
                copy_profile(&tony, &t)?;
            } else {
                std::fs::create_dir_all(&t)?;
            }
            Ok(())
        })
        .await??;
        let port = awm_cdp::free_port()?;
        let name = container_name(&seat_id);
        let (publish, env, volume) = (format!("127.0.0.1:{port}:{port}"), format!("CDP_PORT={port}"), format!("{}:/profile", target.display()));
        self.docker.run(&["run", "-d", "--rm", "--name", &name, "--shm-size", "1g", "-p", &publish, "-e", &env, "-v", &volume, &self.image]).await?;
        if awm_cdp::wait_ready(&format!("http://127.0.0.1:{port}"), Duration::from_secs(30)).await.is_ok() {
            return Ok((name, port));
        }
        self.docker.remove(&name).await;
        anyhow::bail!("seat container {name} did not open its debug port")
    }

    /// Remove the seat containers no recorded seat owns, then the seat folders no running
    /// container mounts. The Python plugin kept its seats in memory, so its containers have
    /// no owner after the first Rust start.
    pub async fn reap(&self, recorded_seats: &[String], recorded_containers: &[String]) -> anyhow::Result<()> {
        for name in self.docker.names(NAME_PREFIX).await? {
            if !recorded_containers.contains(&name) {
                tracing::info!(%name, "removing a seat container no recorded seat owns");
                self.docker.remove(&name).await;
            }
        }
        let mut mounted = Vec::new();
        for name in self.docker.names(NAME_PREFIX).await? {
            let out = self.docker.run(&["inspect", "--format", "{{range .Mounts}}{{.Source}}\n{{end}}", &name]).await.unwrap_or_default();
            mounted.extend(out.lines().map(PathBuf::from));
        }
        let Ok(dir) = std::fs::read_dir(&self.seats_dir) else { return Ok(()) };
        for entry in dir.flatten() {
            let id = entry.file_name().to_string_lossy().to_string();
            let path = entry.path();
            if recorded_seats.contains(&id) || mounted.iter().any(|m| m.starts_with(&path)) {
                continue;
            }
            tracing::info!(seat = %id, "deleting the profile copy of a seat that no longer runs");
            let _ = tokio::task::spawn_blocking(move || std::fs::remove_dir_all(path)).await;
        }
        Ok(())
    }
}

impl Launcher for Arc<Containers> {
    fn launch(&self, seat_id: &str, profile: &str) -> LaunchFuture {
        let (me, s, p) = (self.clone(), seat_id.to_string(), profile.to_string());
        Box::pin(async move { me.launch_inner(s, p).await })
    }

    fn stop(&self, name: &str) -> Pin<Box<dyn Future<Output = ()> + Send>> {
        let (docker, name) = (self.docker.clone(), name.to_string());
        Box::pin(async move { docker.remove(&name).await })
    }

    fn connect(&self, port: u16) -> Arc<dyn Cdp> {
        Arc::new(awm_cdp::Browser::new(&format!("http://127.0.0.1:{port}")))
    }

    fn forget(&self, seat_id: &str) {
        let dir = self.seat_dir(seat_id);
        if let Err(e) = std::fs::remove_dir_all(&dir) {
            if e.kind() != std::io::ErrorKind::NotFound {
                tracing::warn!(error = %e, dir = %dir.display(), "could not delete a seat's profile copy");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn profile_copy_skips_locks_and_keeps_links() {
        let tmp = tempfile::tempdir().unwrap();
        let from = tmp.path().join("p");
        std::fs::create_dir_all(from.join("Default")).unwrap();
        std::fs::write(from.join("Default/Cookies"), "c").unwrap();
        std::fs::write(from.join("SingletonLock"), "").unwrap();
        std::fs::write(from.join("x.lock"), "").unwrap();
        std::os::unix::fs::symlink("Default", from.join("link")).unwrap();
        let to = tmp.path().join("q");
        copy_profile(&from, &to).unwrap();
        assert!(to.join("Default/Cookies").exists());
        assert!(!to.join("SingletonLock").exists() && !to.join("x.lock").exists());
        assert!(std::fs::symlink_metadata(to.join("link")).unwrap().file_type().is_symlink());
    }
}
