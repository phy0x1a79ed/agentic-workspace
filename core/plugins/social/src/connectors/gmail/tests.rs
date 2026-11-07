//! The Gmail connector against loopback fakes of IMAP and SMTP, over plain TCP.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use base64::Engine;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::TcpListener;
use tokio::sync::mpsc;

use super::*;
use crate::connectors::{ChatKind, OnMessage};
use crate::db::{Db, History};

const PW: &str = "app-pw-test";
const ME: &str = "tester@example.test";

fn crlf(lf: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(lf.len() * 2);
    for &c in lf {
        if c == b'\n' {
            out.push(b'\r');
        }
        out.push(c);
    }
    out
}

fn simple(uid: u32, from: &str) -> Vec<u8> {
    format!("From: Tester <{from}>\r\nTo: {ME}\r\nSubject: note {uid}\r\nDate: Sat, 07 Nov 2026 10:0{uid}:00 +0000\r\nMessage-ID: <m{uid}@example.test>\r\n\r\nbody {uid}\r\n").into_bytes()
}

#[derive(Default)]
struct Fake {
    msgs: Vec<(u32, Vec<u8>)>,
    bad: Vec<String>,
    log: Vec<String>,
}

type Shared = Arc<Mutex<Fake>>;

/// IMAP's quoted strings and atoms, enough for the commands the connector sends.
fn args(s: &str) -> Vec<String> {
    let mut out = vec![];
    let mut it = s.chars().peekable();
    while let Some(&c) = it.peek() {
        if c == ' ' {
            it.next();
        } else if c == '"' {
            it.next();
            let mut v = String::new();
            while let Some(c) = it.next() {
                match c {
                    '\\' => v.extend(it.next()),
                    '"' => break,
                    c => v.push(c),
                }
            }
            out.push(v);
        } else {
            let mut v = String::new();
            while let Some(&c) = it.peek() {
                if c == ' ' {
                    break;
                }
                v.push(c);
                it.next();
            }
            out.push(v);
        }
    }
    out
}

fn respond(st: &Shared, cmd: &str) -> (Vec<u8>, String) {
    let mut f = st.lock().unwrap();
    let a = args(cmd);
    let word = |i: usize| a.get(i).map(|s| s.to_uppercase()).unwrap_or_default();
    match (word(0).as_str(), word(1).as_str()) {
        ("LOGIN", _) if a.get(1).map(String::as_str) == Some(ME) && a.get(2).map(String::as_str) == Some(PW) => (vec![], "OK LOGIN completed".into()),
        ("LOGIN", _) => (vec![], "NO [AUTHENTICATIONFAILED] Invalid credentials (Failure)".into()),
        ("EXAMINE", _) if f.bad.contains(&a[1]) => (vec![], "BAD Could not parse command".into()),
        ("EXAMINE", _) => (format!("* {} EXISTS\r\n* 0 RECENT\r\n* FLAGS (\\Seen)\r\n* OK [UIDVALIDITY 1] UIDs valid\r\n", f.msgs.len()).into_bytes(), "OK [READ-ONLY] EXAMINE completed".into()),
        ("UID", "SEARCH") => {
            let hit = |raw: &[u8]| -> bool {
                let text = String::from_utf8_lossy(raw);
                match word(2).as_str() {
                    "FROM" => text.lines().any(|l| l.starts_with("From:") && l.contains(&a[3])),
                    "HEADER" => text.lines().any(|l| l.to_lowercase().starts_with(&format!("{}:", a[3].to_lowercase())) && l.contains(&a[4])),
                    _ => true,
                }
            };
            let uids: Vec<String> = f.msgs.iter().filter(|(_, r)| hit(r)).map(|(u, _)| u.to_string()).collect();
            let line = if uids.is_empty() { "* SEARCH\r\n".to_string() } else { format!("* SEARCH {}\r\n", uids.join(" ")) };
            (line.into_bytes(), "OK SEARCH completed".into())
        }
        ("UID", "FETCH") => {
            let uid: u32 = a[2].parse().unwrap_or(0);
            let mut out = vec![];
            if let Some(pos) = f.msgs.iter().position(|(u, _)| *u == uid) {
                let raw = &f.msgs[pos].1;
                out.extend(format!("* {} FETCH (UID {uid} BODY[] {{{}}}\r\n", pos + 1, raw.len()).into_bytes());
                out.extend(raw);
                out.extend(b")\r\n");
            }
            (out, "OK FETCH completed".into())
        }
        ("LIST", _) => (
            b"* LIST (\\HasNoChildren) \"/\" \"INBOX\"\r\n* LIST (\\HasChildren \\Noselect) \"/\" \"[Gmail]\"\r\n* LIST (\\All \\HasNoChildren) \"/\" \"[Gmail]/All Mail\"\r\n".to_vec(),
            "OK LIST completed".into(),
        ),
        ("LOGOUT", _) => {
            f.log.push("-- logout".into());
            (b"* BYE logging out\r\n".to_vec(), "OK LOGOUT completed".into())
        }
        _ => (vec![], "BAD unknown command".into()),
    }
}

async fn fake_imap(st: Shared) -> u16 {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = l.local_addr().unwrap().port();
    tokio::spawn(async move {
        while let Ok((sock, _)) = l.accept().await {
            let st = st.clone();
            tokio::spawn(async move {
                let (r, mut w) = sock.into_split();
                let mut r = BufReader::new(r);
                w.write_all(b"* OK fake imap ready\r\n").await.unwrap();
                loop {
                    let mut line = String::new();
                    if r.read_line(&mut line).await.unwrap_or(0) == 0 {
                        break;
                    }
                    let line = line.trim_end_matches(['\r', '\n']).to_string();
                    let (tag, cmd) = line.split_once(' ').unwrap_or((&line, ""));
                    let (tag, cmd) = (tag.to_string(), cmd.to_string());
                    st.lock().unwrap().log.push(cmd.clone());
                    let (untagged, status) = respond(&st, &cmd);
                    w.write_all(&untagged).await.unwrap();
                    w.write_all(format!("{tag} {status}\r\n").as_bytes()).await.unwrap();
                    if cmd.to_uppercase().starts_with("LOGOUT") {
                        break;
                    }
                }
            });
        }
    });
    port
}

fn hooks(dir: &tempfile::TempDir) -> (Hooks, mpsc::UnboundedReceiver<Inbound>) {
    let (tx, rx) = mpsc::unbounded_channel();
    let db = Db::open(&dir.path().join("social.db")).unwrap();
    let on_message: OnMessage = Arc::new(move |m| {
        let tx = tx.clone();
        Box::pin(async move {
            let _ = tx.send(m);
        })
    });
    (Hooks { on_message, on_command: None, history: History::new(db.clone(), "gm"), db }, rx)
}

fn account() -> AccountConfig {
    AccountConfig { name: "gm".into(), platform: "gmail".into(), token: PW.into(), address: Some(ME.into()), enabled: true, trust: "cooperative".into(), ..Default::default() }
}

struct Rig {
    conn: Arc<GmailConnector>,
    fake: Shared,
    rx: mpsc::UnboundedReceiver<Inbound>,
    _dir: tempfile::TempDir,
}

async fn rig(msgs: Vec<(u32, Vec<u8>)>, cfg: AccountConfig) -> Rig {
    let fake: Shared = Arc::new(Mutex::new(Fake { msgs, ..Default::default() }));
    let port = fake_imap(fake.clone()).await;
    let dir = tempfile::tempdir().unwrap();
    let (h, rx) = hooks(&dir);
    let ep = Endpoints { imap_host: "127.0.0.1".into(), imap_port: port, imap_tls: false, smtp_host: "127.0.0.1".into(), smtp_port: 1, smtp_tls: false, poll: Duration::from_millis(50) };
    Rig { conn: GmailConnector::with_endpoints(cfg, h, ep), fake, rx, _dir: dir }
}

fn log(r: &Rig) -> Vec<String> {
    r.fake.lock().unwrap().log.clone()
}

// tests/test_gmail_imap.py

#[test]
fn imap_quote_escapes_quotes_and_backslashes() {
    for (v, want) in [
        ("[Gmail]/All Mail", "\"[Gmail]/All Mail\""),
        ("INBOX", "\"INBOX\""),
        ("has:attachment from:\"a b\"", "\"has:attachment from:\\\"a b\\\"\""),
        ("back\\slash", "\"back\\\\slash\""),
        ("two\r\nlines", "\"two  lines\""),
    ] {
        assert_eq!(imap_quote(v), want);
    }
}

#[tokio::test]
async fn all_mail_is_examined_quoted_and_the_query_goes_as_x_gm_raw() {
    let r = rig(vec![(1, simple(1, ME)), (2, simple(2, ME))], account()).await;
    let got = r.conn.search("has:attachment from:\"a b\"", 50, None).await.unwrap();
    assert_eq!(got.iter().map(|m| m.message_id.as_str()).collect::<Vec<_>>(), ["<m2@example.test>", "<m1@example.test>"]);
    let l = log(&r);
    assert!(l.contains(&"EXAMINE \"[Gmail]/All Mail\"".to_string()), "{l:?}");
    assert!(l.contains(&"UID SEARCH X-GM-RAW \"has:attachment from:\\\"a b\\\"\"".to_string()), "{l:?}");
    assert!(l.iter().any(|c| c == "UID FETCH 2 (BODY.PEEK[])"));
    assert!(!l.iter().any(|c| c.starts_with("SELECT")), "every read is read-only: {l:?}");
}

#[tokio::test]
async fn a_bad_reply_falls_back_to_inbox() {
    let r = rig(vec![(1, simple(1, ME))], account()).await;
    r.fake.lock().unwrap().bad.push(ALL_MAIL.into());
    let got = r.conn.search("x", 50, Some("tester+2@example.test")).await.unwrap();
    assert_eq!(got.len(), 1);
    let l: Vec<String> = log(&r).into_iter().filter(|c| c.starts_with("EXAMINE") || c.starts_with("UID SEARCH")).collect();
    assert_eq!(l, ["EXAMINE \"[Gmail]/All Mail\"", "EXAMINE \"INBOX\"", "UID SEARCH X-GM-RAW \"from:tester+2@example.test x\""]);
}

#[tokio::test]
async fn search_keeps_the_newest_limit_newest_first() {
    let r = rig((1..=4).map(|u| (u, simple(u, ME))).collect(), account()).await;
    let got = r.conn.search("x", 2, None).await.unwrap();
    assert_eq!(got.iter().map(|m| m.message_id.as_str()).collect::<Vec<_>>(), ["<m4@example.test>", "<m3@example.test>"]);
}

// tests/test_connectors_and_manifest.py

#[test]
fn an_explicit_subject_line_is_split_off() {
    assert_eq!(split_subject("Subject: Hi there\n\nthe body line"), ("Hi there".into(), "the body line".into()));
    assert_eq!(split_subject("subject:\nbody"), ("(no subject)".into(), "body".into()));
}

#[test]
fn without_a_subject_line_the_first_line_is_the_subject() {
    assert_eq!(split_subject("just a plain message"), ("just a plain message".into(), "just a plain message".into()));
    assert_eq!(split_subject("\n  first  \nsecond"), ("first".into(), "\n  first  \nsecond".into()));
    assert_eq!(split_subject(&"x".repeat(100)).0.len(), 78);
    assert_eq!(split_subject("").0, "(no subject)");
}

#[test]
fn build_makes_a_gmail_connector() {
    let dir = tempfile::tempdir().unwrap();
    let (h, _rx) = hooks(&dir);
    let g = crate::connectors::build(account(), h).unwrap();
    assert_eq!(g.platform(), "gmail");
    assert_eq!(g.account().address.as_deref(), Some(ME));
    assert!(!g.sends_files() && !g.keeps_history() && !g.live());
}

#[test]
fn endpoints_default_to_gmail() {
    let d = Endpoints::default();
    assert_eq!((d.imap_host.as_str(), d.imap_port, d.imap_tls), ("imap.gmail.com", 993, true));
    assert_eq!((d.smtp_host.as_str(), d.smtp_port, d.smtp_tls), ("smtp.gmail.com", 465, true));
    assert_eq!(d.poll, Duration::from_secs(30));
}

#[test]
fn the_tls_config_builds_with_the_webpki_roots() {
    assert!(tls_config().is_ok());
}

// The poll loop

async fn until(mut f: impl FnMut() -> bool) {
    tokio::time::timeout(Duration::from_secs(5), async {
        while !f() {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("condition not reached");
}

#[tokio::test]
async fn the_poll_sets_a_baseline_then_hands_on_only_newer_mail() {
    let mut r = rig(vec![(1, simple(1, ME)), (2, simple(2, ME))], account()).await;
    let task = tokio::spawn(r.conn.clone().start());
    let c = r.conn.clone();
    until(|| c.baseline() == 2 && c.live()).await;
    let nested = crlf(include_bytes!("fixtures/nested.eml"));
    r.fake.lock().unwrap().msgs.push((5, nested));
    let m = tokio::time::timeout(Duration::from_secs(5), r.rx.recv()).await.unwrap().unwrap();
    task.abort();
    assert_eq!(m.account, "gm");
    assert_eq!(m.platform, "gmail");
    assert_eq!((m.channel_id.as_str(), m.sender_id.as_str(), m.channel_name.as_str()), (ME, ME, "INBOX"));
    assert_eq!(m.sender_name, "Tester <tester@example.test>");
    assert_eq!((m.message_id.as_str(), m.ts.as_str()), ("<nested.1@example.test>", "Tue, 03 Nov 2026 10:00:00 +0000"));
    assert_eq!(m.text, "Hello caf\u{e9},\r\na soft break and a trailing line.\r\n");
    assert_eq!(m.attachments.iter().map(|a| a.filename.as_str()).collect::<Vec<_>>(), ["report.pdf", "attachment-1", "data.csv", "attachment-3"]);
    assert_eq!(m.attachments[0].r#ref, json!({"uid": "5", "ord": 0}));
    assert_eq!((m.server.as_str(), m.kind, m.outside, m.resynced), ("", ChatKind::Dm, false, false));
    assert_eq!((m.thread_id.as_str(), m.sender_bot, m.to_holder), ("", false, true));
    assert_eq!(r.conn.baseline(), 5);
    let l = log(&r);
    assert!(l.iter().all(|c| !c.starts_with("UID FETCH 1 ") && !c.starts_with("UID FETCH 2 ")), "old mail was fetched: {l:?}");
    assert!(l.iter().any(|c| c == "UID FETCH 5 (BODY.PEEK[])"));
    assert!(l.iter().filter(|c| c.starts_with("EXAMINE")).all(|c| c == "EXAMINE \"INBOX\""));
    assert!(r.rx.try_recv().is_err(), "one message only");
}

#[tokio::test]
async fn a_bad_app_password_takes_the_account_down() {
    let cfg = AccountConfig { token: "wrong".into(), ..account() };
    let r = rig(vec![(1, simple(1, ME))], cfg).await;
    let err = tokio::time::timeout(Duration::from_secs(5), r.conn.clone().start()).await.unwrap().unwrap_err();
    assert!(err.to_string().starts_with("gmail[gm] IMAP login failed; account down"), "{err}");
    assert!(!err.to_string().contains("wrong"));
    assert!(!r.conn.live());
}

#[tokio::test]
async fn an_empty_inbox_keeps_a_zero_baseline_until_mail_arrives() {
    let mut r = rig(vec![], account()).await;
    let task = tokio::spawn(r.conn.clone().start());
    let c = r.conn.clone();
    until(|| c.live()).await;
    // A first message into an empty inbox only sets the baseline, as the Python's does.
    r.fake.lock().unwrap().msgs.push((1, simple(1, ME)));
    until(|| c.baseline() == 1).await;
    r.fake.lock().unwrap().msgs.push((2, simple(2, ME)));
    let m = tokio::time::timeout(Duration::from_secs(5), r.rx.recv()).await.unwrap().unwrap();
    task.abort();
    assert_eq!(m.message_id, "<m2@example.test>");
}

// fetch, download, list, identity

#[tokio::test]
async fn fetch_pages_back_by_uid_from_one_sender_oldest_first() {
    let other = "tester+2@example.test";
    let r = rig(vec![(1, simple(1, ME)), (2, simple(2, other)), (3, simple(3, ME)), (4, simple(4, ME)), (5, simple(5, ME))], account()).await;
    let got = r.conn.fetch(ME, 2, Some("5")).await.unwrap();
    assert_eq!(got.iter().map(|m| m.message_id.as_str()).collect::<Vec<_>>(), ["<m3@example.test>", "<m4@example.test>"]);
    assert!(log(&r).contains(&format!("UID SEARCH FROM \"{ME}\"")));
    let all = r.conn.fetch("INBOX", 50, Some("not-a-uid")).await.unwrap();
    assert_eq!(all.len(), 5);
    assert_eq!(all[1].channel_id, other);
    assert!(log(&r).contains(&"UID SEARCH ALL".to_string()));
    let one = r.conn.fetch("", 0, None).await.unwrap();
    assert_eq!(one.iter().map(|m| m.message_id.as_str()).collect::<Vec<_>>(), ["<m5@example.test>"]);
}

#[tokio::test]
async fn download_finds_the_message_by_message_id_then_by_uid() {
    let nested = crlf(include_bytes!("fixtures/nested.eml"));
    let r = rig(vec![(1, simple(1, ME)), (9, nested)], account()).await;
    let all = r.conn.download_attachments(ME, "<nested.1@example.test>", None).await.unwrap();
    assert_eq!(all.iter().map(|d| (d.filename.as_str(), d.data.len())).collect::<Vec<_>>(), [("report.pdf", 285), ("attachment-1", 0), ("data.csv", 8), ("attachment-3", 4)]);
    assert!(log(&r).contains(&"UID SEARCH HEADER Message-ID \"<nested.1@example.test>\"".to_string()));
    let one = r.conn.download_attachments(ME, "9", Some(2)).await.unwrap();
    assert_eq!(one, [Downloaded { filename: "data.csv".into(), mime: "text/csv".into(), data: b"a,b\r\n1,2".to_vec() }]);
    assert!(r.conn.download_attachments(ME, "<m1@example.test>", None).await.unwrap().is_empty());
    let err = r.conn.download_attachments(ME, "<nope@example.test>", None).await.unwrap_err();
    assert_eq!((err.error_class.as_str(), err.message.as_str()), ("RuntimeError", "gmail[gm] message '<nope@example.test>' not found"));
}

#[tokio::test]
async fn mailboxes_are_the_channels() {
    let r = rig(vec![], account()).await;
    let got = r.conn.list_channels(false).await.unwrap();
    assert_eq!(got.iter().map(|c| c.id.as_str()).collect::<Vec<_>>(), ["INBOX", "[Gmail]", ALL_MAIL]);
    assert!(got.iter().all(|c| c.kind == "mailbox" && c.id == c.name));
    assert!(log(&r).contains(&"LIST \"\" *".to_string()));
}

#[tokio::test]
async fn identity_is_the_address_and_the_display_name() {
    let r = rig(vec![], account()).await;
    assert_eq!(r.conn.identity().await.unwrap(), Identity { id: ME.into(), name: ME.into() });
    let r = rig(vec![], AccountConfig { display_name: Some("Tester".into()), ..account() }).await;
    assert_eq!(r.conn.identity().await.unwrap().name, "Tester");
}

#[tokio::test]
async fn reads_without_an_address_refuse() {
    let r = rig(vec![], AccountConfig { address: None, ..account() }).await;
    let err = r.conn.fetch("", 5, None).await.unwrap_err();
    assert_eq!((err.error_class.as_str(), err.message.as_str()), ("RuntimeError", "gmail[gm] no address configured"));
    assert!(r.conn.clone().start().await.is_err());
}

// SMTP

#[derive(Default)]
struct Smtp {
    auth: Vec<String>,
    envelope: Vec<String>,
    data: String,
}

async fn fake_smtp(st: Arc<Mutex<Smtp>>) -> u16 {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = l.local_addr().unwrap().port();
    tokio::spawn(async move {
        let (sock, _) = l.accept().await.unwrap();
        let (r, mut w) = sock.into_split();
        let mut r = BufReader::new(r);
        w.write_all(b"220 fake.test ESMTP\r\n").await.unwrap();
        loop {
            let mut line = String::new();
            if r.read_line(&mut line).await.unwrap_or(0) == 0 {
                break;
            }
            let cmd = line.trim_end().to_string();
            let up = cmd.to_uppercase();
            let reply: &[u8] = if up.starts_with("EHLO") {
                b"250-fake.test\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME\r\n"
            } else if up.starts_with("AUTH PLAIN ") {
                st.lock().unwrap().auth.push(cmd[11..].to_string());
                b"235 2.7.0 Accepted\r\n"
            } else if up.starts_with("MAIL FROM") || up.starts_with("RCPT TO") {
                st.lock().unwrap().envelope.push(cmd.clone());
                b"250 OK\r\n"
            } else if up == "DATA" {
                w.write_all(b"354 go ahead\r\n").await.unwrap();
                let mut data = String::new();
                loop {
                    let mut l = String::new();
                    r.read_line(&mut l).await.unwrap();
                    if l == ".\r\n" {
                        break;
                    }
                    data.push_str(&l);
                }
                st.lock().unwrap().data = data;
                b"250 2.0.0 OK queued\r\n"
            } else if up == "QUIT" {
                w.write_all(b"221 bye\r\n").await.unwrap();
                break;
            } else {
                b"502 unknown\r\n"
            };
            w.write_all(reply).await.unwrap();
        }
    });
    port
}

#[tokio::test]
async fn send_logs_in_and_threads_by_message_id() {
    let st = Arc::new(Mutex::new(Smtp::default()));
    let port = fake_smtp(st.clone()).await;
    let dir = tempfile::tempdir().unwrap();
    let (h, _rx) = hooks(&dir);
    let ep = Endpoints { smtp_host: "127.0.0.1".into(), smtp_port: port, smtp_tls: false, ..Endpoints::default() };
    let conn = GmailConnector::with_endpoints(account(), h, ep);
    let sent = conn.send("tester+2@example.test", "Subject: Hi there\n\nthe body line", Some("<parent@example.test>"), &[]).await.unwrap();
    let mid = sent["message_id"].as_str().unwrap().to_string();
    assert!(mid.starts_with('<') && mid.ends_with('>') && mid.matches('.').count() >= 2, "{mid}");
    assert_eq!(sent["channel_id"], "tester+2@example.test");
    let ts = sent["ts"].as_str().unwrap();
    assert!(chrono::DateTime::parse_from_rfc2822(ts).is_ok() && ts.ends_with(" -0000"), "{ts}");
    let s = st.lock().unwrap();
    let plain = base64::engine::general_purpose::STANDARD.decode(&s.auth[0]).unwrap();
    assert_eq!(plain, format!("\0{ME}\0{PW}").into_bytes());
    assert_eq!(s.envelope, [format!("MAIL FROM:<{ME}>"), "RCPT TO:<tester+2@example.test>".to_string()]);
    let (head, body) = s.data.split_once("\r\n\r\n").unwrap();
    let header = |n: &str| head.lines().find_map(|l| l.strip_prefix(&format!("{n}: "))).map(str::to_string);
    assert_eq!(header("From").as_deref(), Some(ME));
    assert_eq!(header("To").as_deref(), Some("tester+2@example.test"));
    assert_eq!(header("Subject").as_deref(), Some("Hi there"));
    assert_eq!(header("Message-ID"), Some(mid));
    assert_eq!(header("In-Reply-To").as_deref(), Some("<parent@example.test>"));
    assert_eq!(header("References").as_deref(), Some("<parent@example.test>"));
    assert!(header("Content-Type").unwrap().starts_with("text/plain"));
    assert_eq!(body, "the body line\r\n");
}

#[tokio::test]
async fn a_bad_recipient_is_a_value_error() {
    let r = rig(vec![], account()).await;
    let err = r.conn.send("not an address", "hi", None, &[]).await.unwrap_err();
    assert_eq!(err.error_class, "ValueError");
    assert!(err.message.starts_with("gmail[gm] bad recipient 'not an address'"), "{}", err.message);
}
