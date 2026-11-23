//! The `outsource` command line: roles plus one subcommand per caller verb.

use crate::{roles, surfaces};
use outsource_proto::{Msg, WatchArgs};
use clap::{Arg, ArgMatches, Command};
use std::sync::Arc;

pub fn command() -> Command {
    let roles = Command::new("outsource")
        .about("outsource: a compute market. Caller verbs talk to this node's entry at ~/.outsource/market.sock.")
        .subcommand_required(true)
        .subcommand(Command::new("serve").about(
            "Run this node's service: its providers (~/.outsource/provider.toml) on ~/.outsource/provider.sock, and its entry, which dials the links in ~/.outsource/outsource.toml.",
        ))
        .subcommand(Command::new("attach").about("Bridge stdio to this node's providers, as a link for another node's entry."))
        .subcommand(Command::new("connect").about("Bridge stdio to this node's entry, for a Nextflow plugin."))
        .subcommand(Command::new("mcp").about("Serve the caller verbs over MCP on stdio (no watch)."))
        .subcommand(Command::new("http").about("Serve POST /invoke (no watch).").arg(Arg::new("addr").long("addr").default_value("127.0.0.1:7890")));
    surfaces::clap_verbs(roles)
}

fn caller_name() -> String {
    format!("cli-{}", std::process::id())
}

pub async fn run(m: ArgMatches) -> anyhow::Result<()> {
    let (name, sub) = m.subcommand().expect("subcommand required");
    // A dialled role's stderr reaches the market host's log; stdout carries the link.
    tracing_subscriber::fmt().with_writer(std::io::stderr).with_ansi(false).init();
    match name {
        "serve" => {
            let cfg = roles::MarketConfig::load()?;
            roles::serve(outsource_market::Market::new(&roles::node()), cfg.socket.unwrap_or_else(roles::default_socket), cfg.links).await
        }
        "attach" => roles::bridge(roles::provider_socket()).await,
        "connect" => roles::bridge(roles::default_socket()).await,
        "mcp" => surfaces::mcp_stdio(Arc::new(roles::client(&caller_name()).await?)).await,
        "http" => {
            let client = Arc::new(roles::client(&caller_name()).await?);
            let listener = tokio::net::TcpListener::bind(sub.get_one::<String>("addr").unwrap()).await?;
            Ok(axum::serve(listener, surfaces::http_router(client)).await?)
        }
        "watch" => watch(sub).await,
        verb => {
            let client = roles::client(&caller_name()).await?;
            match surfaces::invoke(&client, verb, surfaces::clap_args(verb, sub)?, &crate::offer::Site::here()).await {
                Ok(v) => Ok(println!("{}", serde_json::to_string_pretty(&v)?)),
                Err(e) => {
                    eprintln!("{e}");
                    std::process::exit(1)
                }
            }
        }
    }
}

/// Prints one event per line until the link closes.
async fn watch(sub: &ArgMatches) -> anyhow::Result<()> {
    let client = roles::client(&caller_name()).await?;
    let mut events = client.events().expect("fresh client");
    let args: WatchArgs = serde_json::from_value(surfaces::clap_args("watch", sub)?)?;
    if let Ok(Msg::Return(_)) = client.ask(Msg::Watch(args)).await {
        while let Some(Msg::Event(ev)) = events.recv().await {
            println!("{}", serde_json::to_string(&ev)?);
        }
    }
    Ok(())
}
