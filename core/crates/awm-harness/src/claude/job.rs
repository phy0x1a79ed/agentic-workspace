//! Typing one line into a background session that is named by its job id.
//!
//! A caller that started a session itself holds the job id, and addresses the session straight
//! from the daemon roster. The handshake in [`super::pty::connect`] still checks whose REPL
//! answers the socket, which is what stops a recycled socket path from receiving the text.
//!
//! The caller decides whether it may reach the job. This module only checks that the job is
//! alive and in a status where a paste queues as a prompt.

use std::future::Future;

use awm_plugin_sdk::procfs;
use tokio::io::{AsyncRead, AsyncWrite};

use super::pty::{connect, connect_with, Connection, DaemonError, DaemonLane};
use super::records::{py_truthy_str, read_session_record};
use super::{roster, ClaudeHome};

/// The only statuses in which a paste queues as a prompt. Anything else (a dialog waiting for an
/// answer, a status this code has never seen) is refused.
pub const SAFE_STATUSES: [&str; 2] = ["idle", "busy"];

/// The job is gone, unreachable, or at a prompt that a paste would answer.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct JobUnavailable(pub String);

/// Why a line was not sent.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum SendError {
    #[error("send_line takes one line of text")]
    MultiLine,
    #[error(transparent)]
    Unavailable(#[from] JobUnavailable),
    #[error(transparent)]
    Daemon(#[from] DaemonError),
}

/// The PTY lane of the live background session `job` under `home`, or [`JobUnavailable`].
pub fn lane_for(home: &ClaudeHome, job: &str) -> Result<DaemonLane, JobUnavailable> {
    let unavailable = |why: String| JobUnavailable(why);
    let found = roster::load(home)
        .into_iter()
        .find(|j| j.short == job)
        .ok_or_else(|| unavailable(format!("job {job} is not in the daemon roster")))?;
    let lane = DaemonLane::from_job(&found).ok_or_else(|| unavailable(format!("job {job} has no reachable PTY")))?;
    if procfs::start_ticks(lane.repl_pid) != found.repl_proc_start {
        return Err(unavailable(format!("job {job} is no longer running")));
    }
    let record = read_session_record(home, lane.repl_pid).map_err(|e| unavailable(e.to_string()))?;
    let status = py_truthy_str(record.get("status")).unwrap_or_default();
    if !SAFE_STATUSES.contains(&status.as_str()) {
        let state = if status.is_empty() { "in an unknown state" } else { &status };
        return Err(unavailable(format!("job {job} is {state}, not idle or busy")));
    }
    Ok(lane)
}

/// Paste `text` into the session `job` and press Enter.
///
/// The text is a single line: a newline inside a bracketed paste would be kept as part of the
/// prompt, so one is refused rather than silently rewritten.
pub async fn send_line(home: &ClaudeHome, job: &str, text: &str) -> Result<(), SendError> {
    one_line(text)?;
    let lane = lane_for(home, job)?;
    paste_and_commit(connect(&lane).await?, text).await
}

/// [`send_line`] over a stream `open` dials for the lane.
pub async fn send_line_with<S, F, Fut>(home: &ClaudeHome, job: &str, text: &str, open: F) -> Result<(), SendError>
where
    S: AsyncRead + AsyncWrite + Unpin,
    F: FnOnce(&DaemonLane) -> Fut,
    Fut: Future<Output = Result<S, DaemonError>>,
{
    one_line(text)?;
    let lane = lane_for(home, job)?;
    let stream = open(&lane).await?;
    paste_and_commit(connect_with(&lane, stream).await?, text).await
}

fn one_line(text: &str) -> Result<(), SendError> {
    if text.contains(['\n', '\r']) {
        Err(SendError::MultiLine)
    } else {
        Ok(())
    }
}

async fn paste_and_commit<S: AsyncRead + AsyncWrite + Unpin>(mut conn: Connection<S>, text: &str) -> Result<(), SendError> {
    let sent = async {
        conn.write(text).await?;
        conn.commit().await
    }
    .await;
    conn.close().await;
    Ok(sent?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::claude::pty::{frame, FrameReader, CTL, RAW};
    use crate::claude::records::tests::{home, me, own_start, record};
    use serde_json::{json, Value};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::UnixStream;

    fn seed(h: &ClaudeHome, status: &str, proc_start: Option<&str>, sock: &str) {
        let start = proc_start.map(String::from).unwrap_or_else(own_start);
        let worker = json!({"replPid": me(), "replProcStart": start, "ptySock": sock, "ptyAuth": "tok",
                            "sessionId": "sess-1", "startedAt": 1, "decModes": [2004]});
        std::fs::write(h.roster(), json!({"supervisorPid": me(), "workers": {"abc12345": worker}}).to_string()).unwrap();
        std::fs::create_dir_all(h.jobs().join("abc12345")).unwrap();
        std::fs::write(h.job_state("abc12345"), json!({"name": "front-door"}).to_string()).unwrap();
        record(h, me(), json!({"kind": "bg", "status": status, "procStart": own_start()}));
    }

    fn seeded(status: &str) -> (tempfile::TempDir, ClaudeHome) {
        let (d, h) = home();
        seed(&h, status, None, "/run/x.sock");
        (d, h)
    }

    fn refusal(h: &ClaudeHome, job: &str) -> String {
        lane_for(h, job).unwrap_err().0
    }

    #[test]
    fn lane_for_addresses_a_live_job_from_the_roster() {
        let (_d, h) = seeded("idle");
        let lane = lane_for(&h, "abc12345").unwrap();
        assert_eq!(lane.sock, std::path::Path::new("/run/x.sock"));
        assert_eq!((lane.auth.as_str(), lane.session_id.as_str(), lane.name.as_deref()), ("tok", "sess-1", Some("front-door")));
        assert_eq!((lane.dec_modes.as_slice(), lane.repl_pid), (&[2004][..], me()));
    }

    #[test]
    fn lane_for_refuses_an_unknown_job() {
        let (_d, h) = seeded("idle");
        assert!(refusal(&h, "nope0000").contains("not in the daemon roster"));
    }

    #[test]
    fn lane_for_refuses_a_job_whose_process_is_gone() {
        let (_d, h) = home();
        seed(&h, "idle", Some("1"), "/run/x.sock");
        assert!(refusal(&h, "abc12345").contains("no longer running"));
    }

    #[test]
    fn lane_for_accepts_only_the_safe_statuses() {
        for status in ["idle", "busy"] {
            let (_d, h) = seeded(status);
            assert_eq!(lane_for(&h, "abc12345").unwrap().session_id, "sess-1", "{status}");
        }
    }

    #[test]
    fn lane_for_refuses_every_other_status() {
        for status in ["waiting", "", "needs-input", "shutting-down"] {
            let (_d, h) = seeded(status);
            let why = refusal(&h, "abc12345");
            assert!(why.contains("not idle or busy"), "{status}: {why}");
        }
        let (_d, h) = seeded("");
        assert!(refusal(&h, "abc12345").contains("is in an unknown state"));
    }

    #[test]
    fn lane_for_refuses_a_job_with_no_pty() {
        let (_d, h) = home();
        seed(&h, "idle", None, "");
        assert!(refusal(&h, "abc12345").contains("no reachable PTY"));
    }

    #[test]
    fn a_job_lane_needs_a_socket_a_token_and_a_pid() {
        let (_d, h) = seeded("idle");
        let job = roster::load(&h).remove(0);
        assert!(DaemonLane::from_job(&job).is_some());
        assert!(DaemonLane::from_job(&roster::Job { pty_sock: None, ..job.clone() }).is_none());
        assert!(DaemonLane::from_job(&roster::Job { pty_auth: Some(String::new()), ..job.clone() }).is_none());
        assert!(DaemonLane::from_job(&roster::Job { repl_pid: None, ..job }).is_none());
    }

    // A host that greets as this process's REPL, then returns every frame the client sent.
    fn host(stream: UnixStream) -> tokio::task::JoinHandle<Vec<(u8, Vec<u8>)>> {
        tokio::spawn(async move {
            let mut stream = stream;
            let hello = [
                frame(CTL, json!({"t": "hello", "replPid": me()}).to_string().as_bytes()),
                frame(CTL, br#"{"t": "live"}"#),
            ]
            .concat();
            let _ = stream.write_all(&hello).await;
            let (mut reader, mut sent, mut buf) = (FrameReader::new(), Vec::new(), vec![0u8; 65536]);
            while let Ok(n @ 1..) = stream.read(&mut buf).await {
                sent.extend(reader.feed(&buf[..n]));
            }
            sent
        })
    }

    #[tokio::test]
    async fn send_line_pastes_then_commits() {
        let (_d, h) = seeded("idle");
        let (client, server) = UnixStream::pair().unwrap();
        let peer = host(server);
        let mut opened = None;
        send_line_with(&h, "abc12345", "3 new cards, run door list", |lane| {
            opened = Some(lane.session_id.clone());
            async { Ok(client) }
        })
        .await
        .unwrap();
        assert_eq!(opened.as_deref(), Some("sess-1"));
        let frames = peer.await.unwrap();
        let raws: Vec<_> = frames.iter().filter(|(k, _)| *k == RAW).map(|(_, p)| p.clone()).collect();
        assert_eq!(raws, [b"\x1b[200~3 new cards, run door list\x1b[201~".to_vec(), b"\r".to_vec()]);
        let auth: Value = serde_json::from_slice(&frames[1].1).unwrap();
        assert_eq!((auth["t"].as_str(), auth["token"].as_str()), (Some("auth"), Some("tok")));
    }

    #[tokio::test]
    async fn send_line_refuses_multi_line_text_before_opening_anything() {
        let (_d, h) = seeded("idle");
        for text in ["a\nb", "a\rb"] {
            let err = send_line_with(&h, "abc12345", text, |_| async { Err::<UnixStream, _>(DaemonError("opened".into())) })
                .await
                .unwrap_err();
            assert_eq!(err, SendError::MultiLine);
        }
    }
}
