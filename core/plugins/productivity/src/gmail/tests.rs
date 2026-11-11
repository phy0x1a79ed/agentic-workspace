//! The Gmail connector against a loopback fake of IMAP, over plain TCP.

use std::sync::{Arc, Mutex};

use serde_json::json;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::TcpListener;

use super::*;

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
    /// Each APPEND: the mailbox and flags line, and the message.
    appended: Vec<(String, Vec<u8>)>,
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
            b"* LIST (\\HasNoChildren) \"/\" \"INBOX\"\r\n* LIST (\\HasChildren \\Noselect) \"/\" \"[Gmail]\"\r\n* LIST (\\All \\HasNoChildren) \"/\" \"[Gmail]/All Mail\"\r\n* LIST (\\Drafts \\HasNoChildren) \"/\" \"[Gmail]/Entw&APw-rfe\"\r\n".to_vec(),
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
                    if cmd.to_uppercase().starts_with("APPEND ") {
                        let n: usize = cmd.rsplit_once('{').and_then(|(_, n)| n.trim_end_matches('}').parse().ok()).unwrap_or(0);
                        w.write_all(b"+ Ready for literal data\r\n").await.unwrap();
                        let mut msg = vec![0; n];
                        r.read_exact(&mut msg).await.unwrap();
                        let mut end = String::new();
                        r.read_line(&mut end).await.unwrap();
                        st.lock().unwrap().appended.push((cmd.clone(), msg));
                        w.write_all(format!("{tag} OK [APPENDUID 1 9] APPEND completed\r\n").as_bytes()).await.unwrap();
                        continue;
                    }
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

fn account() -> AccountConfig {
    AccountConfig { name: "gm".into(), platform: "gmail".into(), token: PW.into(), address: Some(ME.into()), enabled: true, ..Default::default() }
}

struct Rig {
    conn: GmailConnector,
    fake: Shared,
}

async fn rig(msgs: Vec<(u32, Vec<u8>)>, cfg: AccountConfig) -> Rig {
    let fake: Shared = Arc::new(Mutex::new(Fake { msgs, ..Default::default() }));
    let port = fake_imap(fake.clone()).await;
    let ep = Endpoints { imap_host: "127.0.0.1".into(), imap_port: port, imap_tls: false };
    Rig { conn: GmailConnector::new(cfg, ep), fake }
}

fn log(r: &Rig) -> Vec<String> {
    r.fake.lock().unwrap().log.clone()
}

// tests/test_gmail_imap.py

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
async fn fetch_reads_inbox_read_only_and_parses_the_message() {
    let r = rig(vec![(1, simple(1, ME)), (5, crlf(include_bytes!("fixtures/nested.eml")))], account()).await;
    let got = r.conn.fetch("", 1, None).await.unwrap();
    let m = &got[0];
    assert_eq!((m.account.as_str(), m.platform.as_str()), ("gm", "gmail"));
    assert_eq!((m.channel_id.as_str(), m.sender_id.as_str(), m.channel_name.as_str()), (ME, ME, "INBOX"));
    assert_eq!(m.sender_name, "Tester <tester@example.test>");
    assert_eq!((m.message_id.as_str(), m.ts.as_str()), ("<nested.1@example.test>", "Tue, 03 Nov 2026 10:00:00 +0000"));
    assert_eq!(m.text, "Hello caf\u{e9},\r\na soft break and a trailing line.\r\n");
    assert_eq!(m.attachments.iter().map(|a| a.filename.as_str()).collect::<Vec<_>>(), ["report.pdf", "attachment-1", "data.csv", "attachment-3"]);
    assert_eq!(m.attachments[0].r#ref, json!({"uid": "5", "ord": 0}));
    let l = log(&r);
    assert!(l.iter().all(|c| !c.starts_with("UID FETCH 1 ")), "only the last `limit`: {l:?}");
    assert!(l.iter().filter(|c| c.starts_with("EXAMINE")).all(|c| c == "EXAMINE \"INBOX\""));
}

#[tokio::test]
async fn a_bad_app_password_fails_the_probe_without_echoing_it() {
    let r = rig(vec![], AccountConfig { token: "wrong".into(), ..account() }).await;
    let err = r.conn.probe().await.unwrap_err();
    assert!(err.message.starts_with("gmail[gm] imap:"), "{}", err.message);
    assert!(!err.message.contains("wrong"));
}

#[tokio::test]
async fn a_reply_draft_lands_in_the_drafts_mailbox_threaded_with_its_file() {
    let r = rig(vec![(1, simple(1, "boss@example.test"))], account()).await;
    let file = DraftFile { filename: "plan v2.pdf".into(), mime: "application/pdf".into(), data: b"%PDF-1".to_vec() };
    let d = Draft { body: "see attached".into(), files: vec![file], reply_to: Some("<m1@example.test>".into()), ..Default::default() };
    let id = r.conn.draft(d).await.unwrap();
    let appended = r.fake.lock().unwrap().appended.clone();
    assert_eq!(appended.len(), 1);
    let (cmd, raw) = &appended[0];
    assert!(cmd.starts_with("APPEND \"[Gmail]/Entw&APw-rfe\" (\\Draft)"), "found by its \\Drafts flag, not its name: {cmd}");
    let text = String::from_utf8_lossy(raw);
    for want in ["To: Tester <boss@example.test>", "Subject: Re: note 1", "In-Reply-To: <m1@example.test>", "References: <m1@example.test>", &format!("Message-ID: {id}")] {
        assert!(text.contains(want), "{want} missing from\n{text}");
    }
    let (head, _) = mime::parse(raw);
    let files = head.attachment_bytes(None);
    assert_eq!((files[0].filename.as_str(), files[0].data.as_slice()), ("plan v2.pdf", b"%PDF-1".as_slice()));
    assert!(log(&r).iter().all(|c| !c.to_uppercase().starts_with("SELECT")), "nothing but the append writes");
}

#[test]
fn a_draft_with_no_recipient_is_refused() {
    assert_eq!(compose(ME, &Draft { subject: "s".into(), ..Default::default() }, "<id>", &[]).unwrap_err().error_class, "ValueError");
}
