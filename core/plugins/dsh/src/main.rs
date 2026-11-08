//! The dsh plugin: supervises the DeepSeek harness web fork on loopback, steers its model route,
//! serves harness@1 over its sessions as `harness-ds`, and mounts the fork at `/s/dsh/`.

mod fork;
mod lifecycle;
mod mount;
mod paths;
mod relink;
mod sessions;
mod settings;
mod snapshot;
mod supervisor;
mod verbs;

use std::sync::Arc;
use std::time::Duration;

use awm_harness::contract::Reporter;
use awm_plugin_sdk::{Call, Service};
use clap::Parser;

use crate::lifecycle::{HubReflection, Lifecycle};
use crate::paths::Paths;
use crate::sessions::{ProcFs, Sessions, COMPACT_WAIT, POLL};
use crate::settings::Settings;
use crate::supervisor::Supervisor;
use crate::verbs::{specs, App, HARNESS_TOOL};

/// `dsh state <cmd>`: the pre-redux state check.
#[derive(clap::Parser)]
struct StateCli {
    #[command(subcommand)]
    cmd: awm_statecheck::cli::StateCmd,
}

/// Every verb of [`specs`], bound to `app`.
fn service(svc: Service, app: Arc<App>) -> Service {
    specs().into_iter().fold(svc, |svc, spec| {
        let (tool, verb, app) = (spec.tool, spec.verb, app.clone());
        let mut svc = svc.verb(tool, verb, &spec.description, spec.params, move |c: Call| {
            let app = app.clone();
            async move { app.handle(tool, verb, &c.args).await }
        });
        if tool == HARNESS_TOOL {
            svc = svc.function(&format!("harness_{verb}")).surfaces(["cli", "http"]);
        }
        match spec.timeout {
            Some(t) => svc.timeout(Duration::from_secs(t)),
            None => svc,
        }
    })
}

impl App {
    /// Before the harness first reads its home: retarget moved config links and seed the route.
    async fn boot(self: &Arc<Self>) {
        let home = self.sup.paths.home.clone();
        let file = self.settings.file.clone();
        let done = tokio::task::spawn_blocking(move || (relink::relink(&home), Settings::new(file).ensure())).await;
        match done {
            Ok((_, seeded)) if seeded["changed"] == true => tracing::info!("dsh: model route seeded"),
            Ok(_) => tracing::info!("dsh: model route already declared"),
            Err(e) => tracing::error!("dsh: boot step failed: {e}"),
        }
        if !self.sup.installed() {
            tracing::warn!(
                "dsh: no built harness at {}; the service registers and reports this via status",
                self.sup.paths.cli().display()
            );
            return;
        }
        let app = self.clone();
        tokio::spawn(async move {
            match app.sup.start().await {
                Ok(snap) => {
                    tracing::info!("dsh: harness pid={} version={} listening={}", snap["pid"], snap["version"], snap["listening"]);
                    app.sessions.lifecycle.kick();
                }
                Err(e) => tracing::error!("dsh: initial start failed, the health loop retries: {e}"),
            }
        });
    }

    /// Once the service is reachable: resend the events reflection did not take last time.
    async fn after_ready(&self) -> usize {
        let sent = self.sessions.reporter.resend().await;
        if sent > 0 {
            tracing::info!("dsh: resent {sent} spooled harness events");
        }
        sent
    }
}

/// Respawn a harness that died while wanted, every `DSH_HEALTH_INTERVAL_S`. It watches the
/// process, not an HTTP probe: a slow probe during a long completion is not a death.
fn health(app: Arc<App>) -> awm_plugin_sdk::Supervised {
    awm_plugin_sdk::spawn_supervised("dsh:health", Duration::from_secs(5), move || {
        let app = app.clone();
        async move {
            loop {
                tokio::time::sleep(app.sup.paths.health_interval).await;
                let res = app.sup.reconcile().await;
                if res["action"] == "respawned" {
                    tracing::warn!("dsh: harness respawned (previous exit {})", res["previous_exit"]);
                    app.sup.await_listening().await;
                    app.sessions.lifecycle.kick();
                }
            }
        }
    })
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    let awm_dir = awm_plugin_sdk::config::awm_dir();
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("state") {
        let cli = StateCli::parse_from(std::iter::once("dsh state".to_string()).chain(args.into_iter().skip(1)));
        std::process::exit(awm_statecheck::cli::run(&cli.cmd, "dsh", &snapshot::decl(&awm_dir)?)?);
    }
    snapshot::take(&awm_dir)?;
    let svc = Service::from_env()?;
    let paths = Paths::from_env();
    let (lc, session_events) = Lifecycle::new();
    let app = Arc::new(App {
        sessions: Sessions {
            fork: fork::Fork::new(paths.port),
            procs: Box::new(ProcFs),
            reporter: Reporter::new(svc.context().hub().clone(), paths.spool()),
            lifecycle: lc.clone(),
            compact_wait: COMPACT_WAIT,
            poll: POLL,
        },
        settings: Settings::new(paths.settings_file()),
        hub: svc.context().hub().clone(),
        workspace: paths.workspace.clone(),
        sup: Supervisor::new(paths.clone()),
    });
    let (boot, ready, down) = (app.clone(), app.clone(), app.clone());
    service(svc, app)
        .mount(mount::router(paths.upstream()))
        .on_start(async move {
            boot.boot().await;
            Ok(())
        })
        .on_ready(async move {
            ready.after_ready().await;
            let reflection = HubReflection(ready.hub.clone());
            lifecycle::spawn(lc, session_events, ready.sessions.fork.clone(), reflection);
            health(ready);
        })
        .on_shutdown(async move {
            down.sup.stop().await;
        })
        .run()
        .await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fork::stub;
    use crate::lifecycle::Lifecycle;
    use crate::sessions::tests::{recording, FakeProcs};
    use crate::verbs::CONTRACT_VERBS;
    use serde_json::{json, Value};

    fn baseline(name: &str) -> Value {
        let text = match name {
            "dsh" => include_str!("../../../../docs/plugin-redux/ports/baseline/dsh.describe.json"),
            _ => include_str!("../../../../docs/plugin-redux/ports/baseline/harness-ds.describe.json"),
        };
        serde_json::from_str(text).unwrap()
    }

    /// A param as `describe` shows it: its type only when it is not a string.
    fn as_described(param: &Value) -> Value {
        let mut p = param.clone();
        if p["type"] == "string" {
            p.as_object_mut().unwrap().remove("type");
        }
        p
    }

    /// Every difference between our verbs and the baseline: `(tool, verb, what)`.
    fn differences() -> Vec<(String, String, String)> {
        let mut diffs = vec![];
        for tool in ["dsh", HARNESS_TOOL] {
            let base = baseline(tool);
            let base_verbs = base["verbs"].as_array().unwrap();
            let ours: Vec<_> = specs().into_iter().filter(|s| s.tool == tool).collect();
            if ours.len() != base_verbs.len() {
                diffs.push((tool.into(), "*".into(), format!("{} verbs, baseline {}", ours.len(), base_verbs.len())));
            }
            for spec in ours {
                let Some(b) = base_verbs.iter().find(|v| v["verb"] == spec.verb) else {
                    diffs.push((tool.into(), spec.verb.into(), "not in the baseline".into()));
                    continue;
                };
                if b["description"] != spec.description.as_str() {
                    diffs.push((tool.into(), spec.verb.into(), "description".into()));
                }
                let surfaces = if tool == HARNESS_TOOL { json!(["cli", "http"]) } else { Value::Null };
                if b.get("surfaces").cloned().unwrap_or(Value::Null) != surfaces {
                    diffs.push((tool.into(), spec.verb.into(), "surfaces".into()));
                }
                let (bp, op) = (b["params"].as_array().unwrap(), spec.params.as_array().unwrap());
                if bp.len() != op.len() {
                    diffs.push((tool.into(), spec.verb.into(), "param count".into()));
                }
                for (want, got) in bp.iter().zip(op) {
                    let mut got = as_described(got);
                    if got == *want {
                        continue;
                    }
                    let added = got.as_object_mut().unwrap().remove("description");
                    let what = if added.is_some() && got == *want { "description added" } else { "changed" };
                    diffs.push((tool.into(), spec.verb.into(), format!("{} {what}", want["name"].as_str().unwrap())));
                }
            }
        }
        diffs
    }

    #[test]
    fn describe_differs_from_the_baseline_only_by_the_listed_changes() {
        let target_verbs = ["compact", "interrupt", "resume", "send", "set", "status", "stop"];
        let mut want: Vec<(String, String, String)> = target_verbs
            .iter()
            .flat_map(|v| ["session_id", "ref", "pid"].map(|p| (HARNESS_TOOL.to_string(), v.to_string(), format!("{p} description added"))))
            .collect();
        let mut got = differences();
        want.sort();
        got.sort();
        assert_eq!(got, want);
    }

    #[test]
    fn the_harness_contract_verbs_project_into_harness_ds_off_mcp() {
        let harness: Vec<_> = specs().into_iter().filter(|s| s.tool == HARNESS_TOOL).collect();
        let mut names: Vec<&str> = harness.iter().map(|s| s.verb).collect();
        let mut want = CONTRACT_VERBS.to_vec();
        names.sort();
        want.sort();
        assert_eq!(names, want);
    }

    #[test]
    fn every_function_projects_into_a_single_domain() {
        for s in specs() {
            let flat = format!("{}_{}", s.tool, s.verb);
            assert_eq!(flat.split_once('_'), Some((s.tool, s.verb)), "the gateway folds {flat} on its first underscore");
            assert!(["dsh", HARNESS_TOOL].contains(&s.tool));
        }
    }

    #[test]
    fn the_projected_verb_matches_the_internal_name() {
        let dsh: Vec<&str> = specs().into_iter().filter(|s| s.tool == "dsh").map(|s| s.verb).collect();
        assert_eq!(dsh, ["status", "start", "stop", "restart", "url", "model", "logs"]);
    }

    #[tokio::test]
    async fn every_declared_function_has_a_handler() {
        let dir = tempfile::tempdir().unwrap();
        let port = stub::dead_port().await;
        let paths = Paths::under(dir.path(), port);
        let (reporter, _) = recording(paths.spool(), true);
        let app = Arc::new(App {
            sup: Supervisor::new(paths.clone()),
            sessions: Sessions { fork: fork::Fork::new(port), procs: Box::new(FakeProcs::default()), reporter, lifecycle: Lifecycle::new().0, compact_wait: COMPACT_WAIT, poll: POLL },
            settings: Settings::new(paths.settings_file()),
            hub: awm_plugin_sdk::Hub::new(&format!("http://127.0.0.1:{port}"), None),
            workspace: paths.workspace.clone(),
        });
        for s in specs() {
            let out = match app.handle(s.tool, s.verb, &json!({})).await {
                Ok(v) => v.to_string(),
                Err(e) => e.message,
            };
            assert!(!out.contains("has no verb"), "{} {}: {out}", s.tool, s.verb);
        }
    }

    #[test]
    fn the_lifecycle_verbs_outlast_the_client_default() {
        for s in specs() {
            if s.tool == HARNESS_TOOL {
                assert_eq!(s.timeout, Some(60));
            } else if ["start", "stop", "restart"].contains(&s.verb) {
                assert!(s.timeout.unwrap_or(0) >= 60, "{}", s.verb);
            }
        }
    }

    #[tokio::test]
    async fn the_spool_is_resent_after_ready_and_boot_relinks_and_seeds() {
        let dir = tempfile::tempdir().unwrap();
        let port = stub::dead_port().await;
        let paths = Paths::under(dir.path(), port);
        std::fs::create_dir_all(&paths.state_dir).unwrap();
        std::fs::write(paths.spool(), "{\"session_id\":\"ds00\",\"kind\":\"done\",\"ticket\":\"t\"}\n").unwrap();
        let (reporter, seen) = recording(paths.spool(), true);
        let app = Arc::new(App {
            sup: Supervisor::new(paths.clone()),
            sessions: Sessions { fork: fork::Fork::new(port), procs: Box::new(FakeProcs::default()), reporter, lifecycle: Lifecycle::new().0, compact_wait: COMPACT_WAIT, poll: POLL },
            settings: Settings::new(paths.settings_file()),
            hub: awm_plugin_sdk::Hub::new(&format!("http://127.0.0.1:{port}"), None),
            workspace: paths.workspace.clone(),
        });
        app.boot().await;
        assert!(paths.settings_file().is_file(), "boot seeds the route");
        assert_eq!(app.after_ready().await, 1);
        assert_eq!(seen.lock().unwrap()[0]["ticket"], "t");
        assert!(!paths.spool().exists());
    }
}
