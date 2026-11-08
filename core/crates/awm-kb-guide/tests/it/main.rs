//! guide follows the plugin set: install, update, disable, re-enable and uninstall, each checked
//! with a query; a reconcile after a dropped socket; and a peer's veil. All docs are synthetic.

use awm_embed::{Embedder, Stub};
use awm_index::grammar::{parse_source, parse_text};
use awm_index::Veil;
use awm_kb_guide::{Guide, GuideHub};
use awm_kb_source::{Ask, Source};
use futures_util::future::BoxFuture;
use serde_json::{json, Value};
use std::collections::{BTreeSet, HashMap};
use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex};

#[derive(Default)]
struct FakeHub {
    snapshot: Mutex<Value>,
    tools: Mutex<HashMap<String, Value>>,
    down: Mutex<bool>,
}

impl GuideHub for FakeHub {
    fn plugins(&self) -> BoxFuture<'_, Result<Value, String>> {
        Box::pin(async move {
            if *self.down.lock().unwrap() {
                return Err("the gateway is down".into());
            }
            Ok(self.snapshot.lock().unwrap().clone())
        })
    }
    fn describe<'a>(&'a self, tool: &'a str) -> BoxFuture<'a, Result<Value, String>> {
        Box::pin(async move { self.tools.lock().unwrap().get(tool).cloned().ok_or_else(|| format!("{tool}: not up")) })
    }
}

struct Rig {
    dir: tempfile::TempDir,
    hub: Arc<FakeHub>,
    stub: Arc<Stub>,
    guide: Arc<Guide>,
}

impl Rig {
    fn ws(&self) -> PathBuf {
        self.dir.path().join("ws")
    }

    fn plugin_dir(&self, name: &str) -> PathBuf {
        self.ws().join("plugins").join(name)
    }

    fn write(&self, path: &Path, text: &str) {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, text).unwrap();
    }

    /// The loader's snapshot: these plugins loaded, those skipped.
    fn set(&self, loaded: &[&str], skipped: &[&str]) {
        let plugins: Vec<Value> = loaded
            .iter()
            .map(|n| json!({"name": n, "dir": self.plugin_dir(n), "tools": [{"name": format!("{n}tool")}]}))
            .collect();
        *self.hub.snapshot.lock().unwrap() = json!({"plugins": plugins, "skipped": skipped, "refused": [], "archives": []});
    }

    async fn ask(&self, text: Value, filter: Value, veil: Veil) -> Value {
        let text = parse_text(&text).unwrap();
        let fields = awm_kb_guide::fields();
        let source = parse_source(&filter, &fields, "guide", "query.guide").unwrap();
        let vector = text.about.as_ref().map(|a| self.stub.encode(&[a.as_str()]).unwrap().remove(0));
        self.guide.query(Ask { text: Some(text), source, max: 20, vector, veil, session: None }).await.unwrap()
    }

    async fn found(&self, phrase: &str) -> BTreeSet<String> {
        let out = self.ask(json!({"$contains": phrase}), json!({}), Veil::None).await;
        assert_eq!(out["veiled"], true);
        out["exact"].as_array().unwrap().iter().map(|h| h["id"].as_str().unwrap().to_string()).collect()
    }
}

async fn rig() -> Rig {
    let dir = tempfile::tempdir().unwrap();
    let hub = Arc::new(FakeHub::default());
    let stub = Arc::new(Stub::default());
    let ws = dir.path().join("ws");
    std::fs::create_dir_all(ws.join("docs/deep")).unwrap();
    std::fs::write(ws.join("AGENTS.md"), "# Workspace\n\nThe startup ritual reads the quokka inbox.\n\n## Git\n\nLand with the heron script.\n").unwrap();
    std::fs::write(ws.join("docs/deep/notes.md"), "# Notes\n\n```sh\n# not a heading\n```\n\nThe wombat deploy waits for the gate.\n").unwrap();
    std::fs::write(ws.join("docs/skip.txt"), "the wombat text file is not markdown").unwrap();
    let emb: Arc<dyn Embedder> = stub.clone();
    let guide = Guide::open(&dir.path().join("awm"), &ws, Some(emb), hub.clone()).await.unwrap();
    let r = Rig { dir, hub, stub, guide };
    r.set(&[], &[]);
    r
}

fn ids(v: &[&str]) -> BTreeSet<String> {
    v.iter().map(|s| s.to_string()).collect()
}

const README_A: &str = "# Alpha\n\nAlpha keeps the zebra ledger.\n\n## Setup\n\nRun the zebra install twice.\n";

#[tokio::test]
async fn install_update_disable_enable_and_uninstall() {
    let r = rig().await;
    r.guide.reconcile().await.unwrap();
    let a = r.plugin_dir("alpha");
    r.write(&a.join("README.md"), README_A);
    r.write(&a.join("INSTALL.md"), "# Install\n\nAlpha needs the yak key.\n");
    r.write(&a.join("skills/ledger/SKILL.md"), "# Ledger skill\n\nBalance the zebra ledger nightly.\n");
    r.hub.tools.lock().unwrap().insert("alphatool".into(), json!({"plugin": "alpha", "tool": "alphatool", "verbs": [
        {"verb": "count", "description": "Count the zebras.\nSecond line.", "params": [{"name": "herd", "required": true, "description": "Which herd."}, {"name": "max", "type": "integer"}]},
    ]}));

    // Install.
    r.set(&["alpha"], &[]);
    let changed = r.guide.apply(&json!({"plugin": "alpha", "event": "installed", "version": "1", "root": r.ws().join("plugins")})).await.unwrap();
    assert_eq!(changed.indexed, ["docs:alpha", "tools:alpha"]);
    assert_eq!(r.found("zebra ledger").await, ids(&["doc:alpha/plugins/alpha/README.md#alpha", "doc:alpha/plugins/alpha/skills/ledger/SKILL.md#ledger-skill"]));
    assert_eq!(r.found("yak key").await, ids(&["doc:alpha/plugins/alpha/INSTALL.md#install"]));
    assert_eq!(r.found("count the zebras").await, ids(&["tool:alphatool.count"]));
    let tool = r.guide.get("tool:alphatool.count", &Veil::None, None).await.unwrap().unwrap();
    assert_eq!(tool["title"], "alphatool count: Count the zebras.");
    assert_eq!(tool["text"], "Count the zebras.\nSecond line.\n\nParams:\n- herd (required): Which herd.\n- max (integer)");

    // Update: the old text goes, the new text answers, an unchanged part keeps its vectors.
    r.write(&a.join("README.md"), "# Alpha\n\nAlpha keeps the giraffe ledger.\n\n## Setup\n\nRun the zebra install twice.\n");
    let before = r.stub.encoded.load(Ordering::SeqCst);
    r.guide.apply(&json!({"plugin": "alpha", "event": "updated"})).await.unwrap();
    assert_eq!(r.found("zebra ledger").await, ids(&["doc:alpha/plugins/alpha/skills/ledger/SKILL.md#ledger-skill"]));
    assert_eq!(r.found("giraffe ledger").await, ids(&["doc:alpha/plugins/alpha/README.md#alpha"]));
    assert_eq!(r.stub.encoded.load(Ordering::SeqCst) - before, 1, "only the changed part is embedded");

    // Disable masks: stored, unanswered, and not even by id.
    let stored = r.guide.status().await["stored"].clone();
    r.set(&[], &["alpha"]);
    let changed = r.guide.apply(&json!({"plugin": "alpha", "event": "disabled"})).await.unwrap();
    assert_eq!(changed.masked, ["alpha"]);
    assert!(r.found("giraffe ledger").await.is_empty() && r.found("count the zebras").await.is_empty());
    assert!(r.guide.get("tool:alphatool.count", &Veil::None, None).await.unwrap().is_none());
    let status = r.guide.status().await;
    assert_eq!((status["stored"].clone(), status["masked"].clone()), (stored, json!(["alpha"])));
    assert!(status["count"].as_u64() < status["stored"].as_u64());

    // Re-enable unmasks with no new embedding.
    let before = r.stub.encoded.load(Ordering::SeqCst);
    r.set(&["alpha"], &[]);
    let changed = r.guide.apply(&json!({"plugin": "alpha", "event": "enabled"})).await.unwrap();
    assert_eq!((changed.unmasked, changed.indexed), (vec!["alpha".to_string()], Vec::<String>::new()));
    assert_eq!(r.found("giraffe ledger").await.len(), 1);
    assert_eq!(r.stub.encoded.load(Ordering::SeqCst), before);

    // Uninstall deletes.
    r.set(&[], &[]);
    let changed = r.guide.apply(&json!({"plugin": "alpha", "event": "removed"})).await.unwrap();
    assert_eq!(changed.deleted, ["docs:alpha", "tools:alpha"]);
    assert!(r.found("giraffe ledger").await.is_empty() && r.found("yak key").await.is_empty());
    let status = r.guide.status().await;
    assert_eq!(status["stored"], status["count"]);
    assert_eq!(r.found("quokka inbox").await.len(), 1, "the workspace docs stay");
}

#[tokio::test]
async fn a_reconcile_catches_what_a_dropped_socket_missed() {
    let r = rig().await;
    for n in ["alpha", "beta", "gamma"] {
        r.write(&r.plugin_dir(n).join("README.md"), &format!("# {n}\n\nThe {n} plugin tends the kiwi orchard.\n"));
    }
    r.set(&["alpha", "beta", "gamma"], &[]);
    r.guide.reconcile().await.unwrap();
    assert_eq!(r.found("kiwi orchard").await.len(), 3);
    // While no socket was open: beta was disabled, gamma removed, alpha updated.
    r.write(&r.plugin_dir("alpha").join("README.md"), "# alpha\n\nThe alpha plugin tends the mango orchard.\n");
    r.set(&["alpha"], &["beta"]);
    let changed = r.guide.reconcile().await.unwrap();
    assert_eq!((changed.indexed, changed.masked, changed.deleted), (vec!["docs:alpha".to_string()], vec!["beta".to_string()], vec!["docs:gamma".to_string()]));
    assert!(r.found("kiwi orchard").await.is_empty());
    assert_eq!(r.found("mango orchard").await.len(), 1);
    // The state survives a restart.
    let emb: Arc<dyn Embedder> = r.stub.clone();
    let again = Guide::open(&r.dir.path().join("awm"), &r.ws(), Some(emb), r.hub.clone()).await.unwrap();
    assert_eq!(again.status().await["masked"], json!(["beta"]));
    assert_eq!(again.reconcile().await.unwrap(), awm_kb_guide::Changed::default());
}

#[tokio::test]
async fn a_peer_query_is_veiled_and_finds_no_singleton() {
    let r = rig().await;
    std::fs::write(
        r.dir.path().join("awm").join("edges.yml"),
        "nodes: {local: {role: homebase}, hub: {role: homebase}}\nedges: {}\nsingleton_doors: {hub: {port: 9000, network: lan}}\nsingletons: [{tool: alphatool, host: hub, callers: []}]\n",
    )
    .unwrap();
    std::env::set_var("AWM_EDGES_FILE", r.dir.path().join("awm").join("edges.yml"));
    r.hub.tools.lock().unwrap().insert("alphatool".into(), json!({"verbs": [{"verb": "graze", "description": "Graze the yaks."}]}));
    r.write(&r.plugin_dir("alpha").join("README.md"), "# Alpha\n\nAlpha grazes yaks too.\n");
    r.set(&["alpha"], &[]);
    r.guide.reconcile().await.unwrap();
    let local = r.ask(json!({"$contains": "yaks"}), json!({}), Veil::None).await;
    assert_eq!(local["exact"].as_array().unwrap().len(), 2);
    let peer = r.ask(json!({"$contains": "yaks"}), json!({"plugin": "alpha"}), Veil::Marks { marks: vec![], tags: None }).await;
    assert_eq!(peer["veiled"], true);
    assert_eq!(peer["exact"].as_array().unwrap().iter().map(|h| h["id"].as_str().unwrap()).collect::<Vec<_>>(), ["doc:alpha/plugins/alpha/README.md#alpha"]);
    assert!(r.guide.get("tool:alphatool.graze", &Veil::Unreadable, None).await.unwrap().is_none());
    std::env::remove_var("AWM_EDGES_FILE");
}
