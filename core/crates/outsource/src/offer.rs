//! The caller-facing offer: a transform-shaped job (`requires`, `produces`, `protocol`) compiled
//! into the wire `OfferSet` of one `task` step and one `place` step back to the caller's node.
//! Identity never reads content: a material's id comes from its stat or its git commit.

use outsource_proto::{instance_id, props, slot_id, Axis, Fact, OfferSet, PropSet, Resources, Step, TIME_AXIS};
use serde::Deserialize;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Transform {
    pub requires: Vec<String>,
    pub produces: Vec<String>,
    pub protocol: String,
    #[serde(default)]
    pub metrics: Option<Vec<String>>,
    #[serde(default)]
    pub resources: Option<Resources>,
    #[serde(default)]
    pub dest: Option<String>,
}

/// Where the caller stands: its node's provider id (the `origin` of every material) and the
/// directory its relative paths mean.
#[derive(Debug, Clone)]
pub struct Site {
    pub node: String,
    pub cwd: PathBuf,
}

impl Site {
    /// `OUTSOURCE_NODE`, else the hostname a provider takes as its default id, and this process's cwd.
    pub fn here() -> Site {
        let node = std::env::var("OUTSOURCE_NODE").ok().filter(|n| !n.is_empty()).unwrap_or_else(crate::roles::hostname);
        Site { node, cwd: std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")) }
    }
}

#[derive(Debug)]
pub struct Compiled {
    pub set: OfferSet,
    pub warnings: Vec<String>,
}

struct Material {
    name: String,
    path: PathBuf,
    id: String,
}

pub fn compile(args: Value, site: &Site) -> Result<Compiled, String> {
    let t: Transform = serde_json::from_value(args).map_err(|e| e.to_string())?;
    let mut warnings = vec![];
    let mut mats: Vec<Material> = vec![];
    for raw in &t.requires {
        let m = material(raw, site, &mut warnings)?;
        if mats.iter().any(|o| o.name == m.name) {
            return Err(format!("two requires share the base name {}", m.name));
        }
        mats.push(m);
    }

    let mut envs = mats.iter().filter(|m| m.name.ends_with(".env") && m.path.is_file());
    let env = match (envs.next(), envs.next()) {
        (Some(_), Some(_)) => return Err("requires holds more than one .env file".into()),
        (Some(m), None) => Some(json!({"stage": m.name, "container": container_of(&m.path)?})),
        _ => None,
    };

    let produces: Vec<String> = t.produces.iter().map(|p| p.trim_end_matches('/').to_string()).collect();
    for p in &produces {
        if p.is_empty() || p.starts_with('/') || Path::new(p).components().any(|c| c.as_os_str() == "..") {
            return Err(format!("produces path {p:?} must be relative to the work dir"));
        }
    }
    let outputs: Vec<String> = produces.iter().flat_map(|p| [p.clone(), format!("{p}/**")]).collect();

    let names: Vec<&str> = mats.iter().map(|m| m.name.as_str()).collect();
    let job = instance_id(&props([
        ("protocol", t.protocol.as_str()),
        ("produces", &produces.join("\n")),
        ("names", &names.join("\n")),
    ]));
    let mut spec = json!({
        "script": t.protocol,
        "stage": mats.iter().map(|m| (m.name.clone(), json!({"id": m.id}))).collect::<serde_json::Map<_, _>>(),
        "outputs": outputs,
    });
    if let Some(env) = env {
        spec["env"] = env;
    }
    let mut inputs: Vec<String> = mats.iter().map(|m| m.id.clone()).collect();
    inputs.sort();
    inputs.dedup();
    let task = Step {
        op: "task".into(),
        props: props([("contract", "outsource"), ("job", &job)]),
        inputs,
        slots: vec!["out".into()],
        spec,
        to: None,
    };

    let dest = match &t.dest {
        Some(d) => site.cwd.join(d),
        None => site.cwd.clone(),
    };
    let place = Step {
        op: "place".into(),
        props: PropSet::new(),
        inputs: vec![slot_id(&task.key(), "out")],
        slots: vec![],
        spec: json!({"path": dest.to_string_lossy(), "files": true}),
        to: Some(site.node.clone()),
    };

    let axes = t.metrics.unwrap_or_else(|| vec![TIME_AXIS.into()]);
    let size: u64 = mats.iter().map(|m| outsource_provider::disk_size(&m.path)).sum();
    let mut set: OfferSet = serde_json::from_value(json!({"steps": []})).expect("defaults");
    set.steps = vec![task, place];
    set.start = mats.iter().map(|m| Fact::at_path(&m.id, &site.node, &m.path.to_string_lossy())).collect();
    set.axes = axes.into_iter().map(|name| Axis { name, weight: 1.0, higher_better: false }).collect();
    set.resources = t.resources.unwrap_or_default();
    set.size = Some(size);
    Ok(Compiled { set, warnings })
}

fn container_of(env: &Path) -> Result<String, String> {
    let text = std::fs::read_to_string(env).map_err(|e| format!("{}: {e}", env.display()))?;
    let yaml: serde_yaml::Value = serde_yaml::from_str(&text).map_err(|e| format!("{}: {e}", env.display()))?;
    yaml.get("container")
        .and_then(serde_yaml::Value::as_str)
        .map(String::from)
        .ok_or_else(|| format!("{} names no `container:`; only containers are supported", env.display()))
}

/// A file or a plain folder is `{path, mtime_ns, origin}`; a folder holding `.git` is
/// `{git: HEAD, origin}`, with a warning when it has uncommitted changes.
fn material(raw: &str, site: &Site, warnings: &mut Vec<String>) -> Result<Material, String> {
    let path = std::fs::canonicalize(site.cwd.join(raw)).map_err(|e| format!("requires {raw}: {e}"))?;
    let name = path.file_name().map(|n| n.to_string_lossy().into_owned()).ok_or_else(|| format!("requires {raw} has no base name"))?;
    let shown = path.to_string_lossy();
    let id = if path.join(".git").exists() {
        let head = git(&path, &["rev-parse", "HEAD"])?;
        if !git(&path, &["status", "--porcelain"])?.is_empty() {
            warnings.push(format!("repo {shown} has uncommitted changes; a prior cached result may be reused"));
        }
        instance_id(&props([("git", head.as_str()), ("origin", &site.node)]))
    } else {
        use std::os::unix::fs::MetadataExt;
        let md = std::fs::metadata(&path).map_err(|e| format!("requires {raw}: {e}"))?;
        let mtime_ns = (md.mtime() as i128 * 1_000_000_000 + md.mtime_nsec() as i128).to_string();
        instance_id(&props([("path", &*shown), ("mtime_ns", &mtime_ns), ("origin", &site.node)]))
    };
    Ok(Material { name, path, id })
}

fn git(dir: &Path, args: &[&str]) -> Result<String, String> {
    let out = std::process::Command::new("git").arg("-C").arg(dir).args(args).output().map_err(|e| format!("git: {e}"))?;
    if !out.status.success() {
        return Err(format!("git {} in {}: {}", args.join(" "), dir.display(), String::from_utf8_lossy(&out.stderr).trim()));
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sh(dir: &Path, cmd: &str) {
        assert!(std::process::Command::new("sh").arg("-c").arg(cmd).current_dir(dir).status().unwrap().success(), "{cmd}");
    }

    fn repo(dir: &Path, name: &str) -> PathBuf {
        let r = dir.join(name);
        std::fs::create_dir(&r).unwrap();
        sh(&r, "git init -q && echo x > f && git add f && git -c user.name=t -c user.email=t@t commit -qm c");
        r
    }

    fn site(dir: &Path) -> Site {
        Site { node: "mine".into(), cwd: dir.to_path_buf() }
    }

    fn job(requires: &[&str], protocol: &str) -> Value {
        json!({"requires": requires, "produces": ["out/fit.rds", "plots/"], "protocol": protocol})
    }

    fn stage_id(c: &Compiled, name: &str) -> String {
        c.set.steps[0].spec["stage"][name]["id"].as_str().unwrap().to_string()
    }

    #[test]
    fn shape_compiles_to_a_task_and_a_place_home() {
        let d = tempfile::tempdir().unwrap();
        let d = d.path().canonicalize().unwrap();
        std::fs::write(d.join("iphop.env"), "container: docker://x/iphop:1\nconda: iphop\n").unwrap();
        std::fs::write(d.join("counts.tsv"), "a").unwrap();
        std::fs::create_dir(d.join("plain")).unwrap();
        repo(&d, "analysis");
        let c = compile(job(&["iphop.env", "counts.tsv", "analysis", "plain"], "Rscript analysis/fit.R"), &site(&d)).unwrap();
        let [task, place] = &c.set.steps[..] else { panic!("two steps") };
        assert_eq!((task.op.as_str(), place.op.as_str()), ("task", "place"));
        assert_eq!(task.spec["script"], "Rscript analysis/fit.R");
        assert_eq!(task.spec["env"], json!({"stage": "iphop.env", "container": "docker://x/iphop:1"}));
        assert_eq!(task.spec["outputs"], json!(["out/fit.rds", "out/fit.rds/**", "plots", "plots/**"]));
        assert_eq!(task.slots, ["out"]);
        assert_eq!(task.inputs.len(), 4);
        assert_eq!((place.to.as_deref(), &place.spec), (Some("mine"), &json!({"path": d.to_string_lossy(), "files": true})));
        assert_eq!(place.inputs, [slot_id(&task.key(), "out")]);
        assert_eq!(c.set.start.len(), 4);
        let counts = d.join("counts.tsv");
        assert!(c.set.start.contains(&Fact::at_path(&stage_id(&c, "counts.tsv"), "mine", &counts.to_string_lossy())));
        assert!(c.warnings.is_empty());

        let two = {
            std::fs::write(d.join("b.env"), "container: docker://y\n").unwrap();
            compile(job(&["iphop.env", "b.env"], "x"), &site(&d)).unwrap_err()
        };
        assert!(two.contains("more than one .env"), "{two}");
        std::fs::write(d.join("conda.env"), "conda: only\n").unwrap();
        assert!(compile(job(&["conda.env"], "x"), &site(&d)).unwrap_err().contains("only containers"));
        assert!(compile(json!({"requires": [], "produces": ["/abs"], "protocol": "x"}), &site(&d)).is_err());
        assert!(compile(json!({"requires": [], "produces": [], "protocol": "x", "set": {}}), &site(&d)).is_err());
    }

    #[test]
    fn identity_follows_stat_and_commit_never_content() {
        let d = tempfile::tempdir().unwrap();
        let d = d.path().canonicalize().unwrap();
        std::fs::write(d.join("counts.tsv"), "a").unwrap();
        let a = repo(&d, "a");
        let compiled = |protocol: &str| compile(job(&["counts.tsv", "a"], protocol), &site(&d)).unwrap();
        let base = compiled("p");
        let key = |c: &Compiled| c.set.steps[0].key();

        // A new mtime moves a file's id and the task's key.
        let file = std::fs::File::options().write(true).open(d.join("counts.tsv")).unwrap();
        file.set_modified(std::time::SystemTime::now() + std::time::Duration::from_secs(5)).unwrap();
        let touched = compiled("p");
        assert_ne!(stage_id(&base, "counts.tsv"), stage_id(&touched, "counts.tsv"));
        assert_ne!(key(&base), key(&touched));
        assert_eq!(stage_id(&base, "a"), stage_id(&touched, "a"));

        // A repo is its commit: a clone elsewhere agrees, a new commit moves it, a dirty tree only warns.
        sh(&d, "git clone -q a b");
        assert_eq!(stage_id(&base, "a"), stage_id(&compile(job(&["b"], "p"), &site(&d)).unwrap(), "b"));
        std::fs::write(a.join("f"), "dirty").unwrap();
        let dirty = compiled("p");
        assert_eq!(stage_id(&base, "a"), stage_id(&dirty, "a"));
        assert_eq!(dirty.warnings, [format!("repo {} has uncommitted changes; a prior cached result may be reused", a.display())]);
        sh(&a, "git -c user.name=t -c user.email=t@t commit -qam c2");
        assert_ne!(stage_id(&base, "a"), stage_id(&compiled("p"), "a"));

        // The protocol is part of a result's identity.
        assert_ne!(base.set.steps[0].props["job"], compiled("q").set.steps[0].props["job"]);
    }
}
