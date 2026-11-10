//! Seeds one warm session. The launch, and why it is refused with no Claude Code daemon running,
//! live in awm-harness `claude::launch`; read its docs before changing anything here. This module
//! adds what is particular to the pool: the ledger entry, the directory, and which new session counts.

use std::future::Future;
use std::sync::Arc;

use awm_harness::claude::launch::{self, LaunchError, LaunchSpec, ProcessSpawner, SystemHost};
use awm_harness::claude::roster::{self, Job};
use awm_harness::claude::trust;
use awm_harness::claude::ClaudeHome;

use crate::ledger::{self, Stage};
use crate::lineage::Processes;
use crate::pool::{Pool, Terminals};
use crate::sessions::Sessions;

/// Sessions are named so they read as tooling in `claude agents` and nobody deletes one thinking
/// it is abandoned work.
pub const NOUNS: [&str; 40] = [
    "otter", "heron", "badger", "vole", "finch", "stoat", "lynx", "marmot", "quokka", "teal", "ibis", "civet",
    "tapir", "egret", "gannet", "dunlin", "serval", "kestrel", "oryx", "saiga", "jerboa", "numbat", "quoll",
    "dingo", "fossa", "gerbil", "marten", "pika", "ratel", "shrew", "skink", "tanager", "vireo", "weka", "xerus",
    "yapok", "zorilla", "auklet", "bittern", "chough",
];

/// The prefix of a seed's transient unit, `awm-cx-seed-<hex>`.
pub const UNIT_PREFIX: &str = "awm-cx-seed";

/// What seeding reaches beyond the pool's records. [`LiveLauncher`] is the real one.
pub trait Launcher: Send + Sync {
    /// Why this node runs no sessions for others (lifecycle's station refusal), or None.
    fn station_refusal(&self) -> impl Future<Output = Option<String>> + Send;
    /// Why a launch against `home` must not happen now ([`launch::daemon_refusal`]), or None.
    fn daemon_refusal(&self, home: &ClaudeHome) -> impl Future<Output = Option<String>> + Send;
    /// Start the session `spec` describes and return the first new job `accept` takes.
    fn launch(
        &self,
        home: &ClaudeHome,
        spec: &LaunchSpec,
        accept: impl Fn(&Job) -> bool + Send + Sync,
    ) -> impl Future<Output = Result<Job, LaunchError>> + Send;
}

/// The real launcher. `station` is lifecycle's station refusal, run on the blocking pool.
pub struct LiveLauncher<F> {
    pub station: Arc<F>,
}

impl<F> LiveLauncher<F> {
    pub fn new(station: F) -> Self {
        Self { station: Arc::new(station) }
    }
}

impl<F> Clone for LiveLauncher<F> {
    fn clone(&self) -> Self {
        Self { station: self.station.clone() }
    }
}

impl<F: Fn() -> Option<String> + Send + Sync + 'static> Launcher for LiveLauncher<F> {
    async fn station_refusal(&self) -> Option<String> {
        let station = self.station.clone();
        tokio::task::spawn_blocking(move || station()).await.ok().flatten()
    }

    async fn daemon_refusal(&self, home: &ClaudeHome) -> Option<String> {
        let home = home.clone();
        tokio::task::spawn_blocking(move || launch::daemon_refusal(&home, &SystemHost)).await.ok().flatten()
    }

    async fn launch(
        &self,
        home: &ClaudeHome,
        spec: &LaunchSpec,
        accept: impl Fn(&Job) -> bool + Send + Sync,
    ) -> Result<Job, LaunchError> {
        launch::launch(home, spec, accept, &ProcessSpawner, &SystemHost).await
    }
}

/// Why seeding must not happen right now, or None if it may.
pub async fn precondition<P, T>(pool: &Pool<P, T>, launcher: &impl Launcher) -> Option<String>
where
    P: Processes + Clone + Send + Sync + 'static,
    T: Terminals + Clone + Send + Sync + 'static,
{
    if pool.paths.want == 0 {
        return Some("the pool is switched off (AWM_CX_WANT=0)".into());
    }
    if let Some(refusal) = launcher.station_refusal().await {
        return Some(refusal);
    }
    if let Some(refusal) = launcher.daemon_refusal(&pool.paths.home).await {
        return Some(refusal);
    }
    pool.read(|p| {
        if roster::binary_version(&p.paths.claude_bin).is_none() {
            return Some(format!("no claude binary at {}", p.paths.claude_bin.display()));
        }
        let seed_dir = &p.paths.seed_dir;
        seed_dir.join("CLAUDE.md").exists().then(|| {
            format!(
                "the seed directory {} holds a CLAUDE.md, which every session moved out of it would carry into \
                 someone's project",
                seed_dir.display()
            )
        })
    })
    .await
}

fn noun() -> &'static str {
    NOUNS[(uuid::Uuid::new_v4().as_u128() % NOUNS.len() as u128) as usize]
}

/// Start one warm session and return its short id. [`LaunchError::Refused`] when the
/// precondition fails, [`LaunchError::Timeout`] when no new claimable session appears.
pub async fn seed_one<P, T>(pool: &Pool<P, T>, launcher: &impl Launcher) -> Result<String, LaunchError>
where
    P: Processes + Clone + Send + Sync + 'static,
    T: Terminals + Clone + Send + Sync + 'static,
{
    if let Some(refusal) = precondition(pool, launcher).await {
        return Err(LaunchError::Refused(refusal));
    }
    let paths = &*pool.paths;
    if let Err(e) = tokio::fs::create_dir_all(&paths.seed_dir).await {
        return Err(LaunchError::Refused(format!("cannot make the seed directory {}: {e}", paths.seed_dir.display())));
    }
    trust::grant_plain_dir(&paths.seed_dir, &paths.home).await;
    let uuid = uuid::Uuid::new_v4().to_string();
    let id = ledger::key(&uuid).expect("a v4 uuid is a session id");
    let mut spec = LaunchSpec::new(&paths.claude_bin, &paths.seed_dir, noun());
    spec.flags = paths.seed_flags();
    spec.flags.extend(["--session-id".into(), uuid.clone()]);
    spec.env = paths.seed_env();
    spec.unit_prefix = UNIT_PREFIX.into();
    spec.label = "warm".into();
    let procs = &pool.procs;
    let accept = move |new: &Job| {
        let s = Sessions::new(paths, procs);
        new.session_id.as_deref() == Some(uuid.as_str()) && s.claimable(new, s.binary_version().as_deref(), None)
    };
    if let Err(e) = ledger::set(&paths.ledger_path(), &id, Stage::Spare) {
        return Err(LaunchError::Refused(format!("cannot write the pool ledger: {e}")));
    }
    let s = match launcher.launch(&paths.home, &spec, accept).await {
        Ok(s) => s,
        Err(e) => {
            ledger::remove(&paths.ledger_path(), &id);
            return Err(e);
        }
    };
    tracing::info!("cx: seeded {} as {}", s.name, s.short);
    Ok(s.short)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::pool::testing::TestBox;
    use awm_harness::claude::launch::HostProbe;
    use std::sync::Mutex;

    /// A host with a user manager, so only the roster decides whether a daemon runs.
    struct WithManager;

    impl HostProbe for WithManager {
        fn user_manager_env(&self) -> Option<Vec<(String, String)>> {
            Some(vec![("XDG_RUNTIME_DIR".into(), "/run/user/0".into())])
        }
        fn same_mount_ns(&self, _: u32) -> Option<bool> {
            Some(true)
        }
    }

    /// A launcher that reads the box's roster for the daemon, answers `station`, and records each
    /// launch spec instead of starting anything.
    #[derive(Default)]
    pub(crate) struct FakeLauncher {
        pub station: Option<String>,
        pub launched: Mutex<Vec<LaunchSpec>>,
    }

    impl Launcher for FakeLauncher {
        async fn station_refusal(&self) -> Option<String> {
            self.station.clone()
        }

        async fn daemon_refusal(&self, home: &ClaudeHome) -> Option<String> {
            launch::daemon_refusal(home, &WithManager)
        }

        async fn launch(
            &self,
            home: &ClaudeHome,
            spec: &LaunchSpec,
            accept: impl Fn(&Job) -> bool + Send + Sync,
        ) -> Result<Job, LaunchError> {
            self.launched.lock().unwrap().push(spec.clone());
            let uuid = spec.flags.iter().skip_while(|f| *f != "--session-id").nth(1).unwrap();
            let mut roster: serde_json::Value = serde_json::from_slice(&std::fs::read(home.roster()).unwrap()).unwrap();
            roster["workers"]["warmfresh"]["sessionId"] = uuid.as_str().into();
            std::fs::write(home.roster(), roster.to_string()).unwrap();
            roster::load(home)
                .into_iter()
                .find(|j| accept(j))
                .ok_or_else(|| LaunchError::Timeout("no new warm session appeared within 45s".into()))
        }
    }

    #[tokio::test]
    async fn test_seeding_is_refused_when_no_daemon_is_running() {
        // The protection that keeps the user's whole fleet out of awm's cgroup.
        let b = TestBox::new();
        std::fs::remove_file(b.paths.home.roster()).unwrap();
        let why = precondition(&b.pool(), &FakeLauncher::default()).await;
        assert!(why.unwrap_or_default().contains("no claude code daemon"));
    }

    #[tokio::test]
    async fn a_seed_is_named_from_the_nouns_ledgered_and_launched_as_a_warm_seed_unit() {
        let b = TestBox::new();
        let launcher = FakeLauncher::default();
        let pool = b.pool_with(|p| p.rotate_age_s = 999_999.0);
        std::fs::write(&b.paths.home.trust_file, r#"{"projects":{}}"#).unwrap();
        assert_eq!(seed_one(&pool, &launcher).await.unwrap(), "warmfresh");
        let spec = launcher.launched.lock().unwrap()[0].clone();
        assert!(NOUNS.contains(&spec.name.as_str()));
        assert_eq!(spec.unit_prefix, "awm-cx-seed");
        assert_eq!(spec.label, "warm");
        assert_eq!(spec.cwd, b.paths.seed_dir);
        let (given, id) = spec.flags.split_at(spec.flags.len() - 2);
        assert_eq!(given, b.paths.seed_flags());
        assert_eq!(id[0], "--session-id");
        let key = ledger::key(&id[1]).unwrap();
        assert_eq!(ledger::load(&b.paths.ledger_path()).get(&key), Some(&Stage::Spare));
        assert_eq!(spec.env, b.paths.seed_env());
        let trust = std::fs::read_to_string(&b.paths.home.trust_file).unwrap();
        assert!(trust.contains(b.paths.seed_dir.to_str().unwrap()), "{trust}");
    }

}
