//! The caller-facing offer: a transform-shaped job (`requires`, `produces`, `protocol`) compiled
//! into the wire `OfferSet` of one `task` step and, when anything comes back, one `place` step home;
//! `prep` compiles the same materials into one `prep` step. A material's id comes from its type
//! when it has one, else from its stat or git commit; content is never read.

use outsource_proto::{fold_type, instance_id, props, slot_id, Axis, Fact, OfferSet, PropSet, Resources, Step, TIME_AXIS};
use outsource_provider::{identify, q, Host};
use serde::Deserialize;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

/// A `requires` entry: a path, or `{path?, type?, name?}` with at least one of path and type.
#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub enum Req {
    Path(String),
    Material(ReqMaterial),
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReqMaterial {
    path: Option<String>,
    #[serde(rename = "type")]
    ty: Option<PropSet>,
    name: Option<String>,
}

/// A `produces` entry: a path copied home, or `{path, type}` kept at the provider under its type.
#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub enum Out {
    Path(String),
    Typed(OutTyped),
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OutTyped {
    path: String,
    #[serde(rename = "type")]
    ty: PropSet,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Transform {
    pub requires: Vec<Req>,
    pub produces: Vec<Out>,
    pub protocol: String,
    #[serde(default)]
    pub to: Option<String>,
    #[serde(default)]
    pub metrics: Option<Vec<String>>,
    #[serde(default)]
    pub resources: Option<Resources>,
    #[serde(default)]
    pub dest: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Prep {
    pub requires: Vec<Req>,
    pub to: String,
}

/// Where the caller stands: its node's provider id (the `origin` of every material) and the
/// directory its relative paths mean when known.
#[derive(Debug, Clone)]
pub struct Site {
    pub node: String,
    pub cwd: Option<PathBuf>,
}

impl Site {
    /// This node and this process's cwd.
    pub fn here() -> Site {
        Site { node: crate::roles::node(), cwd: std::env::current_dir().ok() }
    }

    fn path(&self, raw: &str, what: &str) -> Result<PathBuf, String> {
        match &self.cwd {
            Some(cwd) => Ok(cwd.join(raw)),
            None if Path::new(raw).is_absolute() => Ok(raw.into()),
            None => Err(format!("{what} {raw}: a call from {} must give an absolute path", self.node)),
        }
    }
}

#[derive(Debug)]
pub struct Compiled {
    pub set: OfferSet,
}

struct Material {
    name: String,
    path: Option<PathBuf>,
    ty: Option<PropSet>,
    id: String,
    file: bool,
    size: u64,
}

/// The materials of a request: no two share a name, and at most one is a `.env` file.
struct Materials {
    all: Vec<Material>,
    env: Option<Value>,
}

impl Materials {
    async fn gather(reqs: &[Req], site: &Site) -> Result<Materials, String> {
        let mut all: Vec<Material> = vec![];
        for req in reqs {
            let m = material(req, site).await?;
            if all.iter().any(|o| o.name == m.name) {
                return Err(format!("two requires share the name {}", m.name));
            }
            all.push(m);
        }
        let mut envs = all.iter().filter(|m| m.name.ends_with(".env") && m.file);
        let env = match (envs.next(), envs.next()) {
            (Some(_), Some(_)) => return Err("requires holds more than one .env file".into()),
            (Some(m), None) => Some(json!({"stage": m.name, "container": container_of(m.path.as_deref().expect("a file"), site).await?})),
            _ => None,
        };
        Ok(Materials { all, env })
    }

    fn stage(&self) -> Value {
        let item = |m: &Material| match &m.ty {
            Some(ty) => json!({"id": m.id, "type": ty}),
            None => json!({"id": m.id}),
        };
        Value::Object(self.all.iter().map(|m| (m.name.clone(), item(m))).collect())
    }

    fn inputs(&self) -> Vec<String> {
        let mut ids: Vec<String> = self.all.iter().map(|m| m.id.clone()).collect();
        ids.sort();
        ids.dedup();
        ids
    }

    /// The caller's own copies are the sources; a material with no path is found by the market.
    fn start(&self, site: &Site) -> Vec<Fact> {
        self.all.iter().filter_map(|m| Some(Fact::at_path(&m.id, &site.node, &m.path.as_ref()?.to_string_lossy()))).collect()
    }

    fn size(&self) -> u64 {
        self.all.iter().map(|m| m.size).sum()
    }
}

fn new_set(steps: Vec<Step>, mats: &Materials, site: &Site, to: Option<String>) -> OfferSet {
    let mut set: OfferSet = serde_json::from_value(json!({"steps": []})).expect("defaults");
    set.steps = steps;
    set.start = mats.start(site);
    set.size = Some(mats.size());
    set.to = to;
    set
}

pub async fn compile(args: Value, site: &Site) -> Result<Compiled, String> {
    let t: Transform = serde_json::from_value(args).map_err(|e| e.to_string())?;
    let mats = Materials::gather(&t.requires, site).await?;

    let (mut copied, mut kept) = (vec![], vec![]);
    for out in &t.produces {
        let (path, ty) = match out {
            Out::Path(p) => (p.trim_end_matches('/'), None),
            Out::Typed(o) => (o.path.trim_end_matches('/'), Some(fold_type(&o.ty))),
        };
        if path.is_empty() || path.starts_with('/') || Path::new(path).components().any(|c| c.as_os_str() == "..") {
            return Err(format!("produces path {path:?} must be relative to the work dir"));
        }
        match ty {
            Some(ty) if ty.is_empty() => return Err(format!("produces {path} has an empty type")),
            Some(ty) => kept.push(json!({"path": path, "type": ty})),
            None => copied.push(path.to_string()),
        }
    }
    let outputs: Vec<String> = copied.iter().flat_map(|p| [p.clone(), format!("{p}/**")]).collect();

    let names: Vec<&str> = mats.all.iter().map(|m| m.name.as_str()).collect();
    let mut identity = props([
        ("protocol", t.protocol.as_str()),
        ("produces", &copied.join("\n")),
        ("names", &names.join("\n")),
    ]);
    if !kept.is_empty() {
        identity.insert("typed".into(), Value::Array(kept.clone()).to_string());
    }
    let mut spec = json!({"script": t.protocol, "stage": mats.stage(), "outputs": outputs});
    if let Some(env) = mats.env.clone() {
        spec["env"] = env;
    }
    if !kept.is_empty() {
        spec["typed"] = Value::Array(kept);
    }
    let task = Step {
        op: "task".into(),
        props: props([("contract", "outsource"), ("job", &instance_id(&identity))]),
        inputs: mats.inputs(),
        slots: vec!["out".into()],
        spec,
        to: None,
    };

    let mut steps = vec![];
    if !copied.is_empty() {
        let dest = match &t.dest {
            Some(d) => site.path(d, "dest")?,
            None => site.cwd.clone().ok_or_else(|| format!("a call from {} must give an absolute dest for its produces", site.node))?,
        };
        steps.push(Step {
            op: "place".into(),
            props: PropSet::new(),
            inputs: vec![slot_id(&task.key(), "out")],
            slots: vec![],
            spec: json!({"path": dest.to_string_lossy(), "files": true}),
            to: Some(site.node.clone()),
        });
    }
    steps.insert(0, task);

    let axes = t.metrics.unwrap_or_else(|| vec![TIME_AXIS.into()]);
    let mut set = new_set(steps, &mats, site, t.to);
    set.axes = axes.into_iter().map(|name| Axis { name, weight: 1.0, higher_better: false }).collect();
    set.resources = t.resources.unwrap_or_default();
    Ok(Compiled { set })
}

/// A directed offer with no protocol: one `prep` step that stages the materials at `to`.
pub async fn compile_prep(args: Value, site: &Site) -> Result<Compiled, String> {
    let p: Prep = serde_json::from_value(args).map_err(|e| e.to_string())?;
    let mats = Materials::gather(&p.requires, site).await?;
    let mut spec = json!({"stage": mats.stage()});
    if let Some(env) = mats.env.clone() {
        spec["env"] = env;
    }
    let step = Step {
        op: "prep".into(),
        props: props([("contract", "outsource-prep")]),
        inputs: mats.inputs(),
        slots: vec![],
        spec,
        to: None,
    };
    Ok(Compiled { set: new_set(vec![step], &mats, site, Some(p.to)) })
}

async fn container_of(env: &Path, site: &Site) -> Result<String, String> {
    let text = Host::default().sh(&format!("cat {}", q(&env.to_string_lossy()))).await.map_err(|e| format!("{} on {}: {e}", env.display(), site.node))?;
    let yaml: serde_yaml::Value = serde_yaml::from_str(&text).map_err(|e| format!("{}: {e}", env.display()))?;
    yaml.get("container")
        .and_then(serde_yaml::Value::as_str)
        .map(String::from)
        .ok_or_else(|| format!("{} names no `container:`; only containers are supported", env.display()))
}

/// A typed material's id is its type's id, wherever it sits. Otherwise a file or a plain folder
/// is `{path, mtime_ns, origin}`, a plain folder `{path, tree-hash, origin}` over every file's name, size and mtime beneath it,
/// and a folder holding `.git` is `{git: HEAD, origin}` plus a hash of its dirty files when it has any.
async fn material(req: &Req, site: &Site) -> Result<Material, String> {
    let (raw, ty, name) = match req {
        Req::Path(p) => (Some(p.as_str()), None, None),
        Req::Material(m) => (m.path.as_deref(), m.ty.as_ref().map(fold_type), m.name.clone()),
    };
    if raw.is_none() && ty.is_none() {
        return Err("a requires entry needs a path or a type".into());
    }
    if ty.as_ref().is_some_and(PropSet::is_empty) {
        return Err("a requires type must name at least one property".into());
    }
    let probed = match raw {
        Some(raw) => {
            let path = site.path(raw, "requires")?;
            let probed = identify(&Host::default(), &path.to_string_lossy(), &site.node, true).await;
            Some(probed.map_err(|e| format!("requires {raw} on {}: {e}", site.node))?)
        }
        None => None,
    };
    let path = probed.as_ref().map(|p| PathBuf::from(&p.path));
    let base = path.as_ref().and_then(|p| p.file_name()).map(|n| n.to_string_lossy().into_owned());
    let by_type = || ty.iter().flat_map(|t| t.values().map(String::as_str)).collect::<Vec<_>>().join("-");
    let name = name.or(base).unwrap_or_else(by_type);
    if name.is_empty() || name.contains('/') {
        return Err(format!("requires name {name:?} is not a base name"));
    }
    let (file, size) = probed.as_ref().map_or((false, 0), |p| (p.file, p.size));
    let id = match (&ty, probed) {
        (Some(ty), _) => instance_id(ty),
        (None, Some(p)) => p.id,
        (None, None) => unreachable!("checked above"),
    };
    Ok(Material { name, path, ty, id, file, size })
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
        Site { node: "mine".into(), cwd: Some(dir.to_path_buf()) }
    }

    fn job(requires: &[&str], protocol: &str) -> Value {
        json!({"requires": requires, "produces": ["out/fit.rds", "plots/"], "protocol": protocol})
    }

    fn stage_id(c: &Compiled, name: &str) -> String {
        c.set.steps[0].spec["stage"][name]["id"].as_str().unwrap().to_string()
    }

    #[tokio::test]
    async fn shape_compiles_to_a_task_and_a_place_home() {
        let d = tempfile::tempdir().unwrap();
        let d = d.path().canonicalize().unwrap();
        std::fs::write(d.join("iphop.env"), "container: docker://x/iphop:1\nconda: iphop\n").unwrap();
        std::fs::write(d.join("counts.tsv"), "a").unwrap();
        std::fs::create_dir(d.join("plain")).unwrap();
        repo(&d, "analysis");
        let c = compile(job(&["iphop.env", "counts.tsv", "analysis", "plain"], "Rscript analysis/fit.R"), &site(&d)).await.unwrap();
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

        let two = {
            std::fs::write(d.join("b.env"), "container: docker://y\n").unwrap();
            compile(job(&["iphop.env", "b.env"], "x"), &site(&d)).await.unwrap_err()
        };
        assert!(two.contains("more than one .env"), "{two}");
        std::fs::write(d.join("conda.env"), "conda: only\n").unwrap();
        assert!(compile(job(&["conda.env"], "x"), &site(&d)).await.unwrap_err().contains("only containers"));
        assert!(compile(json!({"requires": [], "produces": ["/abs"], "protocol": "x"}), &site(&d)).await.is_err());
        assert!(compile(json!({"requires": [], "produces": [], "protocol": "x", "set": {}}), &site(&d)).await.is_err());
    }

    #[tokio::test]
    async fn identity_follows_stat_and_commit_never_content() {
        let d = tempfile::tempdir().unwrap();
        let d = d.path().canonicalize().unwrap();
        std::fs::write(d.join("counts.tsv"), "a").unwrap();
        let a = repo(&d, "a");
        macro_rules! compiled {
            ($p:expr) => { compile(job(&["counts.tsv", "a"], $p), &site(&d)).await.unwrap() };
        }
        let base = compiled!("p");
        let key = |c: &Compiled| c.set.steps[0].key();

        // A new mtime moves a file's id and the task's key.
        let file = std::fs::File::options().write(true).open(d.join("counts.tsv")).unwrap();
        file.set_modified(std::time::SystemTime::now() + std::time::Duration::from_secs(5)).unwrap();
        let touched = compiled!("p");
        assert_ne!(stage_id(&base, "counts.tsv"), stage_id(&touched, "counts.tsv"));
        assert_ne!(key(&base), key(&touched));
        assert_eq!(stage_id(&base, "a"), stage_id(&touched, "a"));

        // A folder is its whole tree: a file added in a nested subfolder moves its id.
        std::fs::create_dir_all(d.join("tree/x/y")).unwrap();
        let tree = compile(job(&["tree"], "p"), &site(&d)).await.unwrap();
        std::fs::write(d.join("tree/x/y/new"), "n").unwrap();
        assert_ne!(stage_id(&tree, "tree"), stage_id(&compile(job(&["tree"], "p"), &site(&d)).await.unwrap(), "tree"));

        // A repo is its commit: a clone elsewhere agrees, a new commit moves it, a dirty tree moves it with its dirty files.
        sh(&d, "git clone -q a b");
        assert_eq!(stage_id(&base, "a"), stage_id(&compile(job(&["b"], "p"), &site(&d)).await.unwrap(), "b"));
        std::fs::write(a.join("f"), "dirty").unwrap();
        let dirty = compiled!("p");
        assert_ne!(stage_id(&base, "a"), stage_id(&dirty, "a"));
        std::fs::write(a.join("f"), "dirtier").unwrap();
        assert_ne!(stage_id(&dirty, "a"), stage_id(&compiled!("p"), "a"));
        sh(&a, "git checkout -q f");
        assert_eq!(stage_id(&base, "a"), stage_id(&compiled!("p"), "a"));
        std::fs::write(a.join("f"), "dirty").unwrap();
        sh(&a, "git -c user.name=t -c user.email=t@t commit -qam c2");
        assert_ne!(stage_id(&base, "a"), stage_id(&compiled!("p"), "a"));

        // A type replaces stat and commit: one id at any path, and a type-only entry has no start fact.
        std::fs::create_dir(d.join("elsewhere")).unwrap();
        std::fs::write(d.join("elsewhere/other.tsv"), "bb").unwrap();
        let ty = json!({"db": "uniref50", "release": "2024_01"});
        macro_rules! typed {
            ($req:expr) => { compile(json!({"requires": [$req], "produces": [], "protocol": "p"}), &site(&d)).await.unwrap() };
        }
        let (here, there, alone) = (
            typed!(json!({"path": "counts.tsv", "type": ty})),
            typed!(json!({"path": "elsewhere/other.tsv", "type": ty})),
            typed!(json!({"type": ty, "name": "uniref50"})),
        );
        assert_eq!(stage_id(&here, "counts.tsv"), stage_id(&there, "other.tsv"));
        assert_eq!(stage_id(&here, "counts.tsv"), stage_id(&alone, "uniref50"));
        assert_eq!(here.set.steps[0].spec["stage"]["counts.tsv"]["type"], ty);
        assert_eq!((here.set.start.len(), alone.set.start.len()), (1, 0));
        assert_eq!(typed!(json!({"type": {"b": "2", "a": "1"}})).set.steps[0].spec["stage"]["1-2"]["id"], json!(instance_id(&props([("a", "1"), ("b", "2")]))));
        // Values fold to lowercase before the id is taken, in inputs and outputs alike.
        let upper = typed!(json!({"type": {"db": "UniRef50", "release": "2024_01"}, "name": "u"}));
        assert_eq!(stage_id(&upper, "u"), instance_id(&props([("db", "uniref50"), ("release", "2024_01")])));
        let out = compile(json!({"requires": [], "produces": [{"path": "o", "type": {"db": "NR"}}], "protocol": "p"}), &site(&d)).await.unwrap();
        assert_eq!(out.set.steps[0].spec["typed"][0]["type"], json!({"db": "nr"}));
        let empty = compile(json!({"requires": [{"name": "x"}], "produces": [], "protocol": "p"}), &site(&d)).await;
        assert!(empty.unwrap_err().contains("path or a type"));

        // The protocol is part of a result's identity.
        assert_ne!(base.set.steps[0].props["job"], compiled!("q").set.steps[0].props["job"]);
    }

    #[tokio::test]
    async fn directed_offers_typed_outputs_and_prep() {
        let d = tempfile::tempdir().unwrap();
        let d = d.path().canonicalize().unwrap();
        std::fs::write(d.join("e.env"), "container: docker://x/y:1\n").unwrap();
        let ty = json!({"db": "uniref50"});

        // A typed output stays at the provider: no place step home, the type rides in the spec.
        let dl = compile(json!({"requires": [], "produces": [{"path": "u.dmnd", "type": ty}], "protocol": "wget x", "to": "sockeye"}), &site(&d)).await.unwrap();
        assert_eq!(dl.set.to.as_deref(), Some("sockeye"));
        let [task] = &dl.set.steps[..] else { panic!("task only") };
        assert_eq!(task.spec["typed"], json!([{"path": "u.dmnd", "type": ty}]));
        assert_eq!(task.spec["outputs"], json!([]));
        let mixed = compile(json!({"requires": [], "produces": ["r.txt", {"path": "u.dmnd", "type": ty}], "protocol": "wget x"}), &site(&d)).await.unwrap();
        assert_eq!(mixed.set.steps.len(), 2);
        assert_eq!(mixed.set.steps[0].spec["outputs"], json!(["r.txt", "r.txt/**"]));
        assert!(mixed.set.to.is_none());
        assert_ne!(dl.set.steps[0].props["job"], compile(json!({"requires": [], "produces": [{"path": "u.dmnd", "type": {"db": "x"}}], "protocol": "wget x"}), &site(&d)).await.unwrap().set.steps[0].props["job"]);

        // Prep is one `prep` step with no protocol, directed, carrying the env.
        let prep = compile_prep(json!({"to": "sockeye", "requires": ["e.env", {"type": ty}]}), &site(&d)).await.unwrap();
        let [step] = &prep.set.steps[..] else { panic!("one step") };
        assert_eq!((step.op.as_str(), prep.set.to.as_deref()), ("prep", Some("sockeye")));
        assert_eq!(step.spec["env"], json!({"stage": "e.env", "container": "docker://x/y:1"}));
        assert_eq!(step.spec["stage"]["uniref50"]["id"], json!(instance_id(&props([("db", "uniref50")]))));
        assert!(compile_prep(json!({"requires": []}), &site(&d)).await.is_err());
    }
}
