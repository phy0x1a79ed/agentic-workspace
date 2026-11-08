//! The dev plugin: drives a worktree's dev sandbox (`sandbox/run.sh`) for the caller that names
//! the worktree, and serves that script its two gateway calls.

mod lifecycle;
mod sandbox;

use std::path::PathBuf;
use std::time::Duration;

use awm_plugin_sdk::{Call, Service, VerbError};
use clap::{Parser, Subcommand};
use serde_json::{json, Value};

#[derive(Parser)]
#[command(name = "dev", about = "Drive a worktree's dev sandbox.")]
struct Cli {
    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run the dev service (the default).
    Serve,
    /// What sandbox/run.sh calls on its own gateway.
    Sandbox {
        #[command(subcommand)]
        cmd: SandboxCmd,
    },
}

#[derive(Subcommand)]
enum SandboxCmd {
    /// Create the demo scopes and channel posts. Needs AWM_WORKSPACE set to the sandbox folder.
    Seed {
        #[arg(long, env = "AWM_PORT", default_value_t = 7821)]
        port: u16,
        /// The sandbox folder AWM_WORKSPACE must name.
        #[arg(long)]
        sandbox: PathBuf,
    },
    /// Print `<name> <status> <pid>` per service the sandbox gateway runs.
    Services {
        #[arg(long, env = "AWM_PORT", default_value_t = 7821)]
        port: u16,
    },
}

const DESCRIPTIONS: [(&str, &str); 5] = [
    ("start", "Start the worktree's dev sandbox: its Rust gateway on the worktree's port band, which starts the worktree's services."),
    ("stop", "Stop the worktree's dev sandbox (gateway and services)."),
    ("restart", "Restart the worktree's dev sandbox."),
    ("seed", "(Re)seed the worktree's dev sandbox without restarting."),
    ("status", "Show what the worktree's dev sandbox is running."),
];

fn worktree_param() -> Value {
    json!([{"name": "worktree", "type": "string", "required": true,
            "description": "Absolute path of the worktree whose sandbox to drive, under the workspace's projects/."}])
}

fn service(svc: Service) -> Service {
    DESCRIPTIONS.iter().fold(svc, |svc, (action, description)| {
        let action = *action;
        svc.verb("dev", action, description, worktree_param(), move |c: Call| async move {
            let worktree = c.args.get("worktree").and_then(Value::as_str).unwrap_or("").to_string();
            Ok::<Value, VerbError>(lifecycle::run(action, &worktree, &lifecycle::projects_dir(), &lifecycle::Bash).await)
        })
        .timeout(Duration::from_secs(120))
    })
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    match Cli::parse().cmd {
        None | Some(Cmd::Serve) => {
            awm_plugin_sdk::telemetry::init();
            service(Service::from_env()?).run().await
        }
        Some(Cmd::Sandbox { cmd: SandboxCmd::Seed { port, sandbox } }) => {
            let ws = std::env::var("AWM_WORKSPACE").ok();
            if let Err(e) = sandbox::check_sandbox(ws.as_deref(), &sandbox) {
                eprintln!("{e}");
                std::process::exit(1);
            }
            let gw = sandbox::Gateway::new(port, Duration::from_secs(30));
            if let Err(e) = sandbox::seed(&gw, &mut std::io::stdout()).await {
                eprintln!("{e}");
                std::process::exit(1);
            }
            Ok(())
        }
        Some(Cmd::Sandbox { cmd: SandboxCmd::Services { port } }) => {
            for line in sandbox::services(&sandbox::Gateway::new(port, Duration::from_secs(3))).await {
                println!("{line}");
            }
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The describe surface lists a param's type only when it is not a string.
    fn as_described(params: Value) -> Value {
        let mut params = params;
        for p in params.as_array_mut().unwrap() {
            if p["type"] == "string" {
                p.as_object_mut().unwrap().remove("type");
            }
        }
        params
    }

    #[test]
    fn describe_matches_the_baseline() {
        let base: Value = serde_json::from_str(include_str!("../../../../docs/plugin-redux/ports/baseline/dev.describe.json")).unwrap();
        let verbs = base["verbs"].as_array().unwrap();
        assert_eq!(verbs.len(), DESCRIPTIONS.len());
        for (name, description) in DESCRIPTIONS {
            let v = verbs.iter().find(|v| v["verb"] == name).unwrap();
            assert_eq!(v["description"], description);
            assert_eq!(v["params"], as_described(worktree_param()));
        }
    }
}
