//! The public door's verdicts, against Python's (`vectors/policy.json`), and the pure tests of
//! httpsfront's `test_public_policy`, `test_vault_paths`, `test_slice_paths`, `test_tether_paths`
//! and `test_board_door`.

use awm_plugin_auth::policy::{self, board, classify, slices, tether, vault, Verdict};
use base64::Engine;
use serde_json::{json, Value};

fn vectors() -> Value {
    serde_json::from_str(include_str!("vectors/policy.json")).unwrap()
}

fn opt_str(v: &Value) -> Option<&str> {
    v.as_str()
}

fn unhex(v: &Value) -> Option<Vec<u8>> {
    v.as_str().map(|s| hex::decode(s).unwrap())
}

#[test]
fn every_path_gets_pythons_verdicts() {
    let v = vectors();
    let subs: Vec<Option<&str>> = v["subs"].as_array().unwrap().iter().map(opt_str).collect();
    let rows = v["paths"].as_array().unwrap();
    assert!(rows.len() >= 200);
    for row in rows {
        let path = row["path"].as_str().unwrap();
        assert_eq!(classify(path).as_str(), row["verdict"], "{path:?}");
        for (sub, want) in subs.iter().zip(row["allows"].as_array().unwrap()) {
            assert_eq!(policy::allows(path, *sub), want.as_bool().unwrap(), "{path:?} as {sub:?}");
        }
        assert_eq!(vault::owns(path), row["vault_owns"], "{path:?}");
        assert_eq!(vault::upstream_path(path), row["vault_upstream"].as_str().unwrap(), "{path:?}");
        assert_eq!(slices::token_of(path), row["slices_token"].as_str(), "{path:?}");
        assert_eq!(slices::owns(path), row["slices_owns"], "{path:?}");
        assert_eq!(slices::refused(path), row["slices_refused"], "{path:?}");
        assert_eq!(slices::upstream_path(path), row["slices_upstream"].as_str().unwrap(), "{path:?}");
        assert_eq!(tether::owns(path), row["tether_owns"], "{path:?}");
        assert_eq!(tether::refused(path), row["tether_refused"], "{path:?}");
        assert_eq!(tether::upstream_path(path), row["tether_upstream"].as_str().unwrap(), "{path:?}");
        assert_eq!(board::owns(path), row["board_owns"], "{path:?}");
        assert_eq!(board::in_mount(path), row["board_in_mount"], "{path:?}");
    }
}

#[test]
fn every_raw_target_is_stripped_as_python_strips_it() {
    for row in vectors()["raw"].as_array().unwrap() {
        let raw = unhex(&row["raw"]).unwrap();
        assert_eq!(vault::upstream_raw_path(&raw), unhex(&row["vault"]), "vault {raw:?}");
        assert_eq!(slices::upstream_raw_path(&raw), unhex(&row["slices"]), "slices {raw:?}");
        assert_eq!(tether::upstream_raw_path(&raw), unhex(&row["tether"]), "tether {raw:?}");
        assert_eq!(board::upstream_raw_path(&raw), unhex(&row["board"]), "board {raw:?}");
    }
}

#[test]
fn every_board_method_gets_pythons_answer_and_cap() {
    for row in vectors()["board"].as_array().unwrap() {
        let (m, p) = (row["method"].as_str().unwrap(), row["path"].as_str().unwrap());
        assert_eq!(board::allows(m, p), row["allows"], "{m} {p:?}");
        assert_eq!(board::body_cap(m, p) as u64, row["body_cap"].as_u64().unwrap(), "{m} {p:?}");
    }
}

#[test]
fn every_invoke_body_gets_pythons_answer() {
    for row in vectors()["invoke"].as_array().unwrap() {
        let body = base64::engine::general_purpose::STANDARD.decode(row["body"].as_str().unwrap()).unwrap();
        assert_eq!(policy::invoke_allows(&body), row["allows"], "{:?}", String::from_utf8_lossy(&body));
    }
}

#[test]
fn the_constants_are_pythons() {
    let v = vectors();
    let c = &v["constants"];
    let mut exact: Vec<&str> = policy::OPEN_EXACT.to_vec();
    exact.sort_unstable();
    assert_eq!(json!(exact), c["open_exact"]);
    assert_eq!(json!(policy::OPEN_PREFIXES), c["open_prefixes"]);
    let mut fns = policy::TRILIUM_OPEN_FNS.to_vec();
    fns.sort_unstable();
    assert_eq!(json!(fns), c["trilium_open_fns"]);
    assert_eq!(policy::INVOKE_BODY_CAP as u64, c["invoke_body_cap"].as_u64().unwrap());
    let table = |t: &[(&str, &str)]| Value::Object(t.iter().map(|(k, v)| (k.to_string(), json!(v))).collect());
    assert_eq!(table(vault::NOT_FORWARDED), c["vault_not_forwarded"]);
    assert_eq!(table(slices::NOT_FORWARDED), c["slices_not_forwarded"]);
    assert_eq!(table(tether::NOT_FORWARDED), c["tether_not_forwarded"]);
    let all: Vec<&str> = slices::all_not_forwarded().iter().map(|(k, _)| *k).collect();
    assert_eq!(json!(all), c["slices_all_not_forwarded"]);
    assert_eq!(board::MAX_BODY as u64, c["board_max_body"].as_u64().unwrap());
    assert_eq!(board::MAX_FILE_BODY as u64, c["board_max_file_body"].as_u64().unwrap());
    assert_eq!(vault::manifest(), c["vault_manifest"]);
}

// --- test_public_policy -------------------------------------------------------------------

const DENIED: &[&str] = &[
    "/hub/services", "/hub/plugins", "/tools", "/svc/scopes/fn/scope_search",
    "/svc/auth/fn/password", "/svc/auth/fn/verify",
    "/__auth/link", "/__auth/login", "/ca.crt", "/ca.pem",
    "/ui/agents/", "/p/dashboard/dashboard/", "/p/httpsfront/login/",
    "/files/", "/s/fileviewer/", "/s/fileviewer/projects/awm/dev/x",
    "/s/claude-science/mcp",
    "/__landing/tags",
    "/ui/notes/", "/svc/notes/fn/list", "/svc/notes/emit/note:tony:1",
    "/ui/drawio/", "/drawio-app/index.html", "/svc/drawio/fn/save",
    "/svc/drawio/emit/drawio:tony:x.drawio",
    "/etapi/app-info", "/custom/x", "/mcp", "/share/abc",
    "/login", "/setup", "/set-password", "/robots.txt",
    "/ui/trilium", "/ui/trilium/", "/svc/trilium/fn/status",
    "/svc/trilium/fn/restore",
    "/p/trilium/triliumXYZ", "/p/trilium/trilium-admin/x", "/p/trilium/",
];
const ALLOWED: &[&str] = &["/p/trilium/trilium", "/p/trilium/trilium/", "/p/trilium/trilium/assets/index.js"];

#[test]
fn test_denied_paths_are_404_signed_in_or_not() {
    for path in DENIED {
        assert_eq!(classify(path), Verdict::Deny, "{path}");
        for sub in [Some("tony"), None] {
            assert!(!policy::allows(path, sub), "{path} {sub:?}");
        }
    }
    // `/invoke` is decided by its body: a GET has none.
    assert_eq!(classify("/invoke"), Verdict::Invoke);
    assert!(!policy::invoke_allows(b""));
}

#[test]
fn test_allowed_paths_proxy_when_signed_in() {
    for path in ALLOWED {
        assert!(policy::allows(path, Some("tony")), "{path}");
    }
}

fn call(verb: &str) -> Value {
    json!({"name": "trilium", "args": {"verb": verb, "args": {}}})
}

#[test]
fn test_public_invoke_admits_the_trilium_pages_read_verbs() {
    for verb in policy::TRILIUM_OPEN_FNS {
        assert!(policy::invoke_allows(call(verb).to_string().as_bytes()), "{verb}");
    }
}

#[test]
fn test_public_invoke_refuses_every_other_call() {
    let mut with_peer = call("status");
    with_peer["args"]["peer"] = json!("mira");
    let bodies = [
        call("restore"), call("stop"), call("logs"), call("export"), call("slice_resolve"), with_peer,
        json!({"name": "trilium_status", "args": {}}),
        json!({"name": "journal", "args": {"verb": "status", "args": {}}}),
        json!({"name": "trilium", "args": {"verb": "status", "args": []}}),
        json!({"name": "trilium", "args": {"verb": "status"}, "peer": "mira"}),
        json!([]), json!("status"),
    ];
    for body in bodies {
        assert!(!policy::invoke_allows(body.to_string().as_bytes()), "{body}");
    }
}

#[test]
fn test_public_invoke_refuses_a_body_that_is_not_json() {
    assert!(!policy::invoke_allows(b"{not json"));
}

#[test]
fn test_user_scoped_paths_admit_only_their_user() {
    let tony = Some("tony");
    assert!(policy::allows("/s/fileviewer/projects/userdata/tony/notes/a.md", tony));
    assert!(!policy::allows("/s/fileviewer/projects/userdata/steven/notes/a.md", tony));
    assert!(policy::allows("/s/fileviewer/projects/userdata/tony", tony));
    assert!(!policy::allows("/files/projects/userdata/tony/notes/a.md", tony));
}

#[test]
fn test_no_emit_topic_is_public() {
    assert!(!policy::allows("/svc/drawio/emit/drawio:tony:x.drawio", Some("tony")));
    assert!(!policy::allows("/svc/notes/emit/note:tony:abc", Some("tony")));
}

// --- test_vault_paths ---------------------------------------------------------------------

#[test]
fn test_the_vault_owns_everything_under_its_mount() {
    for inner in [
        "", "bootstrap", "favicon.ico", "icon.png", "api/tree", "api/notes/abc123", "assets/v0.105.0/src/index.js",
        "assets/index-abc.js", "src/index-Dso57yoQ.js", "node_modules/@excalidraw/excalidraw/dist/prod/x.js", "pdfjs/viewer.html",
    ] {
        let path = format!("{}{inner}", vault::SHELL);
        assert!(vault::owns(&path), "{path}");
        assert_eq!(classify(&path), Verdict::Vault);
    }
}

#[test]
fn test_deliberately_unforwarded_routes_are_not_the_vaults() {
    for (inner, _) in vault::NOT_FORWARDED {
        let path = format!("{}{}", vault::SHELL, inner.trim_start_matches('/'));
        assert!(!vault::owns(&path), "{path}");
        assert!(!vault::owns(&format!("{}/x", path.trim_end_matches('/'))));
        assert_eq!(classify(&path), Verdict::Deny);
    }
}

#[test]
fn test_the_root_level_names_are_awms_again() {
    for path in ["/api/tree", "/src/index.js", "/assets/x.css", "/favicon.ico", "/bootstrap", "/icon.png", "/robots.txt"] {
        assert!(!vault::owns(path), "{path}");
    }
}

#[test]
fn test_the_vault_and_awm_do_not_overlap() {
    for a in [
        "/p/", "/s/", "/__auth/", "/__landing/", "/hub/", "/invoke", "/tools", "/ca.crt", "/ca.pem", "/robots.txt", "/api/",
        "/assets/", "/src/", "/favicon.ico", "/bootstrap",
    ] {
        assert!(!vault::PREFIX.starts_with(a), "the vault falls inside awm's {a}");
        assert!(!a.starts_with(vault::PREFIX), "awm path {a} falls inside the vault");
    }
}

#[test]
fn test_the_management_page_is_not_the_application() {
    assert!(!vault::owns(policy::TRILIUM_PAGE));
    assert!(!vault::owns(&format!("{}/", policy::TRILIUM_PAGE)));
    assert_eq!(classify(policy::TRILIUM_PAGE), Verdict::Open);
}

#[test]
fn test_root_is_never_the_vaults() {
    assert!(!vault::owns("/"));
    assert_eq!(classify("/"), Verdict::Open);
}

#[test]
fn test_the_mount_is_stripped_and_nothing_else_is() {
    assert_eq!(vault::upstream_path(vault::SHELL), "/");
    assert_eq!(vault::upstream_path("/trilium/api/tree"), "/api/tree");
    for path in ["/", "/p/trilium/trilium/", "/invoke"] {
        assert_eq!(vault::upstream_path(path), path);
    }
}

#[test]
fn test_the_bare_name_is_owned_so_it_can_be_redirected() {
    assert!(vault::owns(vault::SHELL_BARE));
    assert_eq!(classify(vault::SHELL_BARE), Verdict::Vault);
}

#[test]
fn test_the_raw_mount_must_be_in_the_bytes_too() {
    assert_eq!(vault::upstream_raw_path(b"/trilium/api/search/%23x").as_deref(), Some(b"/api/search/%23x".as_slice()));
    assert_eq!(vault::upstream_raw_path(b"/trilium/").as_deref(), Some(b"/".as_slice()));
    assert_eq!(vault::upstream_raw_path(b"/trilium").as_deref(), Some(b"/".as_slice()));
    assert_eq!(vault::upstream_raw_path(b"/%74rilium/api/tree"), None);
}

#[test]
fn test_the_manifest_launches_the_vault_not_the_host() {
    let m = vault::manifest();
    assert_eq!(m["start_url"], vault::SHELL);
    assert_eq!(m["scope"], vault::SHELL);
}

#[test]
fn test_a_peer_bearer_is_not_a_person() {
    assert!(policy::allows(vault::SHELL, Some("tony")));
    for sub in [Some("peer"), Some("peer:mira"), Some("peer:shaula"), Some("operator"), None] {
        assert!(!policy::allows(vault::SHELL, sub), "{sub:?}");
    }
}

// --- test_slice_paths ---------------------------------------------------------------------

const TOKEN: &str = "Ab3-_9xYzQw012345678";

fn sp(inner: &str) -> String {
    format!("{}{TOKEN}{inner}", slices::PREFIX)
}

#[test]
fn test_the_token_is_the_segment_after_the_mount() {
    assert_eq!(slices::token_of(&sp("/api/tree")), Some(TOKEN));
    assert_eq!(slices::token_of(&sp("/")), Some(TOKEN));
    assert_eq!(slices::token_of(&slices::shell_bare(TOKEN)), Some(TOKEN));
}

#[test]
fn test_a_segment_that_is_not_token_shaped_names_no_slice() {
    let long = format!("/slice/{}", "x".repeat(200));
    for bad in ["/slice/", "/slice/short", "/slice/has.a.dot0123456789", long.as_str()] {
        assert_eq!(slices::token_of(bad), None);
        assert!(!slices::owns(bad));
        assert_eq!(classify(bad), Verdict::Deny);
    }
}

#[test]
fn test_the_mount_and_the_token_are_stripped_and_nothing_else_is() {
    assert_eq!(slices::upstream_path(&sp("/")), "/");
    assert_eq!(slices::upstream_path(&slices::shell_bare(TOKEN)), "/");
    assert_eq!(slices::upstream_path(&sp("/api/tree")), "/api/tree");
    assert_eq!(slices::upstream_path("/trilium/api/tree"), "/trilium/api/tree");
}

#[test]
fn test_the_raw_bytes_carry_the_mount_or_the_target_is_refused() {
    assert_eq!(slices::upstream_raw_path(sp("/api/search/%23foo").as_bytes()).as_deref(), Some(b"/api/search/%23foo".as_slice()));
    assert_eq!(slices::upstream_raw_path(slices::shell_bare(TOKEN).as_bytes()).as_deref(), Some(b"/".as_slice()));
    assert_eq!(slices::upstream_raw_path(format!("/%73lice/{TOKEN}").as_bytes()), None);
    assert_eq!(slices::upstream_raw_path(b"/trilium/api/tree"), None);
}

#[test]
fn test_a_slice_inherits_every_refusal_the_vault_records() {
    let all = slices::all_not_forwarded();
    for (entry, _) in vault::NOT_FORWARDED {
        assert!(all.iter().any(|(e, _)| e == entry));
        let inner = if entry.ends_with('/') { format!("{entry}x") } else { entry.to_string() };
        assert!(!slices::owns(&sp(&inner)), "{inner}");
    }
}

#[test]
fn test_a_slice_refuses_what_only_an_account_holder_could_want() {
    assert!(!slices::owns(&sp("/manifest.webmanifest")));
    assert!(!slices::owns(&sp("/robots.txt")));
    assert!(slices::owns(&sp("/")));
    assert!(slices::owns(&sp("/api/tree")));
    assert!(slices::owns(&sp("/icon.png")));
}

#[test]
fn test_the_door_admits_a_slice_with_no_session_at_all() {
    assert_eq!(classify(&sp("/")), Verdict::Slice);
    assert!(policy::allows(&sp("/"), None));
    assert!(policy::allows(&sp("/api/tree"), None));
}

#[test]
fn test_the_door_still_refuses_the_paths_inside_a_slice_that_it_refuses() {
    assert_eq!(classify(&sp("/etapi/app-info")), Verdict::Deny);
    assert!(!policy::allows(&sp("/etapi/app-info"), None));
    assert!(!policy::allows(&sp("/etapi/app-info"), Some("tony")));
}

#[test]
fn test_the_vault_is_untouched_by_the_new_verdict() {
    assert_eq!(classify(vault::SHELL), Verdict::Vault);
    assert!(!policy::allows(vault::SHELL, None));
    assert!(policy::allows(vault::SHELL, Some("tony")));
}

// --- test_tether_paths --------------------------------------------------------------------

const TICKET: &str = "0123456789abcdef0123456789abcdef";

#[test]
fn test_the_mount_claims_the_relays_whole_public_surface() {
    let claimed = [
        "/tether".to_string(), "/tether/win".into(), "/tether/bin/tether".into(), "/tether/bin/tether-windows-x86_64.exe".into(),
        "/tether/bin/tether-macos-arm64".into(), "/tether/claim/1".into(), "/tether/claim/999".into(),
        format!("/tether/join/7/{TICKET}"), format!("/tether/release/7/{TICKET}"), format!("/tether/hold/7/{TICKET}"),
        format!("/tether/poll/7/{TICKET}"), format!("/tether/seat/{TICKET}/recv"), format!("/tether/seat/{TICKET}/send"),
        format!("/tether/seat/{TICKET}/close"), "/room/acre-anchor-kettle".into(), "/room/acre-anchor-kettle/send".into(),
        "/room/acre-anchor-kettle/recv".into(), "/room/acre-anchor-kettle/ws".into(), "/room/acre-anchor-kettle/leave".into(),
        "/room/acre-anchor-kettle-abbey-otter-lamp".into(), "/room/acre-anchor-kettle-abbey-otter-lamp/recv".into(),
    ];
    for path in &claimed {
        assert!(tether::owns(path), "{path}");
        assert!(!tether::refused(path), "{path}");
        assert_eq!(classify(path), Verdict::Tether, "{path}");
        assert!(policy::allows(path, None), "{path}");
    }
}

#[test]
fn test_a_near_miss_inside_the_mount_is_refused_rather_than_forwarded() {
    let t = TICKET;
    let t31 = &TICKET[..31];
    let up = TICKET.to_uppercase();
    let misses = [
        "/tether/health".to_string(), "/tether/".into(), "/tether/claim/0".into(), "/tether/claim/07".into(),
        "/tether/claim/1000".into(), "/tether/claim/7/extra".into(), "/tether/claim".into(), format!("/tether/join/7/{t31}"),
        format!("/tether/join/7/{up}"), "/tether/join/7".into(), format!("/tether/release/7/{t31}"), "/tether/release/7".into(),
        format!("/tether/release/0/{t}"), "/tether/hold/7".into(), format!("/tether/hold/7/{t}/extra"), format!("/tether/poll/0/{t}"),
        format!("/tether/poll/7/{t31}"), format!("/tether/seat/{t}"), format!("/tether/seat/{t}/join"), format!("/tether/seat/{up}/recv"),
        "/tether/seat/7/recv".into(), "/tether/bin/../../etc/passwd".into(), "/tether/bin/".into(), "/tether/win/".into(),
        "/tether/windows".into(), "/tether/invoke".into(), "/room/acre-anchor".into(), "/room/acre-anchor-kettle-acre".into(),
        "/room/acre-anchor-kettle-acre-anchor".into(), "/room/acre-anchor-kettle-acre-anchor-kettle-acre".into(),
        "/room/Acre-anchor-kettle".into(), "/room/acre_anchor_kettle".into(), "/room/ab-anchor-kettle".into(),
        "/room/acre-anchor-kettle/".into(), "/room/acre-anchor-kettle/close".into(), "/room/".into(),
    ];
    for path in &misses {
        assert!(!tether::owns(path), "{path}");
        assert!(tether::refused(path), "{path}");
        assert_eq!(classify(path), Verdict::Deny, "{path}");
        assert!(!policy::allows(path, None));
        assert!(!policy::allows(path, Some("tony")));
    }
}

#[test]
fn test_a_name_that_merely_starts_the_same_is_not_this_mounts() {
    for path in ["/tether-admin", "/tetherfoo", "/p/tether/tether", "/rooms", "/roomy", "/p/room/room"] {
        assert!(!tether::owns(path), "{path}");
        assert!(!tether::refused(path), "{path}");
    }
}

#[test]
fn test_the_mount_comes_off_the_same_way_in_text_and_in_bytes() {
    let cases = [
        ("/tether".to_string(), "/".to_string()),
        ("/tether/claim/7".into(), "/claim/7".into()),
        (format!("/tether/seat/{TICKET}/recv"), format!("/seat/{TICKET}/recv")),
        (format!("/tether/join/7/{TICKET}"), format!("/join/7/{TICKET}")),
        ("/tether/bin/tether".into(), "/bin/tether".into()),
        ("/tether/win".into(), "/win".into()),
        ("/room/acre-anchor-kettle/recv".into(), "/room/acre-anchor-kettle/recv".into()),
    ];
    for (path, inner) in cases {
        assert_eq!(tether::upstream_path(&path), inner);
        assert_eq!(tether::upstream_raw_path(path.as_bytes()), Some(inner.into_bytes()));
    }
}

#[test]
fn test_the_bare_mount_is_the_launcher_and_that_is_why_the_relay_serves_root() {
    assert_eq!(tether::upstream_path(tether::PREFIX), "/");
}

#[test]
fn test_a_mount_that_is_only_there_after_decoding_is_not_the_mount() {
    assert_eq!(tether::upstream_raw_path(b"/%74ether/claim/7"), None);
}

#[test]
fn test_every_route_we_decline_to_forward_is_named_with_its_reason() {
    for (path, reason) in tether::NOT_FORWARDED {
        assert!(!reason.is_empty(), "{path}");
        assert!(tether::refused(&format!("{}{path}", tether::PREFIX)), "{path}");
    }
    assert!(tether::refused(tether::ROOMS));
}

#[test]
fn test_the_bearers_routes_stay_on_the_relays_host() {
    for path in ["/tether/issue", "/tether/status", "/room"] {
        assert!(!tether::owns(path));
        assert!(tether::refused(path));
        assert_eq!(classify(path), Verdict::Deny);
    }
}

// --- test_board_door (the pure parts) -----------------------------------------------------

const CARD: &str = TICKET;

fn board_shapes() -> Vec<(&'static str, String)> {
    vec![
        ("POST", "/board/cards".into()), ("GET", "/board/cards".into()), ("GET", format!("/board/cards/{CARD}")),
        ("POST", format!("/board/cards/{CARD}/claim")), ("POST", format!("/board/cards/{CARD}/complete")),
        ("POST", format!("/board/cards/{CARD}/fail")), ("POST", format!("/board/cards/{CARD}/status")),
        ("POST", format!("/board/cards/{CARD}/reject")), ("POST", format!("/board/cards/{CARD}/messages")),
        ("GET", format!("/board/cards/{CARD}/messages")), ("GET", format!("/board/cards/{CARD}/events")),
        ("POST", format!("/board/cards/{CARD}/files")), ("GET", format!("/board/cards/{CARD}/files")),
        ("GET", format!("/board/cards/{CARD}/files/{CARD}")), ("GET", "/board/stream".into()),
    ]
}

#[test]
fn test_the_policy_verdicts() {
    for (m, path) in board_shapes() {
        assert_eq!(classify(&path), Verdict::Board, "{path}");
        assert!(policy::allows(&path, None));
        assert!(board::allows(m, &path), "{m} {path}");
    }
    let bad = [
        "/board".to_string(), "/board/".into(), "/board/cards/".into(), "/board/stream/".into(), "/board/health".into(),
        "/board/parties".into(), "/board/cards/short".into(), format!("/board/cards/{}", &CARD[..31]), format!("/board/cards/{CARD}0"),
        format!("/board/cards/{}", CARD.to_uppercase()), "/board/cards/01234567-89ab-cdef-0123-456789abcdef".into(),
        format!("/board/cards/{}", "g".repeat(32)), format!("/board/cards/{CARD}/release"), format!("/board/cards/{CARD}/claim/extra"),
        format!("/board/cards/{CARD}/messages/1"), format!("/board/cards/{CARD}/"), "/board//cards".into(),
        format!("/board/cards/{CARD}\n"), format!("/board/cards/{CARD}/files/../../stream"), format!("/board/cards/{CARD}/files/{CARD}/x"),
    ];
    for path in &bad {
        assert!(!board::owns(path), "{path:?}");
        assert_eq!(classify(path), Verdict::Deny, "{path:?}");
        assert!(!policy::allows(path, Some("tony")), "{path:?}");
    }
}

#[test]
fn test_a_wrong_method_on_a_claimed_path_is_refused() {
    let shapes = board_shapes();
    for (m, path) in &shapes {
        let wrong: &[&str] = if *m == "GET" { &["POST", "PUT", "DELETE"] } else { &["PUT", "DELETE", "PATCH"] };
        for other in wrong {
            if shapes.iter().any(|(m2, p2)| m2 == other && p2 == path) {
                continue;
            }
            assert!(!board::allows(other, path), "{other} {path}");
        }
    }
    assert!(!board::allows("HEAD", "/board/cards"));
    assert!(!board::allows("OPTIONS", "/board/cards"));
}

#[test]
fn test_the_shape_helpers_agree_with_each_other() {
    assert!(board::owns(&format!("/board/cards/{CARD}")));
    assert!(!board::owns(&format!("/board/cards/{CARD}\n")));
    assert!(board::allows("get", "/board/stream"));
    assert!(!board::allows("POST", "/board/stream"));
    assert!(board::in_mount("/board") && board::in_mount("/board/x"));
    assert!(!board::in_mount("/boards") && !board::in_mount("/boarding/x"));
    assert_eq!(board::upstream_raw_path(b"/board/cards").as_deref(), Some(b"/board/cards".as_slice()));
    assert_eq!(board::upstream_raw_path(b"/board/c%61rds"), None);
}

#[test]
fn test_a_file_upload_has_the_larger_cap_and_nothing_else_does() {
    assert_eq!(board::body_cap("POST", &format!("/board/cards/{CARD}/files")), board::MAX_FILE_BODY);
    assert_eq!(board::body_cap("post", &format!("/board/cards/{CARD}/files")), board::MAX_FILE_BODY);
    assert_eq!(board::body_cap("GET", &format!("/board/cards/{CARD}/files")), board::MAX_BODY);
    assert_eq!(board::body_cap("POST", &format!("/board/cards/{CARD}/messages")), board::MAX_BODY);
}
