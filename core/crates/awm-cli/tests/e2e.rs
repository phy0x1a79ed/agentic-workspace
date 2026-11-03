//! The gateway end to end: `awm gateway serve` in a temp dir loads a fake plugin, a third-party
//! plugin that needs it, and (when the awm conda env is present) today's Python journal service
//! unchanged. Calls go in through `awm mcp` and the `awm <tool> <verb>` CLI.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

const FAKE_UUID: &str = "6f1f0a52-6c1e-4b8e-9d0e-0b1d2c3e4f50";
const CALLER_UUID: &str = "1a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d";

fn awm_bin() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_awm"))
}

fn fake_bin() -> PathBuf {
    let bin = awm_bin().with_file_name("awm-fake-plugin");
    assert!(bin.exists(), "{} missing: run `cargo test --workspace`", bin.display());
    bin
}

/// The awm env's interpreter, when this machine has one (the build container mounts it).
fn awm_python() -> Option<PathBuf> {
    let root = std::env::var_os("AWM_CONDA_ROOT").map(PathBuf::from)?;
    let py = root.join("envs/awm/bin/python");
    py.exists().then_some(py)
}

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..").canonicalize().unwrap()
}

fn write(path: &Path, text: &str) {
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, text).unwrap();
}

struct Node {
    _tmp: tempfile::TempDir,
    state: PathBuf,
    journal: bool,
}

fn lay_out() -> Node {
    let tmp = tempfile::tempdir().unwrap();
    let base = tmp.path().to_path_buf();
    let (core, third, state, ws) = (base.join("core"), base.join("third"), base.join("state"), base.join("ws"));
    std::fs::create_dir_all(&ws).unwrap();
    let fake = fake_bin();

    write(
        &core.join("fake/awm-plugin.toml"),
        &format!(
            r#"[plugin]
name = "fake"

[[service]]
name = "fake"

[[tool]]
name = "fake"
uuid = "{FAKE_UUID}"
service = "fake"

[[page]]
name = "ui"
dir = "ui"
"#
        ),
    );
    write(&core.join("fake/run.sh"), &format!("exec '{}'\n", fake.display()));
    write(&core.join("fake/ui/index.html"), "<html><body>fake-ui-marker</body></html>\n");

    write(
        &third.join("caller/awm-plugin.toml"),
        &format!(
            r#"[plugin]
name = "caller"
trust = "third-party"

[[service]]
name = "caller"

[[tool]]
name = "caller"
uuid = "{CALLER_UUID}"
service = "caller"

[[needs.tool]]
uuid = "{FAKE_UUID}"
major = 1
"#
        ),
    );
    write(&third.join("caller/run.sh"), &format!("exec '{}'\n", fake.display()));

    // A plugin another user's systemd unit runs: the gateway serves it but does not spawn it.
    write(&core.join("ext/run.sh"), "exit 1\n");
    write(&state.join("ext.token"), "ext-secret\n");

    let python = awm_python();
    let mut env = String::new();
    if let Some(py) = &python {
        let repo = repo();
        let mut path = vec![repo.join("awm/gateway"), repo.join("awm/services/journal")];
        for comp in std::fs::read_dir(repo.join("awm/service_components")).unwrap().flatten() {
            path.push(comp.path());
        }
        let path = path.iter().map(|p| p.display().to_string()).collect::<Vec<_>>().join(":");
        std::os::unix::fs::symlink(repo.join("awm/services/journal"), core.join("journal")).unwrap();
        env = format!(
            "[env]\nAWM_PYTHON = \"{}\"\nPYTHONPATH = \"{path}\"\nAWM_SEARCH = \"0\"\nPYTHONDONTWRITEBYTECODE = \"1\"\n",
            py.display()
        );
    } else {
        eprintln!("no awm conda env (AWM_CONDA_ROOT): skipping the Python journal service");
    }
    write(
        &state.join("config.toml"),
        &format!(
            "node = \"e2e\"\nlisten = \"127.0.0.1:0\"\nworkspace = \"{}\"\ncore_roots = [\"{}\"]\nplugin_roots = [\"{}\"]\ntool_wait_s = 90\n[[external]]\nservice = \"ext\"\ntoken_file = \"{}\"\n[fleet]\nhome = \"e2e\"\nnodes = [\"e2e\"]\n{env}",
            ws.display(),
            core.display(),
            third.display(),
            state.join("ext.token").display()
        ),
    );
    Node { _tmp: tmp, state, journal: python.is_some() }
}

struct Gateway {
    child: Child,
    url: String,
}

impl Gateway {
    fn start(state: &Path) -> Self {
        let child = Command::new(awm_bin())
            .args(["gateway", "serve", "--state"])
            .arg(state)
            .env("AWM_LOG", "info")
            .stderr(std::fs::File::create(state.join("gateway.stderr")).unwrap())
            .spawn()
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(20);
        let url = loop {
            if let Ok(text) = std::fs::read_to_string(state.join("gateway.json")) {
                if let Some(url) = serde_json::from_str::<Value>(&text).ok().and_then(|v| v["url"].as_str().map(str::to_string)) {
                    break url;
                }
            }
            assert!(Instant::now() < deadline, "gateway did not start: {}", log(state));
            std::thread::sleep(Duration::from_millis(50));
        };
        Gateway { child, url }
    }

    /// SIGTERM, then wait: the gateway stands its services down before it exits.
    fn stop(mut self) -> std::process::ExitStatus {
        // SAFETY: kill has no memory preconditions; the pid is our own child.
        unsafe { libc::kill(self.child.id() as i32, libc::SIGTERM) };
        let deadline = Instant::now() + Duration::from_secs(20);
        loop {
            if let Some(status) = self.child.try_wait().unwrap() {
                return status;
            }
            assert!(Instant::now() < deadline, "gateway did not stop within 20 s");
            std::thread::sleep(Duration::from_millis(100));
        }
    }
}

impl Drop for Gateway {
    fn drop(&mut self) {
        let _ = self.child.kill();
    }
}

fn log(state: &Path) -> String {
    std::fs::read_to_string(state.join("gateway.stderr")).unwrap_or_default()
}

/// Run `awm <words>` against the gateway: (exit code, stdout, stderr).
fn cli(url: &str, words: &[&str]) -> (i32, String, String) {
    let out = Command::new(awm_bin()).args(words).env("AWM_GATEWAY_URL", url).env_remove("AWM_DIR").output().unwrap();
    (out.status.code().unwrap_or(-1), String::from_utf8_lossy(&out.stdout).into(), String::from_utf8_lossy(&out.stderr).into())
}

/// One `awm mcp` process, driven line by line over stdio.
struct Mcp {
    child: Child,
    lines: std::io::Lines<BufReader<std::process::ChildStdout>>,
    next: i64,
}

impl Mcp {
    fn start(url: &str) -> Self {
        let mut child = Command::new(awm_bin())
            .arg("mcp")
            .env("AWM_GATEWAY_URL", url)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let lines = BufReader::new(child.stdout.take().unwrap()).lines();
        Mcp { child, lines, next: 1 }
    }

    fn request(&mut self, method: &str, params: Value) -> Value {
        let id = self.next;
        self.next += 1;
        let stdin = self.child.stdin.as_mut().unwrap();
        writeln!(stdin, "{}", json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params})).unwrap();
        stdin.flush().unwrap();
        let line = self.lines.next().expect("awm mcp closed stdout").unwrap();
        let reply: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(reply["id"], id);
        reply
    }

    /// `tools/call` one domain tool: (text, isError).
    fn call(&mut self, tool: &str, arguments: Value) -> (String, bool) {
        let r = self.request("tools/call", json!({"name": tool, "arguments": arguments}));
        let result = &r["result"];
        (result["content"][0]["text"].as_str().unwrap_or_default().to_string(), result["isError"] == true)
    }
}

impl Drop for Mcp {
    fn drop(&mut self) {
        let _ = self.child.kill();
    }
}

fn http() -> reqwest::blocking::Client {
    reqwest::blocking::Client::builder().timeout(Duration::from_secs(120)).build().unwrap()
}

#[test]
fn gateway_end_to_end() {
    let node = lay_out();
    let gw = Gateway::start(&node.state);
    let url = gw.url.clone();
    let state = node.state.clone();
    let fail = |what: &str| -> String { format!("{what}\n--- gateway log ---\n{}", log(&state)) };

    // MCP: the catalog collapses each tool to {verb, args, peer}, and calls route to the plugin.
    let mut mcp = Mcp::start(&url);
    let init = mcp.request("initialize", json!({"protocolVersion": "2025-06-18", "capabilities": {}}));
    assert_eq!(init["result"]["serverInfo"]["name"], "awm");
    let (text, err) = mcp.call("fake", json!({"verb": "echo", "args": {"text": "over mcp"}}));
    assert_eq!((text.as_str(), err), ("over mcp", false), "{}", fail("mcp echo"));

    let list = mcp.request("tools/list", json!({}));
    let tools = list["result"]["tools"].as_array().cloned().unwrap_or_default();
    let names: Vec<&str> = tools.iter().filter_map(|t| t["name"].as_str()).collect();
    for want in ["awm", "fake", "caller"] {
        assert!(names.contains(&want), "tools/list lacks {want}: {names:?}");
    }
    let fake = tools.iter().find(|t| t["name"] == "fake").unwrap();
    let verbs = &fake["inputSchema"]["properties"]["verb"]["enum"];
    for want in ["echo", "add", "whoami", "describe"] {
        assert!(verbs.as_array().unwrap().iter().any(|v| v == want), "fake verbs lack {want}: {verbs}");
    }
    assert!(fake["inputSchema"]["properties"]["peer"].is_object(), "the peer key is missing: {fake}");

    let (text, err) = mcp.call("fake", json!({"verb": "describe"}));
    assert!(!err && text.contains("echo") && text.contains("text"), "fake describe: {text}");
    let (text, err) = mcp.call("fake", json!({"verb": "echo", "args": {"text": "x", "bogus": 1}}));
    assert!(err && text.contains("bogus"), "an unknown argument must be refused with the schema: {text}");
    let (text, err) = mcp.call("fake", json!({"verb": "echo", "args": {"text": "x"}, "peer": "nowhere"}));
    assert!(err, "a wrong peer must be refused, never run locally: {text}");
    let (text, err) = mcp.call("nosuch", json!({"verb": "describe"}));
    assert!(err && text.contains("404"), "unknown tool: {text}");

    // CLI: one command per verb, flags parsed as JSON where they parse.
    let (code, out, errout) = cli(&url, &["fake", "add", "--a", "2", "--b", "3"]);
    assert_eq!(code, 0, "{errout}");
    assert_eq!(out.trim().parse::<f64>().unwrap(), 5.0);
    let (code, out, _) = cli(&url, &["fake", "whoami"]);
    assert_eq!(code, 0);
    let who: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(who["caller"]["node"], "e2e");
    assert_eq!(who["caller"]["lane"], "local");
    let (code, _, errout) = cli(&url, &["fake", "echo", "--text", "x", "--bogus", "1"]);
    assert!(code == 1 && errout.contains("bogus"), "{errout}");

    // A caller frame is honoured only with the edge token.
    let forged = json!({"node": "elsewhere", "lane": "edge", "session": "s1", "trust": "high"}).to_string();
    let body = json!({"name": "fake", "args": {"verb": "whoami"}});
    let forged_reply: Value = http().post(format!("{url}/invoke")).header("X-Awm-Caller", &forged).json(&body).send().unwrap().json().unwrap();
    let who: Value = serde_json::from_str(forged_reply["result"].as_str().unwrap()).unwrap();
    assert_eq!(who["caller"]["node"], "e2e", "a caller frame without the edge token must be ignored");
    let token = std::fs::read_to_string(node.state.join("edge.token")).unwrap();
    let edge_reply: Value = http()
        .post(format!("{url}/invoke"))
        .header("X-Awm-Caller", &forged)
        .header("X-Awm-Edge-Token", token.trim())
        .header("X-Awm-As", "tony")
        .json(&body)
        .send()
        .unwrap()
        .json()
        .unwrap();
    let who: Value = serde_json::from_str(edge_reply["result"].as_str().unwrap()).unwrap();
    assert_eq!((who["caller"]["node"].as_str(), who["caller"]["lane"].as_str(), who["as"].as_str()), (Some("elsewhere"), Some("edge"), Some("tony")));

    // A plugin calls the tool it needs by the name the gateway resolved for its UUID.
    let resolved: Value = serde_json::from_str(
        &std::fs::read_to_string(node.state.join("plugins/caller/resolved-tools.json")).expect("caller's resolved-tools.json"),
    )
    .unwrap();
    assert_eq!(resolved["tools"][FAKE_UUID]["name"], "fake");
    let (code, out, errout) = cli(&url, &["caller", "call_needed", "--verb", "echo", "--args", r#"{"text":"via caller"}"#]);
    assert!(code == 0 && out.contains("via caller"), "{out} {errout}");

    // Background runs and code use report themselves; pages count visits.
    let (code, out, _) = cli(&url, &["fake", "run_job", "--job", "nightly"]);
    assert_eq!((code, out.trim()), (0, "ran"));
    let page = http().get(format!("{url}/p/fake/ui/")).header("Accept", "text/html").send().unwrap();
    assert!(page.status().is_success());
    assert!(page.text().unwrap().contains("fake-ui-marker"));
    let posted = http().post(format!("{url}/usage")).json(&json!({"plugin": "fake", "surface": "code", "name": "lib"})).send().unwrap();
    assert!(posted.status().is_success());

    // The Python journal service, loaded unchanged from its run.sh folder.
    if node.journal {
        let (code, _, errout) =
            cli(&url, &["journal", "post", "--project", "e2e", "--scope", "t", "--author", "e2e", "--body", "hello from the rust gateway"]);
        assert_eq!(code, 0, "{}", fail(&format!("journal post: {errout}")));
        let (text, err) = mcp.call("journal", json!({"verb": "fetch", "args": {"project": "e2e", "scope": "t"}}));
        assert!(!err && text.contains("hello from the rust gateway"), "journal fetch: {text}");
    }

    // An external service registers with its token. The supervisor never ran its run.sh.
    assert!(!node.state.join("logs/ext.log").exists(), "the supervisor spawned an external service");
    let refused = http().post(format!("{url}/hub/service/register")).json(&json!({"name": "ext"})).send().unwrap();
    assert_eq!(refused.status().as_u16(), 403, "an external service must not register without its token");
    let hijack = http().post(format!("{url}/hub/service/register")).json(&json!({"name": "fake"})).send().unwrap();
    assert_eq!(hijack.status().as_u16(), 403, "a spawned service's name must need the token the supervisor gave it");
    let mut ext = Command::new(fake_bin())
        .env("AWM_HUB_URL", &url)
        .env("AWM_SERVICE_NAME", "ext")
        .env("AWM_HUB_TOKEN_FILE", node.state.join("ext.token"))
        .spawn()
        .unwrap();
    let (code, out, errout) = cli(&url, &["ext", "echo", "--text", "from outside"]);
    let _ = ext.kill();
    let _ = ext.wait();
    assert!(code == 0 && out.contains("from outside"), "{}", fail(&format!("external echo: {out} {errout}")));
    let resolved: Value = http().get(format!("{url}/hub/plugins/caller/resolved")).send().unwrap().json().unwrap();
    assert_eq!(resolved["tools"][FAKE_UUID]["name"], "fake");
    assert_eq!(http().get(format!("{url}/hub/plugins/..%2Fx/resolved")).send().unwrap().status().as_u16(), 404);

    // Usage per plugin and surface, not MCP calls alone (R132).
    let (code, out, _) = cli(&url, &["usage", "--days", "1"]);
    assert_eq!(code, 0);
    let tally: Value = serde_json::from_str(&out).unwrap();
    for surface in ["mcp", "cli", "http", "plugin", "page", "code", "background"] {
        assert!(tally["plugins"]["fake"][surface].as_u64().unwrap_or(0) >= 1, "fake lacks {surface} usage: {tally}");
    }
    assert!(tally["plugins"]["caller"]["cli"].as_u64().unwrap_or(0) >= 1, "{tally}");
    if node.journal {
        assert!(tally["plugins"]["journal"]["cli"].as_u64().unwrap_or(0) >= 1, "{tally}");
        assert!(tally["plugins"]["journal"]["mcp"].as_u64().unwrap_or(0) >= 1, "{tally}");
    }
    assert!(tally["detail"]["fake"]["background nightly"].as_u64().unwrap_or(0) >= 1, "{tally}");

    drop(mcp);
    let status = gw.stop();
    assert!(status.success(), "{}", fail("gateway exit"));
    assert!(!node.state.join("gateway.json").exists(), "gateway.json outlived the gateway");
}

#[test]
fn bootstrap_writes_a_config_the_gateway_accepts() {
    let tmp = tempfile::tempdir().unwrap();
    let state = tmp.path().join("state");
    let run = |extra: &[&str]| {
        let out = Command::new(awm_bin()).args(["bootstrap", "--state"]).arg(&state).args(extra).output().unwrap();
        (out.status.success(), String::from_utf8_lossy(&out.stdout).to_string())
    };
    let repo = repo();
    let (ok, out) = run(&["--awm-root", repo.to_str().unwrap()]);
    assert!(ok && out.contains("wrote"), "{out}");
    for sub in ["usage", "logs", "plugins"] {
        assert!(state.join(sub).is_dir());
    }
    let cfg: Value = toml::from_str::<toml::Value>(&std::fs::read_to_string(state.join("config.toml")).unwrap())
        .map(|v| serde_json::to_value(v).unwrap())
        .unwrap();
    assert_eq!(cfg["core_roots"][0], repo.join("awm/services").display().to_string());

    let (ok, out) = run(&["--awm-root", repo.to_str().unwrap()]);
    assert!(ok && out.contains("kept"), "a second bootstrap must keep the config: {out}");

    let bad = tmp.path().join("bad.toml");
    std::fs::write(&bad, "no_such_key = 1\n").unwrap();
    let (ok, _) = run(&["--config", bad.to_str().unwrap(), "--force"]);
    assert!(!ok, "bootstrap must refuse a config the gateway would refuse");
}
