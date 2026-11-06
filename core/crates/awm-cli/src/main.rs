use clap::Parser;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_env("AWM_LOG").unwrap_or_else(|_| "info".into()))
        .with_writer(std::io::stderr)
        .init();
    let cli = awm_cli::Cli::parse();
    let code = match awm_cli::run(cli).await {
        Ok(code) => code,
        Err(e) => {
            eprintln!("awm: {e:#}");
            1
        }
    };
    std::process::exit(code);
}
