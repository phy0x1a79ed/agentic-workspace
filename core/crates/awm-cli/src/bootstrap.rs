//! `awm bootstrap`: turn a state directory and a handed install config into a node's gateway home.
//! awm names no host (fleet.md R140): the install config comes from the node's installer.

use std::path::PathBuf;

#[derive(clap::Args, Debug)]
pub struct Args {
    /// The state directory (AWM_DIR) to prepare.
    #[arg(long, env = "AWM_DIR")]
    pub state: PathBuf,
    /// The install config the node's installer hands over. Copied to <state>/config.toml.
    #[arg(long)]
    pub config: Option<PathBuf>,
    /// The awm checkout whose core plugins and pages to load, when no config is handed over.
    #[arg(long)]
    pub awm_root: Option<PathBuf>,
    /// Replace an existing <state>/config.toml.
    #[arg(long)]
    pub force: bool,
}

pub fn run(args: Args) -> anyhow::Result<i32> {
    for sub in ["", "usage", "logs", "plugins"] {
        std::fs::create_dir_all(args.state.join(sub))?;
    }
    let target = args.state.join(awm_gateway::config::CONFIG_FILE);
    if target.exists() && !args.force {
        println!("kept {} (pass --force to replace it)", target.display());
    } else {
        let text = match (&args.config, &args.awm_root) {
            (Some(path), _) => std::fs::read_to_string(path)?,
            (None, Some(root)) => default_config(root)?,
            (None, None) => anyhow::bail!("pass --config <install config> or --awm-root <awm checkout>"),
        };
        // Refuse a config the gateway would refuse at start.
        toml::from_str::<awm_gateway::config::Config>(&text).map_err(|e| anyhow::anyhow!("install config: {e}"))?;
        std::fs::write(&target, text)?;
        println!("wrote {}", target.display());
    }
    println!("next:");
    println!("  awm gateway serve --state {}", args.state.display());
    println!("  claude mcp add awm -- awm mcp      (with AWM_DIR={} in its environment)", args.state.display());
    Ok(0)
}

fn default_config(root: &std::path::Path) -> anyhow::Result<String> {
    let root = root.canonicalize()?;
    // The core roots default to the tree's own under the workspace (`Config::core_root_dirs`).
    let cfg = toml::toml! {
        workspace = (root.display().to_string())
    };
    Ok(toml::to_string_pretty(&cfg)?)
}
