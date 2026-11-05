//! The gateway end to end: `awm gateway serve` in a temp dir loads a fake plugin, a third-party
//! plugin that needs it, and (when the awm conda env is present) today's Python journal service
//! unchanged. Calls go in through `awm mcp` and the `awm <tool> <verb>` CLI.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use serde_json::{json, Value};


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

fn chrono_now() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs() as i64
}

fn write(path: &Path, text: &str) {
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, text).unwrap();
}

struct Node {
    _tmp: tempfile::TempDir,
    state: PathBuf,
    third: PathBuf,
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
        r#"[plugin]
name = "fake"

[[service]]
name = "fake"

[[tool]]
name = "fake"
service = "fake"

[[page]]
name = "ui"
dir = "ui"

[needs]
secrets = ["FAKE_SECRET"]
network = ["egress-ok.invalid"]
"#,
    );
    write(&core.join("fake/run.sh"), &format!("export FAKE_MOUNT=1\nexec '{}'\n", fake.display()));
    // reflection is the fake too: it answers session_of from sessions.json.
    write(&core.join("reflection/awm-plugin.toml"), "[plugin]\nname = \"reflection\"\n\n[[service]]\nname = \"reflection\"\n\n[[tool]]\nname = \"reflection\"\n");
    write(
        &core.join("reflection/run.sh"),
        &format!("export FAKE_TOOL=reflection FAKE_SESSIONS_FILE='{}'\nexec '{}'\n", state.join("sessions.json").display(), fake.display()),
    );
    // operations is the fake too: it records approval requests in approval.json.
    write(&core.join("operations/awm-plugin.toml"), "[plugin]\nname = \"operations\"\n\n[[service]]\nname = \"operations\"\n\n[[tool]]\nname = \"operations\"\n");
    write(
        &core.join("operations/run.sh"),
        &format!("export FAKE_TOOL=operations FAKE_APPROVAL_FILE='{}'\nexec '{}'\n", state.join("approval.json").display(), fake.display()),
    );
    // The fake also serves the other tools the journal plugin needs to load.
    for tool in ["scope", "search", "auth"] {
        write(&core.join(format!("{tool}/awm-plugin.toml")), &format!("[plugin]\nname = \"{tool}\"\n\n[[service]]\nname = \"{tool}\"\n\n[[tool]]\nname = \"{tool}\"\n"));
        write(&core.join(format!("{tool}/run.sh")), &format!("export FAKE_TOOL={tool}\nexec '{}'\n", fake.display()));
    }
    write(&core.join("fake/ui/index.html"), "<html><body>fake-ui-marker</body></html>\n");

    write(
        &third.join("caller/awm-plugin.toml"),
        r#"[plugin]
name = "caller"

[[service]]
name = "caller"

[[tool]]
name = "caller"
service = "caller"

[[needs.tool]]
name = "fake"
major = 1
verbs = ["echo", "whoami"]
"#,
    );
    write(&third.join("caller/run.sh"), &format!("exec '{}'\n", fake.display()));

    // A plugin another user's systemd unit runs: the gateway serves it but does not spawn it.
    write(&core.join("ext/run.sh"), "exit 1\n");
    write(&state.join("ext.token"), "ext-secret\n");
    write(&state.join("secrets.env"), "# node secrets\nFAKE_SECRET=s3cret\nOTHER_SECRET=not-yours\n");

    let python = awm_python();
    let mut env = String::new();
    if let Some(py) = &python {
        let repo = repo();
        let mut path = vec![repo.join("awm/services/journal"), repo.join("awm/service_components/grantlib")];
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
            "node = \"e2e\"\nlisten = \"127.0.0.1:0\"\nworkspace = \"{}\"\ncore_roots = [\"{}\"]\nplugin_roots = [\"{}\"]\ntool_wait_s = 90\nsession_cache_s = 0\n[[external]]\nservice = \"ext\"\ntoken_file = \"{}\"\n[fleet]\nhome = \"e2e\"\nnodes = [\"e2e\"]\n{env}",
            ws.display(),
            core.display(),
            third.display(),
            state.join("ext.token").display()
        ),
    );
    Node { _tmp: tmp, state, third, journal: python.is_some() }
}

struct Gateway {
    child: Child,
    url: String,
}

impl Gateway {
    fn start(state: &Path) -> Self {
        Self::start_with(state, &[])
    }

    fn start_with(state: &Path, env: &[(&str, &str)]) -> Self {
        let child = Command::new(awm_bin())
            .args(["gateway", "serve", "--state"])
            .arg(state)
            .envs(env.iter().copied())
            .env_remove("AWM_EDGES_FILE")
            .env("AWM_LOG", "info")
            .env("GATEWAY_ONLY_SECRET", "leaked")
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

/// Open a stream WebSocket and talk to it from this sync test.
struct Stream {
    rt: tokio::runtime::Runtime,
    pipe: awm_gateway::peer::WsPipe,
}

impl Stream {
    fn open(url: &str) -> Result<Self, String> {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let ws = url.replacen("http", "ws", 1);
        let pipe = rt.block_on(awm_gateway::peer::connect_ws(&ws, &[]))?;
        Ok(Stream { rt, pipe })
    }

    fn send(&self, m: awm_proto::WsMsg) {
        self.pipe.tx.send(m).unwrap();
    }

    fn next(&mut self) -> Option<awm_proto::WsMsg> {
        let rx = &mut self.pipe.rx;
        self.rt.block_on(async { tokio::time::timeout(Duration::from_secs(20), rx.recv()).await.ok().flatten() })
    }
}

/// Emit on `emit_url`'s `<tool> emit` until the stream yields a payload, which it returns.
fn first_emitted(stream: &mut Stream, emit_url: &str, tool: &str) -> Value {
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        let resp = http().post(format!("{emit_url}/invoke"))
            .json(&json!({"name": tool, "args": {"verb": "emit", "args": {"topic": "news", "payload": {"n": 1, "project": "open"}}}}))
            .send().unwrap();
        assert!(resp.status().is_success(), "emit: {}", resp.text().unwrap_or_default());
        let rx = &mut stream.pipe.rx;
        let got = stream.rt.block_on(async { tokio::time::timeout(Duration::from_millis(300), rx.recv()).await.ok().flatten() });
        if let Some(awm_proto::WsMsg::Text(t)) = got {
            return serde_json::from_str(&t).unwrap();
        }
        assert!(Instant::now() < deadline, "no emitted payload arrived: {got:?}");
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
    assert!(!verbs.as_array().unwrap().iter().any(|v| v == "run_job"), "a hook-only verb lists on MCP: {verbs}");
    assert!(fake["inputSchema"]["properties"]["peer"].is_object(), "the peer key is missing: {fake}");

    let (text, err) = mcp.call("fake", json!({"verb": "describe"}));
    assert!(!err && text.contains("echo") && text.contains("text"), "fake describe: {text}");
    assert!(!text.contains("run_job"), "MCP describe shows a hook-only verb: {text}");
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
    assert_eq!((who["trust"].as_str(), who["scope"].as_str()), (Some("high"), None), "an edge caller's trust reaches the args");

    // A local session gets its id, its level and its scope (from its cwd) from reflection's
    // session_of. The model's own `_caller_trust` is refused as an unknown argument.
    let worktree = node.state.parent().unwrap().join("ws/projects/awm/redux-x");
    std::fs::create_dir_all(worktree.join("core")).unwrap();
    write(&worktree.join(".git"), "gitdir: ../.bare/worktrees/redux-x\n");
    let uuid = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0";
    let r9 = format!("cc{}", uuid.replace('-', ""));
    let session = |trust: &str| json!({"4242": {"ok": true, "known": true, "session_id": r9, "trust": trust, "mode": null, "cwd": worktree.join("core")}}).to_string();
    write(&node.state.join("sessions.json"), &session("cooperative"));
    let as_session = |args: Value| -> Value {
        http().post(format!("{url}/invoke")).header("X-Awm-Session-Pid", "4242").json(&json!({"name": "fake", "args": args})).send().unwrap().json().unwrap()
    };
    let reply = as_session(json!({"verb": "whoami"}));
    let who: Value = serde_json::from_str(reply["result"].as_str().unwrap()).unwrap();
    assert_eq!(who["caller"]["session"], json!(r9), "{who}");
    assert_eq!((who["caller"]["trust"].as_str(), who["caller"]["scope"].as_str()), (Some("cooperative"), Some("awm/redux-x")), "{who}");
    assert_eq!((who["trust"].as_str(), who["scope"].as_str()), (Some("cooperative"), Some("awm/redux-x")), "{who}");
    // The stamped level gates the call before the tool is looked up (channels.md R125).
    let gated: Value = http().post(format!("{url}/invoke")).header("X-Awm-Session-Pid", "4242")
        .json(&json!({"name": "trilium", "args": {"verb": "note_get"}})).send().unwrap().json().unwrap();
    assert!(gated.to_string().contains("a cooperative session may not use the \\\"trilium\\\" tool"), "{gated}");
    let reply = as_session(json!({"verb": "whoami", "args": {"_caller_trust": "trusted"}}));
    assert!(reply["result"].is_null() && reply.to_string().contains("_caller_trust"), "{reply}");
    write(&node.state.join("sessions.json"), &session("trusted"));
    let reply = as_session(json!({"verb": "whoami"}));
    let who: Value = serde_json::from_str(reply["result"].as_str().unwrap()).unwrap();
    assert_eq!(who["trust"], "trusted", "{who}");
    let ungated: Value = http().post(format!("{url}/invoke")).header("X-Awm-Session-Pid", "4242")
        .json(&json!({"name": "trilium", "args": {"verb": "note_get"}})).send().unwrap().json().unwrap();
    assert!(ungated.to_string().contains("Unknown tool"), "a trusted session passes the level gate: {ungated}");
    write(&node.state.join("sessions.json"), "{}");
    let reply = as_session(json!({"verb": "whoami"}));
    let who: Value = serde_json::from_str(reply["result"].as_str().unwrap()).unwrap();
    assert_eq!((who["caller"]["session"].clone(), who["trust"].as_str()), (Value::Null, Some("trusted")), "a pid no harness claims: {who}");

    // The CLI names its own pid, and the gateway walks up to the session that runs it. A child
    // of a cooperative session is cooperative. A shell no harness claims is a user's, trusted.
    // This test is the session, and a child process of it stands in for the CLI.
    let mut cli_proc = std::process::Command::new("sleep").arg("60").spawn().unwrap();
    let (me, parent) = (cli_proc.id(), std::process::id());
    let as_cli = |args: Value| -> Value {
        http().post(format!("{url}/invoke")).header("X-Awm-Caller-Pid", me.to_string()).json(&json!({"name": "fake", "args": args})).send().unwrap().json().unwrap()
    };
    let at = |pid: u32, rec: Value| json!({pid.to_string(): rec}).to_string();
    write(&node.state.join("sessions.json"), &at(parent, json!({"ok": true, "known": true, "session_id": r9, "trust": "cooperative", "mode": null, "cwd": worktree.join("core")})));
    let who: Value = serde_json::from_str(as_cli(json!({"verb": "whoami"}))["result"].as_str().unwrap()).unwrap();
    assert_eq!((who["trust"].as_str(), who["caller"]["session"].as_str()), (Some("cooperative"), Some(r9.as_str())), "a CLI under a cooperative session: {who}");
    assert_eq!(who["pid"], json!(parent), "the service finds the session by the pid the walk ended on: {who}");
    write(&node.state.join("sessions.json"), "{}");
    let who: Value = serde_json::from_str(as_cli(json!({"verb": "whoami"}))["result"].as_str().unwrap()).unwrap();
    assert_eq!(who["trust"], "trusted", "a shell with no harness ancestor: {who}");
    let _ = cli_proc.kill();

    // A lookup that fails runs untrusted with mode `unknown`, which may only compact and ask
    // reflection who it is (fleet.md R103 item 2).
    write(&node.state.join("sessions.json"), &at(4242, json!("not an answer")));
    let reply = as_session(json!({"verb": "whoami"}));
    assert!(reply["result"].is_null() && reply.to_string().contains("may not call fake.whoami"), "a failed lookup must be gated: {reply}");
    let reply: Value = http().post(format!("{url}/invoke")).header("X-Awm-Session-Pid", "4242")
        .json(&json!({"name": "awm", "args": {"verb": "reload"}})).send().unwrap().json().unwrap();
    assert!(reply["result"].is_null(), "an untrusted caller reloaded the gateway: {reply}");
    write(&node.state.join("sessions.json"), &at(4242, json!({"ok": true, "known": true, "session_id": r9, "trust": "trusted", "cwd": "/"})));
    let reply = as_session(json!({"verb": "whoami"}));
    assert!(reply.to_string().contains("may not call fake.whoami"), "a session whose mode is unread is gated: {reply}");
    write(&node.state.join("sessions.json"), "{}");

    // A plugin gets the secrets its manifest lists, and nothing else of the node's (R116 item 1).
    let env_of = |tool: &str, name: &str| -> String {
        let (code, out, errout) = cli(&url, &[tool, "env", "--name", name]);
        assert_eq!(code, 0, "{errout}");
        out.trim().to_string()
    };
    assert_eq!(env_of("fake", "FAKE_SECRET"), "s3cret");
    assert_eq!(env_of("fake", "OTHER_SECRET"), "null");
    assert_eq!(env_of("caller", "FAKE_SECRET"), "null", "a plugin got a secret it does not declare");
    assert_eq!(env_of("fake", "GATEWAY_ONLY_SECRET"), "null", "the gateway's own environment leaked into a service");

    // A plugin reaches only the hosts its needs.network lists, through its own egress proxy.
    let proxy = env_of("fake", "HTTPS_PROXY");
    assert!(proxy.starts_with("http://127.0.0.1:"), "a guarded plugin has no proxy: {proxy}");
    assert!(env_of("fake", "NO_PROXY").contains("127.0.0.1"));
    let connect = |target: &str| -> String {
        use std::io::{Read, Write};
        let mut s = std::net::TcpStream::connect(proxy.trim_start_matches("http://")).unwrap();
        write!(s, "CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n").unwrap();
        let mut out = String::new();
        let _ = s.read_to_string(&mut out);
        out
    };
    let refused = connect("example.org:443");
    assert!(refused.starts_with("HTTP/1.1 403") && refused.contains("add \"example.org\" to network"), "{refused}");
    let listed = connect("egress-ok.invalid:443");
    assert!(listed.starts_with("HTTP/1.1 502"), "a listed host must pass the guard and fail only on DNS: {listed}");
    assert!(connect("egress-ok.invalid:8443").starts_with("HTTP/1.1 403"), "a listed host allows only 80 and 443");

    // A plugin calls the tool it needs by its name, looked up at call time.
    let owner: Value = http().get(format!("{url}/hub/tool/fake")).send().unwrap().json().unwrap();
    assert_eq!((owner["plugin"].as_str(), owner["peer"].as_str()), (Some("fake"), None), "{owner}");
    assert_eq!(http().get(format!("{url}/hub/tool/nobody")).send().unwrap().status().as_u16(), 404);
    let (code, out, errout) = cli(&url, &["caller", "call_needed", "--verb", "echo", "--args", r#"{"text":"via caller"}"#]);
    assert!(code == 0 && out.contains("via caller"), "{out} {errout}");
    // It names itself by its service token: a trusted call on the service lane.
    let (code, out, errout) = cli(&url, &["caller", "call_needed", "--verb", "whoami"]);
    assert!(code == 0, "{errout}");
    assert!(out.contains(r#"\"lane\": \"service\""#) && out.contains(r#"\"trust\": \"trusted\""#), "a service call: {out}");
    let forged = http()
        .post(format!("{url}/invoke"))
        .header("X-Awm-Service-Token", "not-a-token")
        .json(&json!({"name": "fake", "args": {"verb": "whoami"}}))
        .send()
        .unwrap();
    assert_eq!(forged.status().as_u16(), 401, "a wrong service token passed");
    // A verb the manifest does not list is refused, by the service token (R116 item 3).
    let (code, out, errout) = cli(&url, &["caller", "call_needed", "--verb", "add", "--args", r#"{"a":1,"b":2}"#]);
    assert!(code == 0 && out.contains("does not list fake.add"), "an undeclared verb passed: {out} {errout}");
    // A page is held to its plugin's needs by the path it was served from.
    let from_page = |tool: &str| {
        http()
            .post(format!("{url}/invoke"))
            .header("Referer", format!("{url}/p/fake/ui/"))
            .json(&json!({"name": tool, "args": {"verb": "echo", "args": {"text": "from the page"}}}))
            .send()
            .unwrap()
            .status()
            .as_u16()
    };
    assert_eq!(from_page("fake"), 200, "a page may call its own tool");
    assert_eq!(from_page("caller"), 403, "a page called a tool its plugin does not need");
    let gone = http().post(format!("{url}/svc/fake/fn/echo")).json(&json!({"text": "x"})).send().unwrap();
    assert_eq!(gone.status().as_u16(), 404, "the /svc door still answers");

    // Background runs and code use report themselves; pages count visits.
    let (code, out, _) = cli(&url, &["fake", "run_job", "--job", "nightly"]);
    assert_eq!((code, out.trim()), (0, "ran"));
    let page = http().get(format!("{url}/p/fake/ui/")).header("Accept", "text/html").send().unwrap();
    assert!(page.status().is_success());
    assert!(page.text().unwrap().contains("fake-ui-marker"));

    // A service's own HTTP server, behind the gateway at /s/<service>/.
    let mounted = http().get(format!("{url}/s/fake/hello?q=1")).send().unwrap();
    assert_eq!(mounted.text().unwrap(), "mount GET /hello?q=1");
    assert_eq!(http().get(format!("{url}/s/caller/x")).send().unwrap().status().as_u16(), 404, "a service with no mount");

    // Emit topics: each payload reaches each subscriber as one JSON frame.
    let mut sub = Stream::open(&format!("{url}/hub/emit/fake/news")).unwrap();
    assert_eq!(first_emitted(&mut sub, &url, "fake")["n"], 1);
    let mut listen = Command::new(awm_bin()).args(["listen", "fake", "news", "--match", "n=1"]).env("AWM_GATEWAY_URL", &url)
        .stdout(Stdio::piped()).spawn().unwrap();
    let mut lines = BufReader::new(listen.stdout.take().unwrap()).lines();
    let poke = url.clone();
    let poker = std::thread::spawn(move || {
        for _ in 0..40 {
            let _ = http().post(format!("{poke}/invoke"))
                .json(&json!({"name": "fake", "args": {"verb": "emit", "args": {"topic": "news", "payload": {"n": 1}}}})).send();
            std::thread::sleep(Duration::from_millis(250));
        }
    });
    let line = lines.next().expect("awm listen printed nothing").unwrap();
    assert_eq!(serde_json::from_str::<Value>(&line).unwrap()["n"], 1);
    let _ = listen.kill();
    poker.join().unwrap();
    assert!(Stream::open(&format!("{url}/hub/emit/nobody/news")).is_err(), "an unknown service has no topics");

    // A direct session: the init goes first, then bytes relay both ways through the bridge.
    let mut session = Stream::open(&format!("{url}/hub/session/fake/echo")).unwrap();
    session.send(awm_proto::WsMsg::Text(json!({"hello": "init"}).to_string()));
    match session.next() {
        Some(awm_proto::WsMsg::Text(t)) => assert_eq!(serde_json::from_str::<Value>(&t).unwrap()["init"]["hello"], "init"),
        other => panic!("session greeting: {other:?} {}", log(&node.state)),
    }
    session.send(awm_proto::WsMsg::Binary(vec![7, 0, 255]));
    assert_eq!(session.next(), Some(awm_proto::WsMsg::Binary(vec![7, 0, 255])));
    assert!(Stream::open(&format!("{url}/hub/session/fake/nope")).is_err(), "an undeclared session kind is refused");

    // The service verbs of the `awm` tool, and their CLI.
    let (code, out, _) = cli(&url, &["services"]);
    assert_eq!(code, 0);
    let rows: Value = serde_json::from_str(&out).unwrap();
    let fake_row = rows.as_array().unwrap().iter().find(|r| r["name"] == "fake").cloned().unwrap();
    assert_eq!((fake_row["status"].clone(), fake_row["enabled"].clone()), (json!("ready"), json!(true)), "{fake_row}");
    assert!(fake_row["mount"].as_str().unwrap().starts_with("http://127.0.0.1:"));
    let (code, out, err) = cli(&url, &["services", "restart", "caller"]);
    assert_eq!(code, 0, "{out}{err}");
    let (code, out, _) = cli(&url, &["caller", "echo", "--text", "back after restart"]);
    assert_eq!((code, out.trim()), (0, "back after restart"));
    let (code, out, _) = cli(&url, &["fake", "echo", "--help"]);
    assert_eq!(code, 0);
    assert!(out.contains("\"verb\": \"echo\"") && !out.contains("\"verb\": \"add\""), "--help describes the one verb: {out}");
    let posted = http().post(format!("{url}/usage")).json(&json!({"plugin": "fake", "surface": "code", "name": "lib"})).send().unwrap();
    assert!(posted.status().is_success());

    // The Python journal service, loaded from its plugin folder.
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

    // A plugin change runs hot (R144): install, rename, remove, with no gateway restart.
    // The plugins the change does not touch keep their processes.
    let fake_pid = |url: &str| -> i64 {
        let (_, out, _) = cli(url, &["awm", "services"]);
        let list: Value = serde_json::from_str(&out).unwrap();
        list.as_array().unwrap().iter().find(|s| s["name"] == "fake").and_then(|s| s["pid"].as_i64()).unwrap()
    };
    let before = fake_pid(&url);
    let reload = || -> Value {
        let (code, out, errout) = cli(&url, &["reload"]);
        assert_eq!(code, 0, "awm reload: {out} {errout}");
        serde_json::from_str(&out).unwrap()
    };
    let hot = node.third.join("hot");
    write(&hot.join("awm-plugin.toml"), "[plugin]\nname = \"hot\"\n\n[[service]]\nname = \"hot\"\n\n[[tool]]\nname = \"hot\"\n");
    write(&hot.join("run.sh"), &format!("export FAKE_TOOL=hot\nexec '{}'\n", fake_bin().display()));
    let r = reload();
    assert_eq!(r["added"], json!(["hot"]), "{r}");
    let (code, out, errout) = cli(&url, &["hot", "echo", "--text", "installed hot"]);
    assert!(code == 0 && out.contains("installed hot"), "{}", fail(&format!("hot echo: {out} {errout}")));
    write(&hot.join("awm-plugin.toml"), "[plugin]\nname = \"hot\"\n\n[[service]]\nname = \"hot\"\n\n[[tool]]\nname = \"hot-two\"\n");
    write(&hot.join("run.sh"), &format!("export FAKE_TOOL=hot-two\nexec '{}'\n", fake_bin().display()));
    let r = reload();
    assert_eq!(r["updated"], json!(["hot"]), "{r}");
    let (code, out, _) = cli(&url, &["hot-two", "echo", "--text", "renamed hot"]);
    assert!(code == 0 && out.contains("renamed hot"), "{out}");
    let (code, _, _) = cli(&url, &["hot", "echo", "--text", "x"]);
    assert_ne!(code, 0, "the old name must go with the rename");
    std::fs::remove_dir_all(&hot).unwrap();
    let r = reload();
    assert_eq!(r["removed"], json!(["hot"]), "{r}");
    assert_eq!(http().get(format!("{url}/hub/tool/hot-two")).send().unwrap().status().as_u16(), 404);
    let r = reload();
    assert_eq!((r["added"].as_array().map(Vec::len), r["updated"].as_array().map(Vec::len)), (Some(0), Some(0)), "{r}");
    assert_eq!(fake_pid(&url), before, "a reload restarted a plugin it did not change");

    // An install copies a folder into the state's installed root, pins it and loads it hot.
    // A refused plugin rolls the whole change back.
    let src = node.state.parent().unwrap().join("src");
    let plugin_at = |dir: &Path, name: &str, tool: &str, needs: &str| {
        write(&dir.join("awm-plugin.toml"), &format!("[plugin]\nname = \"{name}\"\n\n[[service]]\nname = \"{name}\"\n\n[[tool]]\nname = \"{tool}\"\n{needs}"));
        write(&dir.join("run.sh"), &format!("export FAKE_TOOL={tool}\nexec '{}'\n", fake_bin().display()));
    };
    plugin_at(&src.join("inst"), "inst", "inst", "");
    let (code, out, errout) = cli(&url, &["install", src.join("inst").to_str().unwrap()]);
    assert_eq!(code, 0, "{out} {errout}");
    let (code, out, errout) = cli(&url, &["inst", "echo", "--text", "installed"]);
    assert!(code == 0 && out.contains("installed"), "{}", fail(&format!("inst echo: {out} {errout}")));
    let pins = std::fs::read_to_string(node.state.join("installed.toml")).unwrap();
    assert!(pins.contains("name = \"inst\"") && pins.contains("folder:"), "{pins}");
    plugin_at(&src.join("inst"), "inst", "inst-two", "");
    let (code, _, _) = cli(&url, &["inst-two", "echo", "--text", "x"]);
    assert_ne!(code, 0, "an edit of the source must change nothing before the next install");
    let (code, out, errout) = cli(&url, &["install", src.join("inst").to_str().unwrap()]);
    assert_eq!(code, 0, "{out} {errout}");
    let (code, out, _) = cli(&url, &["inst-two", "echo", "--text", "updated"]);
    assert!(code == 0 && out.contains("updated"), "{out}");
    plugin_at(&src.join("fake"), "fake", "fake-again", "");
    let (code, out, _) = cli(&url, &["install", src.join("fake").to_str().unwrap()]);
    assert!(code != 0 && out.contains("already loaded"), "a core plugin's name must not install: {out}");
    plugin_at(&src.join("inst"), "inst", "inst-three", "\n[[needs.tool]]\nname = \"nobody\"\nverbs = [\"get\"]\n");
    let (code, out, _) = cli(&url, &["install", src.join("inst").to_str().unwrap()]);
    assert!(code != 0 && out.contains("nobody"), "{out}");
    let (code, out, _) = cli(&url, &["inst-two", "echo", "--text", "still here"]);
    assert!(code == 0 && out.contains("still here"), "a refused install must leave the old plugin: {out}");
    let (code, out, errout) = cli(&url, &["uninstall", "inst"]);
    assert_eq!(code, 0, "{out} {errout}");
    assert_eq!(http().get(format!("{url}/hub/tool/inst-two")).send().unwrap().status().as_u16(), 404);
    assert!(!node.state.join("installed/inst").exists());

    // A git source installs a pinned commit, and only on an approval that names that commit.
    let repo = src.join("gitted");
    plugin_at(&repo, "gitted", "gitted", "");
    let git = |args: &[&str]| {
        let out = Command::new("git").arg("-C").arg(&repo).args(args).output().unwrap();
        assert!(out.status.success(), "git {args:?}: {}", String::from_utf8_lossy(&out.stderr));
        String::from_utf8_lossy(&out.stdout).trim().to_string()
    };
    git(&["init", "-q"]);
    git(&["add", "-A"]);
    git(&["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one"]);
    let sha = git(&["rev-parse", "HEAD"]);
    let source = format!("git:{}@{sha}", repo.display());
    let (code, out, _) = cli(&url, &["install", &source]);
    assert!(code != 0 && out.contains("AWM_APPROVER_GRANT_PUBKEY"), "with no grant key no approval can pass: {out}");
    // The gateway checks the approver's signature itself, with the key the passkey enrolment gives.
    let signer = ed25519_dalek::SigningKey::from_bytes(&[9; 32]);
    let pk = signer.verifying_key().to_bytes();
    let b64 = |b: &[u8]| base64::Engine::encode(&base64::engine::general_purpose::URL_SAFE_NO_PAD, b);
    let secrets = std::fs::read_to_string(node.state.join("secrets.env")).unwrap();
    write(&node.state.join("secrets.env"), &format!("{secrets}AWM_APPROVER_GRANT_PUBKEY={}\n", b64(&pk)));
    let (code, out, errout) = cli(&url, &["install", &source]);
    assert_eq!(code, 0, "{out} {errout}");
    assert!(out.contains("qfake") && out.contains(&format!("gitted@{sha}")), "{out}");
    let asked: Value = serde_json::from_str(&std::fs::read_to_string(node.state.join("approval.json")).unwrap()).unwrap();
    assert_eq!(asked["kind"], "plugin_commits");
    assert_eq!(asked["payload"]["plugins"][0]["commit"], json!(sha));
    assert_eq!(http().get(format!("{url}/hub/tool/gitted")).send().unwrap().status().as_u16(), 404, "nothing loads before approval");
    let (code, out, _) = cli(&url, &["install", &source, "--approval", "qfake"]);
    assert!(code != 0 && out.contains("pending"), "a pending request must not install: {out}");
    let now = chrono_now();
    let grant = |commits: Value, key: &ed25519_dalek::SigningKey| {
        let body = json!({"grant_id": "gfake", "request_id": "qfake", "kind": "plugin_commits", "node": asked["node"],
            "subject": "tony", "payload_sha256": awm_trust::grant::payload_digest(&asked["payload"]),
            "not_before": now - 5, "not_after": now + 600, "approved_by_device": "dfake", "commits": commits});
        let sig = ed25519_dalek::Signer::sign(key, &awm_trust::grant::canonical(&body)).to_bytes();
        json!({"status": "approved", "kind": "plugin_commits",
               "grant": {"grant": body, "sig": b64(&sig), "key_id": awm_trust::grant::key_id(&pk)}})
    };
    write(&node.state.join("approval.json"), &grant(json!([format!("gitted@{}", "0".repeat(40))]), &signer).to_string());
    let (code, out, _) = cli(&url, &["install", &source, "--approval", "qfake"]);
    assert!(code != 0 && out.contains("not this change"), "a grant for another commit must not install: {out}");
    // A well-formed grant the approver's key did not sign approves nothing, whatever operations says.
    let forger = ed25519_dalek::SigningKey::from_bytes(&[10; 32]);
    write(&node.state.join("approval.json"), &grant(json!([format!("gitted@{sha}")]), &forger).to_string());
    let (code, out, _) = cli(&url, &["install", &source, "--approval", "qfake"]);
    assert!(code != 0 && out.contains("does not verify"), "a grant with a bad signature must not install: {out}");
    assert_eq!(http().get(format!("{url}/hub/tool/gitted")).send().unwrap().status().as_u16(), 404);
    write(&node.state.join("approval.json"), &grant(json!([format!("gitted@{sha}")]), &signer).to_string());
    let (code, out, errout) = cli(&url, &["install", &source, "--approval", "qfake"]);
    assert_eq!(code, 0, "{out} {errout}");
    let (code, out, _) = cli(&url, &["gitted", "echo", "--text", "from git"]);
    assert!(code == 0 && out.contains("from git"), "{out}");
    let pins = std::fs::read_to_string(node.state.join("installed.toml")).unwrap();
    assert!(pins.contains(&format!("commit = \"{sha}\"")) && pins.contains("source = \"git:"), "{pins}");
    assert!(!node.state.join("installed/gitted/.git").exists());
    let (code, out, errout) = cli(&url, &["uninstall", "gitted"]);
    assert_eq!(code, 0, "{out} {errout}");

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

/// A `peer=` call leaves through `ssh <peer> awm edge-serve`, and the peer runs it as a
/// call from the calling node (fleet.md R92, R93). A stand-in for ssh runs `edge-serve`
/// against the far gateway, with the login's key named `awm:near:user`.
#[test]
fn a_peer_call_crosses_the_ssh_edge() {
    let tmp = tempfile::tempdir().unwrap();
    let base = tmp.path();
    let edges = "nodes:\n  near: {role: homebase}\n  far: {role: homebase}\nedges:\n  near: {far: lan}\n";
    let node = |name: &str, tool: Option<&str>| -> PathBuf {
        let (core, state) = (base.join(name).join("core"), base.join(name).join("state"));
        std::fs::create_dir_all(&core).unwrap();
        if let Some(tool) = tool {
            write(&core.join(tool).join("awm-plugin.toml"),
                  &format!("[plugin]\nname = \"{tool}\"\n\n[[service]]\nname = \"{tool}\"\n\n[[tool]]\nname = \"{tool}\"\n"));
            write(&core.join(tool).join("run.sh"), &format!("export FAKE_TOOL={tool}\nexec '{}'\n", fake_bin().display()));
        }
        write(&state.join("edges.yml"), edges);
        write(&state.join("config.toml"), &format!(
            "node = \"{name}\"\nlisten = \"127.0.0.1:0\"\nworkspace = \"{}\"\ncore_roots = [\"{}\"]\ntool_wait_s = 30\nsession_cache_s = 0\n",
            base.join(name).display(), core.display()));
        state
    };
    let (far_state, near_state) = (node("far", Some("remote")), node("near", None));
    let far = Gateway::start(&far_state);

    let home = base.join("home");
    write(&home.join(".ssh/authorized_keys"), "ssh-ed25519 AAAAC3NzaE2Ekey awm:near:user\n");
    write(&base.join("user-auth"), "publickey ssh-ed25519 AAAAC3NzaE2Ekey\n");
    let ssh = base.join("fake-ssh");
    write(&ssh, &format!(
        "#!/bin/sh\nunset AWM_EDGES_FILE\nexport AWM_DIR='{}' AWM_GATEWAY_URL='{}' AWM_NODE_NAME=far SSH_USER_AUTH='{}' HOME='{}'\nexec '{}' edge-serve\n",
        far_state.display(), far.url, base.join("user-auth").display(), home.display(), awm_bin().display()));
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(&ssh, std::fs::Permissions::from_mode(0o755)).unwrap();
    let near = Gateway::start_with(&near_state, &[("AWM_EDGE_SSH", ssh.to_str().unwrap())]);

    let invoke = |peer: &str| {
        let resp = http().post(format!("{}/invoke", near.url))
            .json(&json!({"name": "remote", "args": {"verb": "whoami", "args": {}, "peer": peer}}))
            .send().unwrap();
        (resp.status().as_u16(), resp.json::<Value>().unwrap_or(Value::Null))
    };
    let (status, body) = invoke("far");
    assert_eq!(status, 200, "{body} near: {} far: {}", log(&near_state), log(&far_state));
    let result: Value = serde_json::from_str(body["result"].as_str().unwrap()).unwrap();
    assert_eq!(result["caller"]["node"], "near", "the far node runs it as a call from near: {result}");
    assert_eq!(result["as"], "peer:near", "a service sees a call from another node by its stamp: {result}");

    // Streams cross the edge too: an emit subscription and a direct session on the far node.
    let mut sub = Stream::open(&format!("{}/hub/emit/remote/news?peer=far", near.url)).unwrap();
    assert_eq!(first_emitted(&mut sub, &far.url, "remote")["n"], 1, "near: {} far: {}", log(&near_state), log(&far_state));
    let mut session = Stream::open(&format!("{}/hub/session/remote/echo?peer=far", near.url)).unwrap();
    session.send(awm_proto::WsMsg::Text(json!({"host": "h"}).to_string()));
    match session.next() {
        Some(awm_proto::WsMsg::Text(t)) => assert_eq!(serde_json::from_str::<Value>(&t).unwrap()["init"]["host"], "h"),
        other => panic!("edge session greeting: {other:?} near: {} far: {}", log(&near_state), log(&far_state)),
    }
    session.send(awm_proto::WsMsg::Binary(vec![1, 2, 3]));
    assert_eq!(session.next(), Some(awm_proto::WsMsg::Binary(vec![1, 2, 3])));

    // A marked project stays hidden from the other node, on calls and on streams.
    write(&far_state.join("private.json"), &json!({"version": 1, "marks": [{"project": "open", "path": "/nowhere/open"}]}).to_string());
    let resp = http().post(format!("{}/invoke", near.url))
        .json(&json!({"name": "remote", "args": {"verb": "scoped", "args": {"project": "open"}, "peer": "far"}}))
        .send().unwrap();
    assert!(resp.status().as_u16() >= 400, "a marked project is unknown to a peer: {}", resp.text().unwrap_or_default());
    let local = http().post(format!("{}/invoke", far.url))
        .json(&json!({"name": "remote", "args": {"verb": "scoped", "args": {"project": "open"}}})).send().unwrap();
    assert!(local.status().is_success(), "the node itself still sees it");
    let (status, body) = invoke("elsewhere");
    assert!(status >= 400, "a node with no edge from here is refused: {body}");
    drop(near);
    drop(far);
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
    assert_eq!(cfg["workspace"], repo.display().to_string());
    assert!(cfg.get("core_roots").is_none(), "the core roots default to the tree's own: {cfg}");

    let (ok, out) = run(&["--awm-root", repo.to_str().unwrap()]);
    assert!(ok && out.contains("kept"), "a second bootstrap must keep the config: {out}");

    let bad = tmp.path().join("bad.toml");
    std::fs::write(&bad, "no_such_key = 1\n").unwrap();
    let (ok, _) = run(&["--config", bad.to_str().unwrap(), "--force"]);
    assert!(!ok, "bootstrap must refuse a config the gateway would refuse");
}

const SESSION: &str = "cc0123456789abcdef0123456789abcdef";

/// A node whose `harness-cc` is the fake, holding one caller token, with the isolated door on.
fn lay_out_isolated() -> Node {
    let tmp = tempfile::tempdir().unwrap();
    let base = tmp.path().to_path_buf();
    let (core, state, ws) = (base.join("core"), base.join("state"), base.join("ws"));
    std::fs::create_dir_all(&ws).unwrap();
    let fake = fake_bin();
    for (name, tool) in [("fake", "fake"), ("cch", "harness-cc")] {
        write(
            &core.join(format!("{name}/awm-plugin.toml")),
            &format!("[plugin]\nname = \"{name}\"\n\n[[service]]\nname = \"{name}\"\n\n[[tool]]\nname = \"{tool}\"\nservice = \"{name}\"\n{}", if name == "cch" { "implements = [\"harness@1\"]\n" } else { "" }),
        );
    }
    write(&core.join("fake/run.sh"), &format!("exec '{}'\n", fake.display()));
    let tokens = json!({"good": {"session_id": SESSION, "claude_session_id": "x", "name": "iso", "project": "awm", "scope": "iso",
        "trust": "untrusted", "tools": {"fake": ["echo", "whoami", "scoped"], "harness-cc": ["caller_of_token"]}}});
    write(&core.join("cch/run.sh"), &format!("export FAKE_TOOL=harness-cc FAKE_TOKENS='{tokens}'\nexec '{}'\n", fake.display()));
    write(
        &state.join("config.toml"),
        &format!(
            "node = \"e2e\"\nlisten = \"127.0.0.1:0\"\nisolated_listen = \"127.0.0.1:0\"\nworkspace = \"{}\"\ncore_roots = [\"{}\"]\ntool_wait_s = 30\n",
            ws.display(),
            core.display()
        ),
    );
    Node { _tmp: tmp, third: base.join("third"), state, journal: false }
}

/// One JSON-RPC message to `<base>/mcp`: (status, body).
fn rpc(base: &str, token: Option<&str>, method: &str, params: Value) -> (u16, Value) {
    let mut req = http().post(format!("{base}/mcp")).header("Accept", "application/json, text/event-stream");
    if let Some(t) = token {
        req = req.header("X-Awm-Caller-Token", t);
    }
    let body = if method.starts_with("notifications/") {
        json!({"jsonrpc": "2.0", "method": method})
    } else {
        json!({"jsonrpc": "2.0", "id": 7, "method": method, "params": params})
    };
    let resp = req.json(&body).send().unwrap();
    let status = resp.status().as_u16();
    (status, resp.json().unwrap_or(Value::Null))
}

fn door_call(base: &str, tool: &str, arguments: Value) -> (String, bool) {
    let (status, r) = rpc(base, Some("good"), "tools/call", json!({"name": tool, "arguments": arguments}));
    assert_eq!(status, 200, "{r}");
    (r["result"]["content"][0]["text"].as_str().unwrap_or_default().to_string(), r["result"]["isError"] == true)
}

#[test]
fn isolated_door_end_to_end() {
    let node = lay_out_isolated();
    let gw = Gateway::start(&node.state);
    let info: Value = serde_json::from_str(&std::fs::read_to_string(node.state.join("gateway.json")).unwrap()).unwrap();
    let door = info["isolated_url"].as_str().expect("no isolated_url in gateway.json").to_string();
    let state = node.state.clone();
    let fail = |what: &str| -> String { format!("{what}\n--- gateway log ---\n{}", log(&state)) };

    // No token, or one the harness plugin does not know: 401 before any MCP.
    assert_eq!(rpc(&door, None, "initialize", json!({})).0, 401);
    assert_eq!(rpc(&door, Some("guess"), "initialize", json!({})).0, 401, "{}", fail("unknown token"));

    let (status, init) = rpc(&door, Some("good"), "initialize", json!({"protocolVersion": "2025-06-18", "capabilities": {}}));
    assert_eq!((status, &init["result"]["serverInfo"]["name"]), (200, &json!("awm")), "{}", fail("initialize"));
    assert_eq!(rpc(&door, Some("good"), "notifications/initialized", json!({})).0, 202);

    // tools/list shows only the whitelist under the ceiling: no harness-cc, no awm, no peers.
    let (_, list) = rpc(&door, Some("good"), "tools/list", json!({}));
    let tools = list["result"]["tools"].as_array().cloned().unwrap_or_default();
    let names: Vec<&str> = tools.iter().filter_map(|t| t["name"].as_str()).collect();
    assert_eq!(names, vec!["fake"], "{list}");
    let verbs = &tools[0]["inputSchema"]["properties"]["verb"]["enum"];
    assert_eq!(verbs, &json!(["echo", "scoped", "whoami", "describe"]));
    assert_eq!(tools[0]["inputSchema"]["properties"]["peer"]["enum"], json!(["local"]));

    let (text, err) = door_call(&door, "fake", json!({"verb": "echo", "args": {"text": "from the box"}}));
    assert_eq!((text.as_str(), err), ("from the box", false), "{}", fail("door echo"));
    let (text, err) = door_call(&door, "fake", json!({"verb": "describe"}));
    assert!(!err && text.contains("echo") && !text.contains("\"add\""), "describe leaks verbs: {text}");

    // The caller is the session the token names, untrusted, in its own scope.
    let (text, _) = door_call(&door, "fake", json!({"verb": "whoami"}));
    let who: Value = serde_json::from_str(&text).unwrap();
    assert_eq!(who["caller"]["session"], SESSION, "{who}");
    assert_eq!((&who["trust"], &who["scope"], &who["session_id"]), (&json!("untrusted"), &json!("awm/iso"), &json!(SESSION)));

    // Scope pinning: filled when absent, refused when it names another scope.
    let (text, err) = door_call(&door, "fake", json!({"verb": "scoped", "args": {"_caller_trust": "trusted"}}));
    let got: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
    assert!(!err && got["project"] == "awm" && got["scope"] == "iso" && got["_caller_trust"] == "untrusted", "{text}");
    for bad in [json!({"scope": "other"}), json!({"project": "cloud"})] {
        let (text, err) = door_call(&door, "fake", json!({"verb": "scoped", "args": bad}));
        assert!(err && text.contains("own scope awm/iso"), "{bad}: {text}");
    }

    // Off the whitelist, under the ceiling, or on a peer: refused.
    for (tool, args, why) in [
        ("fake", json!({"verb": "add", "args": {"a": 1, "b": 2}}), "whitelist"),
        ("harness-cc", json!({"verb": "caller_of_token", "args": {"token": "good"}}), "whitelist"),
        ("awm", json!({"verb": "status"}), "whitelist"),
        ("fake", json!({"verb": "echo", "args": {"text": "x"}, "peer": "elsewhere"}), "this node only"),
    ] {
        let (text, err) = door_call(&door, tool, args.clone());
        assert!(err && text.contains("403") && text.contains(why), "{tool} {args}: {text}");
    }

    // The door listener serves /mcp only. The loopback /invoke refuses a token.
    let invoke = http().post(format!("{door}/invoke")).json(&json!({"name": "fake", "args": {"verb": "echo", "args": {"text": "x"}}})).send().unwrap();
    assert_eq!(invoke.status().as_u16(), 404);
    let invoke = http()
        .post(format!("{}/invoke", gw.url))
        .header("X-Awm-Caller-Token", "good")
        .json(&json!({"name": "fake", "args": {"verb": "echo", "args": {"text": "x"}}}))
        .send()
        .unwrap();
    assert_eq!(invoke.status().as_u16(), 403);
    assert_eq!(http().get(format!("{door}/mcp")).header("X-Awm-Caller-Token", "good").send().unwrap().status().as_u16(), 405);

    // The loopback listener answers the same door.
    let (text, err) = door_call(&gw.url, "fake", json!({"verb": "echo", "args": {"text": "loopback"}}));
    assert_eq!((text.as_str(), err), ("loopback", false));
    assert!(gw.stop().success());
}
