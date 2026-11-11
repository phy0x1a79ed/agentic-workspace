//! Tony's Gmail through IMAP with the mailbox address and an App Password (`gmail_conn.py`).
//!
//! Every call opens its own IMAP session. Reads use `BODY.PEEK[]`, so nothing is marked read.
//! Search and download look in `[Gmail]/All Mail`, falling back to INBOX. There is no send: a
//! [`Draft`] is appended to the mailbox flagged `\Drafts`, and Tony sends it himself.
//!
//! The host, port and TLS are [`Endpoints`], read from `AWM_GMAIL_IMAP_HOST`,
//! `AWM_GMAIL_IMAP_PORT` and `AWM_GMAIL_IMAP_TLS` (`0` or `false` for plain TCP), so tests run
//! against a loopback fake.

use std::pin::Pin;
use std::sync::{Arc, OnceLock};
use std::task::{Context, Poll};
use std::time::Duration;

use async_imap::error::Error as ImapError;
use async_imap::Session;
use async_trait::async_trait;
use awm_chat::{runtime, value, Channel, ChatKind, Downloaded, Identity, Inbound, Result};
use awm_plugin_sdk::VerbError;
use base64::Engine;
use futures_util::StreamExt;
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::net::TcpStream;
use tokio_rustls::client::TlsStream;
use tokio_rustls::rustls;

use crate::config::AccountConfig;
use crate::Account;

mod mime;

pub const IMAP_HOST: &str = "imap.gmail.com";
pub const IMAP_PORT: u16 = 993;
/// Gmail's virtual mailbox of every message, archived or not: the scope for search and lookup.
pub const ALL_MAIL: &str = "[Gmail]/All Mail";
/// One IMAP session's ceiling, so a wedged server cannot stall a call for good.
const SESSION_TIMEOUT: Duration = Duration::from_secs(300);

/// Where the connector reaches Gmail.
#[derive(Debug, Clone, PartialEq)]
pub struct Endpoints {
    pub imap_host: String,
    pub imap_port: u16,
    pub imap_tls: bool,
}

impl Default for Endpoints {
    fn default() -> Self {
        Self { imap_host: IMAP_HOST.into(), imap_port: IMAP_PORT, imap_tls: true }
    }
}

impl Endpoints {
    /// Gmail's own endpoints, each overridden by its `AWM_GMAIL_*` variable when set.
    pub fn from_env() -> Self {
        let var = |k: &str| std::env::var(k).ok().map(|v| v.trim().to_string()).filter(|v| !v.is_empty());
        let flag = |k: &str| var(k).map(|v| !matches!(v.to_lowercase().as_str(), "0" | "false" | "no" | "off"));
        let d = Self::default();
        Self {
            imap_host: var("AWM_GMAIL_IMAP_HOST").unwrap_or(d.imap_host),
            imap_port: var("AWM_GMAIL_IMAP_PORT").and_then(|p| p.parse().ok()).unwrap_or(d.imap_port),
            imap_tls: flag("AWM_GMAIL_IMAP_TLS").unwrap_or(d.imap_tls),
        }
    }
}

/// An IMAP transport: TLS to Gmail, or plain TCP to a test fake.
#[derive(Debug)]
enum Stream {
    Plain(TcpStream),
    Tls(Box<TlsStream<TcpStream>>),
}

impl AsyncRead for Stream {
    fn poll_read(self: Pin<&mut Self>, cx: &mut Context<'_>, buf: &mut ReadBuf<'_>) -> Poll<std::io::Result<()>> {
        match self.get_mut() {
            Stream::Plain(s) => Pin::new(s).poll_read(cx, buf),
            Stream::Tls(s) => Pin::new(s.as_mut()).poll_read(cx, buf),
        }
    }
}

impl AsyncWrite for Stream {
    fn poll_write(self: Pin<&mut Self>, cx: &mut Context<'_>, buf: &[u8]) -> Poll<std::io::Result<usize>> {
        match self.get_mut() {
            Stream::Plain(s) => Pin::new(s).poll_write(cx, buf),
            Stream::Tls(s) => Pin::new(s.as_mut()).poll_write(cx, buf),
        }
    }
    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        match self.get_mut() {
            Stream::Plain(s) => Pin::new(s).poll_flush(cx),
            Stream::Tls(s) => Pin::new(s.as_mut()).poll_flush(cx),
        }
    }
    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        match self.get_mut() {
            Stream::Plain(s) => Pin::new(s).poll_shutdown(cx),
            Stream::Tls(s) => Pin::new(s.as_mut()).poll_shutdown(cx),
        }
    }
}

type Imap = Session<Stream>;

fn tls_config() -> std::result::Result<Arc<rustls::ClientConfig>, String> {
    static CFG: OnceLock<std::result::Result<Arc<rustls::ClientConfig>, String>> = OnceLock::new();
    CFG.get_or_init(|| {
        let mut roots = rustls::RootCertStore::empty();
        roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
        // The workspace links both ring and aws-lc-rs, so the provider is named, never guessed.
        rustls::ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
            .with_safe_default_protocol_versions()
            .map(|b| Arc::new(b.with_root_certificates(roots).with_no_client_auth()))
            .map_err(|e| e.to_string())
    })
    .clone()
}

/// `_imap_quote`: one IMAP quoted string. Only `"` and `\` are escaped (RFC 3501
/// quoted-specials); a line break, which would end the command, becomes a space.
pub fn imap_quote(value: &str) -> String {
    let v = value.replace(['\r', '\n'], " ");
    format!("\"{}\"", v.replace('\\', "\\\\").replace('"', "\\\""))
}

/// `email.utils.make_msgid()`: `<centiseconds.pid.random@host>`.
fn make_msgid() -> String {
    let centis = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() / 10).unwrap_or(0);
    format!("<{centis}.{}.{}@{}>", std::process::id(), rand::random::<u64>(), hostname())
}

fn hostname() -> String {
    let mut buf = [0u8; 256];
    // SAFETY: the buffer is valid for its length, and gethostname writes at most that much.
    let rc = unsafe { libc::gethostname(buf.as_mut_ptr().cast(), buf.len()) };
    let end = buf.iter().position(|&b| b == 0).unwrap_or(buf.len());
    if rc != 0 || end == 0 {
        return "localhost".into();
    }
    String::from_utf8_lossy(&buf[..end]).into_owned()
}

/// `email.utils.formatdate()`: RFC 2822 in UTC, with the `-0000` zone.
fn formatdate() -> String {
    chrono::Utc::now().format("%a, %d %b %Y %H:%M:%S -0000").to_string()
}

/// Python's `repr` of a str, for the error texts that quote one.
fn py_repr(s: &str) -> String {
    let q = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::from(q);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == q => {
                out.push('\\');
                out.push(c);
            }
            c => out.push(c),
        }
    }
    out.push(q);
    out
}

/// An IMAP failure, safe to log: a parse error can carry message bytes, so it is named only.
fn reason(e: &ImapError) -> String {
    match e {
        ImapError::Parse(_) => "unparseable server response".into(),
        e => e.to_string(),
    }
}

/// One file on a draft.
#[derive(Debug, Clone, PartialEq)]
pub struct DraftFile {
    pub filename: String,
    pub mime: String,
    pub data: Vec<u8>,
}

/// A message for Tony to finish and send. `reply_to` is the Message-ID it answers.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Draft {
    pub to: String,
    pub cc: String,
    pub subject: String,
    pub body: String,
    pub files: Vec<DraftFile>,
    pub reply_to: Option<String>,
}

/// One account's mailbox.
pub struct GmailConnector {
    cfg: AccountConfig,
    addr: String,
    ep: Endpoints,
}

impl GmailConnector {
    /// The connector for `cfg` at `ep`.
    pub fn new(cfg: AccountConfig, ep: Endpoints) -> Self {
        let addr = cfg.address.clone().unwrap_or_default();
        Self { cfg, addr, ep }
    }

    fn name(&self) -> &str {
        &self.cfg.name
    }

    fn err(&self, e: &ImapError) -> VerbError {
        runtime(format!("gmail[{}] imap: {}", self.name(), reason(e)))
    }

    fn need_address(&self) -> Result<()> {
        if self.addr.is_empty() {
            return Err(runtime(format!("gmail[{}] no address configured", self.name())));
        }
        Ok(())
    }

    async fn connect(&self) -> std::result::Result<Imap, ImapError> {
        let tcp = TcpStream::connect((self.ep.imap_host.as_str(), self.ep.imap_port)).await?;
        let stream = if self.ep.imap_tls {
            let cfg = tls_config().map_err(|e| ImapError::Io(std::io::Error::other(e)))?;
            let host = rustls::pki_types::ServerName::try_from(self.ep.imap_host.clone()).map_err(|e| ImapError::Io(std::io::Error::other(e)))?;
            Stream::Tls(Box::new(tokio_rustls::TlsConnector::from(cfg).connect(host, tcp).await?))
        } else {
            Stream::Plain(tcp)
        };
        let mut client = async_imap::Client::new(stream);
        match client.read_response().await {
            Some(Ok(_)) => {}
            Some(Err(e)) => return Err(e.into()),
            None => return Err(ImapError::ConnectionLost),
        }
        client.login(&self.addr, &self.cfg.token).await.map_err(|(e, _)| e)
    }

    /// Log in, run `op`, log out: each call has its own session, as the Python's did.
    async fn session<R>(&self, op: impl for<'s> FnOnce(&'s mut Imap) -> futures_util::future::BoxFuture<'s, std::result::Result<R, ImapError>>) -> std::result::Result<R, ImapError> {
        let run = async {
            let mut s = self.connect().await?;
            let out = op(&mut s).await;
            let _ = s.logout().await;
            out
        };
        tokio::time::timeout(SESSION_TIMEOUT, run).await.unwrap_or_else(|_| Err(ImapError::Io(std::io::Error::new(std::io::ErrorKind::TimedOut, "imap session timed out"))))
    }

    fn inbound(&self, r: mime::Row) -> Inbound {
        Inbound {
            account: self.cfg.name.clone(),
            platform: "gmail".into(),
            channel_id: r.from_addr.clone(),
            channel_name: "INBOX".into(),
            text: r.text,
            sender_id: r.from_addr,
            sender_name: r.sender_name,
            thread_id: String::new(),
            message_id: r.message_id,
            ts: r.ts,
            attachments: r.attachments,
            sender_bot: false,
            to_holder: true,
            server: String::new(),
            kind: ChatKind::Dm,
            outside: false,
            resynced: false,
        }
    }

    /// The raw message with this Message-ID, or with this bare UID, from All Mail.
    async fn raw_message(&self, message_id: &str) -> Result<Vec<u8>> {
        let account = self.name().to_string();
        let mid = message_id.to_string();
        let raw = self
            .session(move |s| {
                Box::pin(async move {
                    select_mailbox(s, &account, ALL_MAIL).await?;
                    let mut raw = None;
                    let uids = search_uids(s, &format!("HEADER Message-ID {}", imap_quote(&mid))).await?;
                    if let Some(uid) = uids.last() {
                        raw = fetch_body(s, &uid.to_string()).await?;
                    }
                    if raw.is_none() && !mid.is_empty() && mid.bytes().all(|b| b.is_ascii_digit()) {
                        raw = fetch_body(s, &mid).await?;
                    }
                    Ok(raw)
                })
            })
            .await
            .map_err(|e| self.err(&e))?;
        raw.ok_or_else(|| runtime(format!("gmail[{}] message {} not found", self.name(), py_repr(message_id))))
    }

    /// Save `d` to Drafts and return its Message-ID. A reply takes the recipient, subject and
    /// thread headers of the message it answers, unless `d` names them.
    pub async fn draft(&self, mut d: Draft) -> Result<String> {
        self.need_address()?;
        let mut thread = vec![];
        if let Some(parent) = d.reply_to.clone().filter(|r| !r.is_empty()) {
            let raw = self.raw_message(&parent).await?;
            let (head, _) = mime::parse(&raw);
            let pid = head.get("Message-ID").unwrap_or(&parent).trim().to_string();
            if d.to.is_empty() {
                d.to = head.get("Reply-To").or(head.get("From")).unwrap_or("").trim().to_string();
            }
            if d.subject.is_empty() {
                let s = head.get("Subject").unwrap_or("").trim();
                d.subject = if s.to_lowercase().starts_with("re:") { s.to_string() } else { format!("Re: {s}") };
            }
            let refs = head.get("References").unwrap_or("").split_whitespace().map(str::to_string).collect::<Vec<_>>();
            thread.push(("In-Reply-To", pid.clone()));
            thread.push(("References", refs.into_iter().chain([pid]).collect::<Vec<_>>().join(" ")));
        }
        let msg_id = make_msgid();
        let raw = compose(&self.addr, &d, &msg_id, &thread)?;
        self.session(move |s| {
            Box::pin(async move {
                let folder = drafts_folder(s).await?;
                s.append(&folder, Some("(\\Draft)"), None, &raw).await?;
                Ok(())
            })
        })
        .await
        .map_err(|e| self.err(&e))?;
        Ok(msg_id)
    }
}

/// The mailbox flagged `\Drafts`, whatever its localised name. Gmail always has one.
async fn drafts_folder(s: &mut Imap) -> std::result::Result<String, ImapError> {
    let mut names = vec![];
    {
        let stream = s.list(Some(""), Some("*")).await?;
        let mut stream = std::pin::pin!(stream);
        while let Some(n) = stream.next().await {
            let n = n?;
            if n.attributes().iter().any(|a| matches!(a, async_imap::types::NameAttribute::Drafts)) {
                names.push(n.name().to_string());
            }
        }
    }
    names.into_iter().next().ok_or_else(|| ImapError::Io(std::io::Error::other("no mailbox is flagged \\Drafts")))
}

/// RFC 2047 for a header value that is not plain ASCII.
fn header_word(v: &str) -> String {
    if v.is_ascii() {
        return v.replace(['\r', '\n'], " ");
    }
    format!("=?utf-8?b?{}?=", base64::engine::general_purpose::STANDARD.encode(v))
}

/// Base64 in 76-column lines, as MIME wants it.
fn b64_lines(data: &[u8]) -> String {
    let enc = base64::engine::general_purpose::STANDARD.encode(data);
    enc.as_bytes().chunks(76).map(|c| String::from_utf8_lossy(c).into_owned()).collect::<Vec<_>>().join("\r\n")
}

/// The draft as an RFC 5322 message: a plain body, and a multipart/mixed one with files.
pub fn compose(from: &str, d: &Draft, msg_id: &str, thread: &[(&str, String)]) -> Result<Vec<u8>> {
    if d.to.trim().is_empty() {
        return Err(value("draft needs `to`, or `reply_to` naming a message to answer"));
    }
    let mut head = vec![
        format!("From: {from}"),
        format!("To: {}", header_word(&d.to)),
        format!("Subject: {}", header_word(if d.subject.is_empty() { "(no subject)" } else { &d.subject })),
        format!("Date: {}", formatdate()),
        format!("Message-ID: {msg_id}"),
        "MIME-Version: 1.0".into(),
    ];
    if !d.cc.trim().is_empty() {
        head.insert(2, format!("Cc: {}", header_word(&d.cc)));
    }
    head.extend(thread.iter().map(|(k, v)| format!("{k}: {v}")));
    let text = format!("Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n{}", b64_lines(d.body.as_bytes()));
    let out = if d.files.is_empty() {
        format!("{}\r\n{text}\r\n", head.join("\r\n"))
    } else {
        let boundary = format!("=_awm_{:016x}", rand::random::<u64>());
        let mut parts = vec![text];
        for f in &d.files {
            let name = header_word(&f.filename).replace('"', "'");
            let mime = if f.mime.is_empty() { "application/octet-stream" } else { &f.mime };
            parts.push(format!(
                "Content-Type: {mime}; name=\"{name}\"\r\nContent-Disposition: attachment; filename=\"{name}\"\r\nContent-Transfer-Encoding: base64\r\n\r\n{}",
                b64_lines(&f.data)
            ));
        }
        let body: String = parts.iter().map(|p| format!("--{boundary}\r\n{p}\r\n")).collect();
        format!("{}\r\nContent-Type: multipart/mixed; boundary=\"{boundary}\"\r\n\r\n{body}--{boundary}--\r\n", head.join("\r\n"))
    };
    Ok(out.into_bytes())
}

/// `UID FETCH <uid> (BODY.PEEK[])`: the first body the server returns, or None on a NO.
async fn fetch_body(s: &mut Imap, uid: &str) -> std::result::Result<Option<Vec<u8>>, ImapError> {
    let mut stream = match s.uid_fetch(uid, "(BODY.PEEK[])").await {
        Ok(st) => st,
        Err(ImapError::No(_)) => return Ok(None),
        Err(e) => return Err(e),
    };
    let mut body = None;
    while let Some(item) = stream.next().await {
        match item {
            Ok(f) => {
                if body.is_none() {
                    body = f.body().map(<[u8]>::to_vec);
                }
            }
            Err(ImapError::No(_)) => return Ok(None),
            Err(e) => return Err(e),
        }
    }
    Ok(body)
}

/// `UID SEARCH <query>`, ascending. A NO is an empty result, as the Python's non-OK branch.
async fn search_uids(s: &mut Imap, query: &str) -> std::result::Result<Vec<u32>, ImapError> {
    let mut uids: Vec<u32> = match s.uid_search(query).await {
        Ok(u) => u.into_iter().collect(),
        Err(ImapError::No(_)) => return Ok(vec![]),
        Err(e) => return Err(e),
    };
    uids.sort_unstable();
    Ok(uids)
}

/// `_select_mailbox`: EXAMINE `mailbox`, falling back to INBOX. Returns the one used.
async fn select_mailbox(s: &mut Imap, account: &str, mailbox: &str) -> std::result::Result<String, ImapError> {
    match s.examine(mailbox).await {
        Ok(_) => Ok(mailbox.to_string()),
        Err(e) => {
            tracing::warn!("gmail[{account}]: cannot select {} ({}) - falling back to INBOX; archived mail will not be visible", py_repr(mailbox), reason(&e));
            s.examine("INBOX").await?;
            Ok("INBOX".into())
        }
    }
}

#[async_trait]
impl Account for GmailConnector {
    fn platform(&self) -> &str {
        "gmail"
    }

    /// One login and logout, which a bad App Password or IMAP turned off fails.
    async fn probe(&self) -> Result<()> {
        self.need_address()?;
        self.session(|_| Box::pin(async { Ok(()) })).await.map_err(|e| self.err(&e))
    }

    /// The last `limit` INBOX messages, oldest first, from `channel` when it is an address.
    /// `before` is an exclusive UID bound; a cursor that is not a number is ignored.
    async fn fetch(&self, channel: &str, limit: usize, before: Option<&str>) -> Result<Vec<Inbound>> {
        self.need_address()?;
        let before = before.and_then(|b| b.trim().parse::<i64>().ok()).filter(|&b| b != 0);
        let channel = channel.to_string();
        let rows = self
            .session(move |s| {
                Box::pin(async move {
                    s.examine("INBOX").await?;
                    let query = if !channel.is_empty() && channel.contains('@') { format!("FROM {}", imap_quote(&channel)) } else { "ALL".into() };
                    let mut uids = search_uids(s, &query).await?;
                    if let Some(b) = before {
                        uids.retain(|&u| i64::from(u) < b);
                    }
                    let keep = limit.max(1);
                    let uids = &uids[uids.len().saturating_sub(keep)..];
                    let mut out = vec![];
                    for uid in uids {
                        if let Some(raw) = fetch_body(s, &uid.to_string()).await? {
                            out.push(mime::row(&raw, &uid.to_string()));
                        }
                    }
                    Ok(out)
                })
            })
            .await
            .map_err(|e| self.err(&e))?;
        Ok(rows.into_iter().map(|r| self.inbound(r)).collect())
    }

    /// Gmail's own query syntax over All Mail through `X-GM-RAW`, newest first.
    async fn search(&self, query: &str, limit: usize, channel: Option<&str>) -> Result<Vec<Inbound>> {
        self.need_address()?;
        let q = match channel {
            Some(c) if c.contains('@') => format!("from:{c} {query}").trim().to_string(),
            _ => query.to_string(),
        };
        let account = self.name().to_string();
        let rows = self
            .session(move |s| {
                Box::pin(async move {
                    select_mailbox(s, &account, ALL_MAIL).await?;
                    let uids = search_uids(s, &format!("X-GM-RAW {}", imap_quote(&q))).await?;
                    let uids = &uids[uids.len().saturating_sub(limit.max(1))..];
                    let mut out = vec![];
                    for uid in uids.iter().rev() {
                        if let Some(raw) = fetch_body(s, &uid.to_string()).await? {
                            out.push(mime::row(&raw, &uid.to_string()));
                        }
                    }
                    Ok(out)
                })
            })
            .await
            .map_err(|e| self.err(&e))?;
        Ok(rows.into_iter().map(|r| self.inbound(r)).collect())
    }

    /// Re-fetch one message by its Message-ID, or by a bare UID, and return its file parts.
    async fn download_attachments(&self, _channel: &str, message_id: &str, idx: Option<i64>) -> Result<Vec<Downloaded>> {
        self.need_address()?;
        let raw = self.raw_message(message_id).await?;
        Ok(mime::parse(&raw).0.attachment_bytes(idx))
    }

    /// The account's mailboxes and labels. Gmail has no DMs, so `include_dms` is ignored.
    async fn list_channels(&self, _include_dms: bool) -> Result<Vec<Channel>> {
        let names = self
            .session(|s| {
                Box::pin(async move {
                    let mut out = vec![];
                    let stream = match s.list(Some(""), Some("*")).await {
                        Ok(st) => st,
                        Err(ImapError::No(_)) => return Ok(out),
                        Err(e) => return Err(e),
                    };
                    let mut stream = std::pin::pin!(stream);
                    while let Some(n) = stream.next().await {
                        let n = n?;
                        let name = n.name().trim().trim_matches('"').to_string();
                        if !name.is_empty() {
                            out.push(name);
                        }
                    }
                    Ok(out)
                })
            })
            .await
            .map_err(|e| self.err(&e))?;
        Ok(names.into_iter().map(|n| Channel { id: n.clone(), name: n, kind: "mailbox".into() }).collect())
    }

    async fn identity(&self) -> Result<Identity> {
        Ok(Identity { id: self.addr.clone(), name: self.cfg.display_name.clone().filter(|d| !d.is_empty()).unwrap_or_else(|| self.addr.clone()) })
    }

    fn as_gmail(&self) -> Option<&GmailConnector> {
        Some(self)
    }
}

#[cfg(test)]
mod tests;
