//! The native Globus client: the status mapping the CLI's output gave, the transfer
//! document, submission ids, skipped-error paging, and the token store. Every token, id and
//! secret here is synthetic, and the auth server is a local socket.

mod common;

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::sync::{Arc, Mutex};

use awm_plugin_dvc::globus::{self, Submit, TokenStore, Tokens, NATIVE_CLIENT_ID, STATUS_FIELDS, TOKEN_FILE};
use serde_json::{json, Value};

// -- status ---------------------------------------------------------------------------------

#[test]
fn status_of_maps_empty_values_to_null_and_counters_to_ints() {
    let doc = json!({
        "status": "ACTIVE", "label": "", "files": "12", "files_transferred": 3, "files_skipped": null,
        "bytes_transferred": "4096", "effective_bytes_per_second": 17, "faults": "0",
        "nice_status": "None", "request_time": "2026-11-07T00:00:00+00:00", "completion_time": null,
    });

    let st = globus::status_of("task-1", &doc);

    assert_eq!(st["task_id"], "task-1");
    assert_eq!(st["status"], "ACTIVE");
    assert_eq!(st["label"], Value::Null);
    assert_eq!(st["nice_status"], Value::Null);
    assert_eq!(st["completion_time"], Value::Null);
    assert_eq!(st["files_skipped"], Value::Null);
    assert_eq!(st["files"], 12);
    assert_eq!(st["files_transferred"], 3);
    assert_eq!(st["bytes_transferred"], 4096);
    assert_eq!(st["faults"], 0);
    assert_eq!(st["request_time"], "2026-11-07T00:00:00+00:00");
    assert_eq!(st["done"], false);
    for f in STATUS_FIELDS {
        assert!(st.get(f).is_some(), "{f}");
    }
}

#[test]
fn a_missing_field_is_null() {
    let st = globus::status_of("t", &json!({"status": "ACTIVE"}));
    assert_eq!(st["files"], Value::Null);
    assert_eq!(st["label"], Value::Null);
}

#[test]
fn done_is_set_only_for_the_terminal_states() {
    for (status, done) in [("SUCCEEDED", true), ("FAILED", true), ("ACTIVE", false), ("INACTIVE", false), ("", false)] {
        assert_eq!(globus::status_of("t", &json!({"status": status}))["done"], done, "{status}");
    }
    assert_eq!(globus::status_of("t", &json!({}))["done"], false);
}

#[test]
fn task_status_reads_the_task_path() {
    let api = common::FakeApi::new(|_, path, _| Ok(json!({"status": "SUCCEEDED", "path_seen": path})));
    let st = globus::task_status(api.as_ref(), "task-1").unwrap();
    assert_eq!(st["done"], true);
    assert_eq!(api.calls()[0].path, "/task/task-1");
}

// -- the transfer document ------------------------------------------------------------------

fn opts(label: &str, rules: Option<Vec<Value>>) -> Submit<'_> {
    Submit {
        source: "src-ep",
        destination: "dst-ep",
        label,
        sync_level: 1,
        delete_destination_extra: false,
        skip_source_errors: true,
        filter_rules: rules,
    }
}

#[test]
fn the_document_clips_the_label_and_always_verifies_checksums() {
    let long = "x".repeat(200);
    let items = vec![json!({"DATA_TYPE": "transfer_item", "source_path": "/a", "destination_path": "/b", "recursive": false})];

    let d = globus::document("sub-1", &items, &opts(&long, None));

    assert_eq!(d["label"].as_str().unwrap().chars().count(), 128);
    assert_eq!(d["verify_checksum"], true);
    assert_eq!(d["DATA_TYPE"], "transfer");
    assert_eq!(d["submission_id"], "sub-1");
    assert_eq!(d["source_endpoint"], "src-ep");
    assert_eq!(d["destination_endpoint"], "dst-ep");
    assert_eq!(d["sync_level"], 1);
    assert_eq!(d["delete_destination_extra"], false);
    assert_eq!(d["skip_source_errors"], true);
    assert_eq!(d["DATA"], json!(items));
}

#[test]
fn a_multibyte_label_is_clipped_by_character() {
    let long = "é".repeat(130);
    let d = globus::document("s", &[], &opts(&long, None));
    assert_eq!(d["label"].as_str().unwrap(), "é".repeat(128));
}

#[test]
fn filter_rules_appear_only_when_there_are_some() {
    assert!(globus::document("s", &[], &opts("l", None)).get("filter_rules").is_none());
    assert!(globus::document("s", &[], &opts("l", Some(vec![]))).get("filter_rules").is_none());
    let rules = vec![json!({"DATA_TYPE": "filter_rule", "method": "exclude", "type": "dir", "name": "node_modules"})];
    assert_eq!(globus::document("s", &[], &opts("l", Some(rules.clone())))["filter_rules"], json!(rules));
}

#[test]
fn submit_fetches_a_fresh_submission_id_per_call() {
    let api = common::submitting_api();

    let a = globus::submit(api.as_ref(), &[], &opts("l", None)).unwrap();
    let b = globus::submit(api.as_ref(), &[], &opts("l", None)).unwrap();

    assert_eq!((a.as_str(), b.as_str()), ("task-1", "task-2"));
    let calls: Vec<(String, String)> = api.calls().iter().map(|c| (c.method.to_string(), c.path.clone())).collect();
    let want = [("GET", "/submission_id"), ("POST", "/transfer"), ("GET", "/submission_id"), ("POST", "/transfer")];
    assert_eq!(calls, want.map(|(m, p)| (m.to_string(), p.to_string())));
    let sids: Vec<Value> = api.posts().iter().map(|d| d["submission_id"].clone()).collect();
    assert_eq!(sids, [json!("sub-1"), json!("sub-2")]);
}

#[test]
fn a_submit_with_no_task_id_back_is_an_error() {
    let api = common::FakeApi::new(|_, path, _| Ok(if path == "/submission_id" { json!({"value": "sub-1"}) } else { json!({"code": "Accepted"}) }));
    let e = globus::submit(api.as_ref(), &[], &opts("l", None)).unwrap_err();
    assert!(e.0.contains("no task id"), "{}", e.0);
}

#[test]
fn a_missing_submission_id_stops_the_submit() {
    let api = common::FakeApi::new(|_, _, _| Ok(json!({})));
    assert!(globus::submit(api.as_ref(), &[], &opts("l", None)).is_err());
    assert!(api.posts().is_empty());
}

#[test]
fn skipped_errors_follows_next_marker_pages() {
    let api = common::FakeApi::new(|_, path, _| {
        Ok(match path {
            "/task/task-1/skipped_errors" => json!({"DATA": [{"source_path": "/a"}, {"source_path": "/b"}], "next_marker": "m2"}),
            "/task/task-1/skipped_errors?marker=m2" => json!({"DATA": [{"source_path": "/c"}], "next_marker": 3}),
            "/task/task-1/skipped_errors?marker=3" => json!({"DATA": [{"source_path": "/d"}], "next_marker": null}),
            other => panic!("unexpected {other}"),
        })
    });

    let all = globus::skipped_errors(api.as_ref(), "task-1").unwrap();

    let got: Vec<&str> = all.iter().map(|e| e["source_path"].as_str().unwrap()).collect();
    assert_eq!(got, ["/a", "/b", "/c", "/d"]);
    assert_eq!(api.calls().len(), 3);
}

#[test]
fn a_task_with_no_skips_is_one_empty_page() {
    let api = common::FakeApi::new(|_, _, _| Ok(json!({"DATA": []})));
    assert!(globus::skipped_errors(api.as_ref(), "t").unwrap().is_empty());
    assert_eq!(api.calls().len(), 1);
}

// -- the CLI's storage ----------------------------------------------------------------------

const TRANSFER_RS: &str = "transfer.api.globus.org";

/// A storage.db shaped like the globus CLI's, holding only synthetic values.
fn storage(dir: &Path, rows: &[(&str, &str, Value)], clients: &[(&str, Value)]) -> std::path::PathBuf {
    let path = dir.join("storage.db");
    let conn = rusqlite::Connection::open(&path).unwrap();
    conn.execute_batch(
        "CREATE TABLE token_storage (namespace TEXT, resource_server TEXT, token_data_json TEXT);
         CREATE TABLE config_storage (namespace TEXT, config_name TEXT, config_data_json TEXT);",
    )
    .unwrap();
    for (ns, rs, tok) in rows {
        conn.execute("INSERT INTO token_storage VALUES (?, ?, ?)", rusqlite::params![ns, rs, tok.to_string()]).unwrap();
    }
    for (ns, client) in clients {
        conn.execute("INSERT INTO config_storage VALUES (?, 'auth_client_data', ?)", rusqlite::params![ns, client.to_string()]).unwrap();
    }
    path
}

#[test]
fn import_reads_the_production_transfer_token_and_its_client() {
    let (_t, dir) = common::tempdir();
    let db = storage(
        &dir,
        &[
            ("userprofile/other", TRANSFER_RS, json!({"refresh_token": "rt-other"})),
            ("userprofile/production", "auth.globus.org", json!({"refresh_token": "rt-auth"})),
            ("userprofile/production", TRANSFER_RS, json!({"refresh_token": "rt-synthetic", "access_token": "at-synthetic", "expires_at_seconds": 1234})),
        ],
        &[("userprofile/production", json!({"client_id": "cid-synthetic", "client_secret": "secret-synthetic"}))],
    );

    let t = globus::import_from_cli(&db).unwrap();

    assert_eq!(t.refresh_token, "rt-synthetic");
    assert_eq!(t.access_token, "at-synthetic");
    assert_eq!(t.expires_at_seconds, 1234.0);
    assert_eq!(t.client_id, "cid-synthetic");
    assert_eq!(t.client_secret.as_deref(), Some("secret-synthetic"));
    assert!(t.source.contains("storage.db"));
}

#[test]
fn import_without_a_client_record_uses_the_native_app() {
    let (_t, dir) = common::tempdir();
    let db = storage(&dir, &[("userprofile/production", TRANSFER_RS, json!({"refresh_token": "rt-synthetic"}))], &[]);

    let t = globus::import_from_cli(&db).unwrap();

    assert_eq!(t.client_id, NATIVE_CLIENT_ID);
    assert_eq!(t.client_secret, None);
    assert_eq!(t.access_token, "");
}

#[test]
fn import_refuses_a_storage_with_no_usable_transfer_token() {
    let (_t, dir) = common::tempdir();
    assert!(globus::import_from_cli(&dir.join("absent.db")).unwrap_err().0.contains("no globus CLI login"));
    let db = storage(&dir, &[("userprofile/production", "auth.globus.org", json!({"refresh_token": "rt"}))], &[]);
    assert!(globus::import_from_cli(&db).unwrap_err().0.contains("holds no"));
    std::fs::remove_file(&db).unwrap();
    let db = storage(&dir, &[("userprofile/production", TRANSFER_RS, json!({"access_token": "at"}))], &[]);
    assert!(globus::import_from_cli(&db).unwrap_err().0.contains("no refresh token"));
}

// -- refresh against a local auth server ----------------------------------------------------

#[derive(Debug, Default, Clone)]
struct Seen {
    request_line: String,
    headers: Vec<(String, String)>,
    body: String,
}

impl Seen {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(k, _)| k.eq_ignore_ascii_case(name)).map(|(_, v)| v.as_str())
    }
    fn form(&self) -> Vec<(String, String)> {
        self.body
            .split('&')
            .filter(|kv| !kv.is_empty())
            .map(|kv| {
                let (k, v) = kv.split_once('=').unwrap_or((kv, ""));
                (k.to_string(), v.to_string())
            })
            .collect()
    }
}

/// Answer one token request with `reply` and keep what was asked.
fn auth_server(reply: Value) -> (String, Arc<Mutex<Option<Seen>>>, std::thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let seen: Arc<Mutex<Option<Seen>>> = Arc::default();
    let out = seen.clone();
    let h = std::thread::spawn(move || {
        let (stream, _) = listener.accept().unwrap();
        let mut r = BufReader::new(stream.try_clone().unwrap());
        let mut s = Seen::default();
        r.read_line(&mut s.request_line).unwrap();
        let mut len = 0;
        loop {
            let mut line = String::new();
            r.read_line(&mut line).unwrap();
            let line = line.trim_end();
            if line.is_empty() {
                break;
            }
            let (k, v) = line.split_once(':').unwrap();
            if k.eq_ignore_ascii_case("content-length") {
                len = v.trim().parse().unwrap();
            }
            s.headers.push((k.trim().to_string(), v.trim().to_string()));
        }
        let mut body = vec![0u8; len];
        r.read_exact(&mut body).unwrap();
        s.body = String::from_utf8(body).unwrap();
        *out.lock().unwrap() = Some(s);
        let text = reply.to_string();
        let mut w = stream;
        write!(w, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{text}", text.len()).unwrap();
        w.flush().unwrap();
    });
    (url, seen, h)
}

fn store(state: &Path, auth_url: &str, cli: &Path) -> TokenStore {
    let mut s = TokenStore::new(state);
    s.auth_url = auth_url.to_string();
    s.cli_storage = cli.to_path_buf();
    s
}

fn reply() -> Value {
    json!({"access_token": "at-fresh", "expires_in": 3600, "resource_server": TRANSFER_RS, "refresh_token": "rt-rotated", "token_type": "Bearer"})
}

#[test]
fn a_confidential_client_refreshes_with_basic_auth_and_the_file_is_private() {
    let (_t, dir) = common::tempdir();
    let state = dir.join("state");
    let db = storage(
        &dir,
        &[("userprofile/production", TRANSFER_RS, json!({"refresh_token": "rt-synthetic", "access_token": "at-stale", "expires_at_seconds": 1}))],
        &[("userprofile/production", json!({"client_id": "cid-synthetic", "client_secret": "secret-synthetic"}))],
    );
    let (url, seen, h) = auth_server(reply());
    let s = store(&state, &url, &db);

    let token = s.access_token(false).unwrap();
    h.join().unwrap();

    assert_eq!(token, "at-fresh");
    let seen = seen.lock().unwrap().clone().unwrap();
    assert!(seen.request_line.starts_with("POST /v2/oauth2/token "), "{}", seen.request_line);
    use base64::Engine;
    let basic = base64::engine::general_purpose::STANDARD.encode("cid-synthetic:secret-synthetic");
    assert_eq!(seen.header("authorization"), Some(format!("Basic {basic}").as_str()));
    let form = seen.form();
    assert!(form.contains(&("grant_type".into(), "refresh_token".into())), "{form:?}");
    assert!(form.contains(&("refresh_token".into(), "rt-synthetic".into())), "{form:?}");
    assert!(!form.iter().any(|(k, _)| k == "client_id"), "{form:?}");

    let file = state.join(TOKEN_FILE);
    assert_eq!(std::fs::metadata(&file).unwrap().permissions().mode() & 0o777, 0o600);
    let saved: Tokens = serde_json::from_str(&std::fs::read_to_string(&file).unwrap()).unwrap();
    assert_eq!(saved.access_token, "at-fresh");
    assert_eq!(saved.refresh_token, "rt-rotated");
    assert_eq!(saved.client_secret.as_deref(), Some("secret-synthetic"));
    assert!(saved.expires_at_seconds > awm_plugin_dvc::now());
}

#[test]
fn a_native_client_refreshes_with_its_id_in_the_form() {
    let (_t, dir) = common::tempdir();
    let state = dir.join("state");
    let (url, seen, h) = auth_server(json!({"access_token": "at-native", "expires_in": 60}));
    let s = store(&state, &url, &dir.join("no-cli.db"));
    std::fs::create_dir_all(&state).unwrap();
    s.write(&Tokens {
        client_id: "cid-native".into(),
        client_secret: None,
        refresh_token: "rt-native".into(),
        access_token: String::new(),
        expires_at_seconds: 0.0,
        source: "test".into(),
    })
    .unwrap();

    assert_eq!(s.access_token(false).unwrap(), "at-native");
    h.join().unwrap();

    let seen = seen.lock().unwrap().clone().unwrap();
    assert_eq!(seen.header("authorization"), None);
    let form = seen.form();
    assert!(form.contains(&("client_id".into(), "cid-native".into())), "{form:?}");
    assert!(form.contains(&("refresh_token".into(), "rt-native".into())), "{form:?}");
    let file = state.join(TOKEN_FILE);
    assert_eq!(std::fs::metadata(&file).unwrap().permissions().mode() & 0o777, 0o600);
    let saved: Tokens = serde_json::from_str(&std::fs::read_to_string(&file).unwrap()).unwrap();
    assert_eq!(saved.access_token, "at-native");
    // No rotated refresh token came back, so the old one stays.
    assert_eq!(saved.refresh_token, "rt-native");
}

#[test]
fn a_live_token_is_used_without_a_refresh() {
    let (_t, dir) = common::tempdir();
    let state = dir.join("state");
    std::fs::create_dir_all(&state).unwrap();
    // Port 9 (discard) is never served here: a refresh would fail the test.
    let s = store(&state, "http://127.0.0.1:9", &dir.join("no-cli.db"));
    s.write(&Tokens {
        client_id: "cid".into(),
        client_secret: None,
        refresh_token: "rt".into(),
        access_token: "at-live".into(),
        expires_at_seconds: awm_plugin_dvc::now() + 3600.0,
        source: "test".into(),
    })
    .unwrap();

    assert_eq!(s.access_token(false).unwrap(), "at-live");
}

#[test]
fn ensure_imported_copies_the_cli_login_once() {
    let (_t, dir) = common::tempdir();
    let state = dir.join("state");
    let db = storage(&dir, &[("userprofile/production", TRANSFER_RS, json!({"refresh_token": "rt-synthetic"}))], &[]);
    let s = store(&state, "http://127.0.0.1:9", &db);

    assert!(s.ensure_imported().unwrap());
    assert!(!s.ensure_imported().unwrap());
    let file = state.join(TOKEN_FILE);
    assert_eq!(std::fs::metadata(&file).unwrap().permissions().mode() & 0o777, 0o600);
    assert_eq!(s.read().unwrap().unwrap().refresh_token, "rt-synthetic");
}
