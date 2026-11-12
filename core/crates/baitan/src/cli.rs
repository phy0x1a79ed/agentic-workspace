//! The `baitan` command line: roles plus one subcommand per caller verb.

use crate::{roles, surfaces};
use baitan_proto::{Msg, WatchArgs};
use clap::{Arg, ArgMatches, Command};
use std::path::PathBuf;
use std::sync::Arc;

pub fn command() -> Command {
    let config = || Arg::new("config").long("config").value_parser(clap::value_parser!(PathBuf));
    let roles = Command::new("baitan")
        .about("baitan (摆摊): a compute market. Caller verbs talk to the market at BAITAN_CONNECT or ~/.baitan/market.sock.")
        .subcommand_required(true)
        .subcommand(Command::new("serve").about("Run the market: listen on the socket and dial the links in ~/.baitan/baitan.toml."))
        .subcommand(Command::new("provider").about("Run a provider over stdio (~/.baitan/provider.toml).").arg(config()))
        .subcommand(
            Command::new("front")
                .about("Run a Slurm front over stdio (~/.baitan/front.toml).")
                .arg(config())
                .arg(Arg::new("cluster").long("cluster").help("Register only this cluster")),
        )
        .subcommand(
            Command::new("connect")
                .about("Bridge stdio to the market socket.")
                .arg(Arg::new("socket").long("socket").value_parser(clap::value_parser!(PathBuf))),
        )
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
        "serve" => roles::serve(baitan_market::Market::new("market"), roles::MarketConfig::load()?).await,
        "provider" => roles::provider(sub.get_one::<PathBuf>("config").cloned()).await,
        "front" => roles::front(sub.get_one::<PathBuf>("config").cloned(), sub.get_one::<String>("cluster").cloned()).await,
        "connect" => roles::connect(sub.get_one::<PathBuf>("socket").cloned().unwrap_or_else(roles::default_socket)).await,
        "mcp" => surfaces::mcp_stdio(Arc::new(roles::client(&caller_name()).await?)).await,
        "http" => {
            let client = Arc::new(roles::client(&caller_name()).await?);
            let listener = tokio::net::TcpListener::bind(sub.get_one::<String>("addr").unwrap()).await?;
            Ok(axum::serve(listener, surfaces::http_router(client)).await?)
        }
        "watch" => watch(sub).await,
        verb => {
            let client = roles::client(&caller_name()).await?;
            match surfaces::invoke(&client, verb, surfaces::clap_args(verb, sub)?).await {
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
