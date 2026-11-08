//! Gmail through IMAP and SMTP with the mailbox address and an App Password (`gmail_conn.py`).
//!
//! Receive is a poll, not IDLE: every 30 s one IMAP session searches INBOX and hands each UID
//! above the baseline to `on_message`. The first session only records the baseline, so old mail
//! is never replayed. Every read uses `BODY.PEEK[]`, so polling never marks mail read. Search and
//! download look in `[Gmail]/All Mail`, falling back to INBOX. Send is plain text over SMTPS.
//!
//! The hosts, ports and TLS are [`Endpoints`], read from `AWM_GMAIL_IMAP_HOST`,
//! `AWM_GMAIL_IMAP_PORT`, `AWM_GMAIL_IMAP_TLS`, `AWM_GMAIL_SMTP_HOST`, `AWM_GMAIL_SMTP_PORT` and
//! `AWM_GMAIL_SMTP_TLS` (`0` or `false` for plain TCP), so tests run against loopback fakes.

use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, OnceLock};
use std::task::{Context, Poll};
use std::time::Duration;

use async_imap::error::Error as ImapError;
use async_imap::Session;
use awm_plugin_sdk::Backoff;
use async_trait::async_trait;
use futures_util::StreamExt;
use serde_json::json;
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::net::TcpStream;
use tokio_rustls::client::TlsStream;
use tokio_rustls::rustls;

use super::{runtime, value, Channel, Connector, Downloaded, Hooks, Identity, Inbound, OutFile, Result, Sent};
use crate::config::AccountConfig;

mod mime;

pub const IMAP_HOST: &str = "imap.gmail.com";
pub const IMAP_PORT: u16 = 993;
pub const SMTP_HOST: &str = "smtp.gmail.com";
pub const SMTP_PORT: u16 = 465;
pub const POLL_INTERVAL: Duration = Duration::from_secs(30);
/// Gmail's virtual mailbox of every message, archived or not: the scope for search and lookup.
pub const ALL_MAIL: &str = "[Gmail]/All Mail";
/// One IMAP session's ceiling, so a wedged server cannot stall the poll for good.
const SESSION_TIMEOUT: Duration = Duration::from_secs(300);

/// Where the connector reaches Gmail.
#[derive(Debug, Clone, PartialEq)]
pub struct Endpoints {
    pub imap_host: String,
    pub imap_port: u16,
    pub imap_tls: bool,
    pub smtp_host: String,
    pub smtp_port: u16,
    pub smtp_tls: bool,
    pub poll: Duration,
}

impl Default for Endpoints {
    fn default() -> Self {
        Self {
            imap_host: IMAP_HOST.into(),
            imap_port: IMAP_PORT,
            imap_tls: true,
            smtp_host: SMTP_HOST.into(),
            smtp_port: SMTP_PORT,
            smtp_tls: true,
            poll: POLL_INTERVAL,
        }
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
            smtp_host: var("AWM_GMAIL_SMTP_HOST").unwrap_or(d.smtp_host),
            smtp_port: var("AWM_GMAIL_SMTP_PORT").and_then(|p| p.parse().ok()).unwrap_or(d.smtp_port),
            smtp_tls: flag("AWM_GMAIL_SMTP_TLS").unwrap_or(d.smtp_tls),
            poll: d.poll,
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

/// `_split_subject`: an explicit `Subject:` first line, else the first non-empty line.
pub fn split_subject(text: &str) -> (String, String) {
    let lines: Vec<&str> = text.split('\n').collect();
    if lines[0].to_lowercase().starts_with("subject:") {
        let subject = lines[0]["subject:".len()..].trim();
        let mut rest = &lines[1..];
        if rest.first().is_some_and(|l| l.trim().is_empty()) {
            rest = &rest[1..];
        }
        let subject = if subject.is_empty() { "(no subject)" } else { subject };
        return (subject.to_string(), rest.join("\n"));
    }
    let first = lines.iter().map(|l| l.trim()).find(|l| !l.is_empty()).unwrap_or("(no subject)");
    (first.chars().take(78).collect(), text.to_string())
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

/// One account's mailbox.
pub struct GmailConnector {
    cfg: AccountConfig,
    hooks: Hooks,
    addr: String,
    ep: Endpoints,
    baseline: AtomicU32,
    live: AtomicBool,
}

impl GmailConnector {
    /// The connector for `cfg`, at Gmail's endpoints or the `AWM_GMAIL_*` overrides.
    pub fn create(cfg: AccountConfig, hooks: Hooks) -> anyhow::Result<Arc<dyn Connector>> {
        Ok(Self::with_endpoints(cfg, hooks, Endpoints::from_env()))
    }

    /// The connector for `cfg` at `ep`.
    pub fn with_endpoints(cfg: AccountConfig, hooks: Hooks, ep: Endpoints) -> Arc<Self> {
        let addr = cfg.address.clone().unwrap_or_default();
        Arc::new(Self { cfg, hooks, addr, ep, baseline: AtomicU32::new(0), live: AtomicBool::new(false) })
    }

    fn name(&self) -> &str {
        &self.cfg.name
    }

    fn err(&self, e: &ImapError) -> super::VerbError {
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
            kind: super::ChatKind::Dm,
            outside: false,
            resynced: false,
        }
    }

    /// One poll: the new messages and the new baseline. Baseline 0 only sets the baseline.
    async fn poll(&self, baseline: u32) -> std::result::Result<(Vec<mime::Row>, u32), ImapError> {
        self.session(move |s| {
            Box::pin(async move {
                s.examine("INBOX").await?;
                let uids = match s.uid_search("ALL").await {
                    Ok(u) => u,
                    Err(ImapError::No(_)) => return Ok((vec![], baseline)),
                    Err(e) => return Err(e),
                };
                let Some(&max_uid) = uids.iter().max() else { return Ok((vec![], baseline)) };
                if baseline == 0 {
                    return Ok((vec![], max_uid));
                }
                let mut fresh: Vec<u32> = uids.into_iter().filter(|&u| u > baseline).collect();
                fresh.sort_unstable();
                let mut out = vec![];
                for &uid in &fresh {
                    if let Some(raw) = fetch_body(s, &uid.to_string()).await? {
                        out.push(mime::row(&raw, &uid.to_string()));
                    }
                }
                Ok((out, fresh.last().copied().unwrap_or(baseline)))
            })
        })
        .await
    }

    #[cfg(test)]
    fn baseline(&self) -> u32 {
        self.baseline.load(Ordering::SeqCst)
    }
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
impl Connector for GmailConnector {
    fn platform(&self) -> &str {
        "gmail"
    }

    fn account(&self) -> &AccountConfig {
        &self.cfg
    }

    async fn start(self: Arc<Self>) -> anyhow::Result<()> {
        let name = self.name().to_string();
        if self.addr.is_empty() {
            tracing::error!("gmail[{name}] no address configured; account down");
            anyhow::bail!("gmail[{name}] no address configured; account down");
        }
        // A bad App Password or IMAP turned off is permanent: return, do not spin.
        match self.poll(0).await {
            Ok((_, b)) => {
                self.baseline.store(b, Ordering::SeqCst);
                self.live.store(true, Ordering::SeqCst);
                tracing::info!("gmail[{name}] connected as {} (baseline uid={b})", self.addr);
            }
            Err(e) => {
                tracing::error!("gmail[{name}] IMAP login failed; account down: {}", reason(&e));
                anyhow::bail!("gmail[{name}] IMAP login failed; account down: {}", reason(&e));
            }
        }
        let mut backoff = Backoff::doubling(1.0, 60.0);
        loop {
            tokio::time::sleep(self.ep.poll).await;
            match self.poll(self.baseline.load(Ordering::SeqCst)).await {
                Ok((rows, b)) => {
                    self.baseline.store(b, Ordering::SeqCst);
                    self.live.store(true, Ordering::SeqCst);
                    backoff.reset();
                    for r in rows {
                        (self.hooks.on_message)(self.inbound(r)).await;
                    }
                }
                Err(e) => {
                    self.live.store(false, Ordering::SeqCst);
                    let wait = backoff.wait();
                    tracing::warn!("gmail[{name}] poll error ({}); retry in {:.1}s", reason(&e), wait.as_secs_f64());
                    tokio::time::sleep(wait).await;
                }
            }
        }
    }

    /// Plain text to `channel`, an address. `thread` is a Message-ID to reply under.
    async fn send(&self, channel: &str, text: &str, thread: Option<&str>, _files: &[OutFile]) -> Result<Sent> {
        use lettre::message::header::{ContentType, To};
        use lettre::message::{Mailbox, Mailboxes};
        use lettre::transport::smtp::authentication::Credentials;
        use lettre::{AsyncSmtpTransport, AsyncTransport, Message, Tokio1Executor};

        let (subject, mut body) = split_subject(text);
        // EmailMessage ends the body with one newline; lettre's DATA terminator adds that one.
        if body.ends_with('\n') {
            body.pop();
            if body.ends_with('\r') {
                body.pop();
            }
        }
        let from: Mailbox = self.addr.parse().map_err(|e| value(format!("gmail[{}] bad from address {}: {e}", self.name(), py_repr(&self.addr))))?;
        let to: Mailboxes = channel.parse().map_err(|e| value(format!("gmail[{}] bad recipient {}: {e}", self.name(), py_repr(channel))))?;
        let msg_id = make_msgid();
        let mut b = Message::builder().from(from).mailbox(To::from(to)).subject(subject).message_id(Some(msg_id.clone()));
        if let Some(t) = thread.filter(|t| !t.is_empty()) {
            b = b.in_reply_to(t.to_string()).references(t.to_string());
        }
        let msg = b.header(ContentType::TEXT_PLAIN).body(body).map_err(|e| runtime(format!("gmail[{}] could not build the message: {e}", self.name())))?;
        let builder = if self.ep.smtp_tls {
            AsyncSmtpTransport::<Tokio1Executor>::relay(&self.ep.smtp_host).map_err(|e| runtime(format!("gmail[{}] smtp: {e}", self.name())))?
        } else {
            AsyncSmtpTransport::<Tokio1Executor>::builder_dangerous(&self.ep.smtp_host)
        };
        let transport = builder.port(self.ep.smtp_port).credentials(Credentials::new(self.addr.clone(), self.cfg.token.clone())).build();
        transport.send(msg).await.map_err(|e| runtime(format!("gmail[{}] smtp: {e}", self.name())))?;
        let mut out = Sent::new();
        out.insert("message_id".into(), json!(msg_id));
        out.insert("channel_id".into(), json!(channel));
        out.insert("ts".into(), json!(formatdate()));
        Ok(out)
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
        let Some(raw) = raw else {
            return Err(runtime(format!("gmail[{}] message {} not found", self.name(), py_repr(message_id))));
        };
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

    fn live(&self) -> bool {
        self.live.load(Ordering::SeqCst)
    }
}

#[cfg(test)]
mod tests;
