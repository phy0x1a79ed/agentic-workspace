//! The fileviewer service: the mount, and its one verb `status`.

use std::sync::Arc;

use awm_plugin_fileviewer::server::{router, Config};
use awm_plugin_sdk::{Call, Service, VerbError};
use awm_statecheck::cli::StateCmd;
use awm_statecheck::decl::Decl;
use clap::{Parser, Subcommand};
use serde_json::json;

#[derive(Parser)]
#[command(name = "fileviewer", about = "Serve a file by its absolute path behind the gateway.")]
struct Cli {
    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run the fileviewer service (the default).
    Serve,
    /// The pre-redux state check. fileviewer keeps no state.
    State {
        #[command(subcommand)]
        cmd: StateCmd,
    },
}

const STATUS: &str = "Report the fileviewer mount: its origin-relative prefix (/s/fileviewer), the filesystem root it \
exposes, whether the listener is up, and how many mask (deny) globs are hiding secrets. The URL shape is \
/s/fileviewer/<absolute-path> — an origin-relative link that the gateway serves with a correct Content-Type through \
the HTTPS front, so it renders on any device viewing the notes page. Masked or missing paths 404.";

fn env(name: &str) -> Option<String> {
    std::env::var(name).ok()
}

/// `FILEVIEWER_PORT`, else a free loopback port, so the status can name the listener.
fn port() -> anyhow::Result<u16> {
    match env("FILEVIEWER_PORT").map(|p| p.trim().to_string()).filter(|p| !p.is_empty()) {
        Some(p) => Ok(p.parse()?),
        None => Ok(std::net::TcpListener::bind(("127.0.0.1", 0))?.local_addr()?.port()),
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    if let Some(Cmd::State { cmd }) = Cli::parse().cmd {
        let awm_dir = awm_plugin_sdk::config::awm_dir();
        let decl = Decl::parse(include_str!("../state.toml"), &awm_dir.join("services/fileviewer"), &awm_dir, env)?;
        std::process::exit(awm_statecheck::cli::run(&cmd, "fileviewer", &decl)?);
    }
    let svc = Service::from_env()?;
    let port = port()?;
    let root = env("FILEVIEWER_MOUNT_ROOT").unwrap_or_else(|| "/".into());
    let cfg = Arc::new(Config::new(&svc.context().env().service, &root, env("FILEVIEWER_MASK_FILE").as_deref(), format!("http://127.0.0.1:{port}")));
    tracing::info!("fileviewer listening on {}: {} → {} ({} deny globs)", cfg.listen, cfg.prefix, cfg.root_raw, cfg.mask.globs.len());
    let status = cfg.clone();
    svc.verb("fileviewer", "status", STATUS, json!([]), move |_: Call| {
        let status = status.status();
        async move { Ok::<_, VerbError>(status) }
    })
    .mount_on(port, router(cfg))
    .run()
    .await
}
