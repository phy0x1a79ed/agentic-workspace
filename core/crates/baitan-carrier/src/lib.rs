//! baitan's carrier: one JSON envelope per line over any two-way byte stream.
//!
//! A `Link` is the only type the market and providers see. Nothing here redials: the binary
//! decides when to dial again, because HPC hosts lock accounts after failed 2FA prompts.

use baitan_proto::Envelope;
use std::path::Path;
use std::process::Stdio;
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tokio::process::Command;
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver, UnboundedSender};

/// Both halves of a stream. `rx` closes when the stream ends or a write fails.
pub struct Link {
    pub tx: UnboundedSender<Envelope>,
    pub rx: UnboundedReceiver<Envelope>,
}

pub fn from_stream<R, W>(reader: R, writer: W) -> Link
where
    R: AsyncRead + Send + Unpin + 'static,
    W: AsyncWrite + Send + Unpin + 'static,
{
    spawn_link(reader, writer, ())
}

/// `guard` lives as long as the reading half; dropping it ends whatever feeds the stream.
fn spawn_link<R, W, G>(reader: R, mut writer: W, guard: G) -> Link
where
    R: AsyncRead + Send + Unpin + 'static,
    W: AsyncWrite + Send + Unpin + 'static,
    G: Send + 'static,
{
    let (in_tx, in_rx) = unbounded_channel::<Envelope>();
    let (out_tx, mut out_rx) = unbounded_channel::<Envelope>();

    let reader_task = tokio::spawn(async move {
        let _guard = guard;
        let mut lines = BufReader::new(reader).lines();
        loop {
            tokio::select! {
                line = lines.next_line() => match line {
                    Ok(Some(line)) if line.trim().is_empty() => {}
                    Ok(Some(line)) => match Envelope::decode(&line) {
                        Ok(env) => {
                            if in_tx.send(env).is_err() {
                                break;
                            }
                        }
                        Err(e) => tracing::warn!("dropped undecodable line: {e}"),
                    },
                    Ok(None) | Err(_) => break,
                },
                _ = in_tx.closed() => break,
            }
        }
    });

    let abort = reader_task.abort_handle();
    tokio::spawn(async move {
        while let Some(env) = out_rx.recv().await {
            let mut line = env.encode();
            line.push('\n');
            if writer.write_all(line.as_bytes()).await.is_err() || writer.flush().await.is_err() {
                abort.abort();
                return;
            }
        }
        let _ = writer.shutdown().await;
    });

    Link { tx: out_tx, rx: in_rx }
}

pub fn stdio() -> Link {
    from_stream(tokio::io::stdin(), tokio::io::stdout())
}

pub async fn unix_connect(path: impl AsRef<Path>) -> std::io::Result<Link> {
    let (r, w) = UnixStream::connect(path).await?.into_split();
    Ok(from_stream(r, w))
}

pub struct Listener(UnixListener);

/// Binds `path`, replacing a stale socket file.
pub fn unix_listen(path: impl AsRef<Path>) -> std::io::Result<Listener> {
    let path = path.as_ref();
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let _ = std::fs::remove_file(path);
    UnixListener::bind(path).map(Listener)
}

impl Listener {
    pub async fn accept(&self) -> std::io::Result<Link> {
        let (r, w) = self.0.accept().await?.0.into_split();
        Ok(from_stream(r, w))
    }
}

/// Runs `sh -c cmd` and speaks over its stdio. The child is killed when the link is dropped.
pub fn dial(cmd: &str) -> std::io::Result<Link> {
    let mut child = Command::new("sh")
        .arg("-c")
        .arg(cmd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .kill_on_drop(true)
        .spawn()?;
    let stdin = child.stdin.take().expect("piped");
    let stdout = child.stdout.take().expect("piped");
    Ok(spawn_link(stdout, stdin, child))
}

#[cfg(test)]
mod tests {
    use super::*;
    use baitan_proto::Msg;
    use serde_json::Value;
    use std::time::Duration;

    fn env(n: &str) -> Envelope {
        Envelope::new(n, "peer", &Msg::Return(Value::Null))
    }

    async fn recv(l: &mut Link) -> Option<Envelope> {
        tokio::time::timeout(Duration::from_secs(5), l.rx.recv()).await.expect("timed out")
    }

    #[tokio::test]
    async fn round_trip_over_unix_socket() {
        let dir = tempfile::tempdir().unwrap();
        let sock = dir.path().join("s");
        let listener = unix_listen(&sock).unwrap();
        let mut client = unix_connect(&sock).await.unwrap();
        let mut server = listener.accept().await.unwrap();
        let e = env("a");
        client.tx.send(e.clone()).unwrap();
        assert_eq!(recv(&mut server).await.unwrap(), e);
        let r = e.reply(&Msg::Return(Value::Null));
        server.tx.send(r.clone()).unwrap();
        assert_eq!(recv(&mut client).await.unwrap(), r);
    }

    #[tokio::test]
    async fn bad_line_is_dropped_and_eof_closes_rx() {
        let (a, b) = tokio::io::duplex(4096);
        let (ar, aw) = tokio::io::split(a);
        let mut link = from_stream(ar, aw);
        let (br, mut bw) = tokio::io::split(b);
        let good = env("g");
        bw.write_all(format!("not json\n{}\n", good.encode()).as_bytes()).await.unwrap();
        assert_eq!(recv(&mut link).await.unwrap(), good);
        drop((br, bw));
        assert!(recv(&mut link).await.is_none());
    }

    #[tokio::test]
    async fn dial_echoes_through_cat() {
        let mut link = dial("cat").unwrap();
        let e = env("c");
        link.tx.send(e.clone()).unwrap();
        assert_eq!(recv(&mut link).await.unwrap(), e);
    }

    #[tokio::test]
    async fn dropping_a_dialed_link_kills_the_child() {
        let dir = tempfile::tempdir().unwrap();
        let pidfile = dir.path().join("pid");
        let link = dial(&format!("echo $$ > {}; exec cat", pidfile.display())).unwrap();
        let pid = loop {
            if let Ok(s) = std::fs::read_to_string(&pidfile) {
                if let Ok(p) = s.trim().parse::<i32>() {
                    break p;
                }
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        };
        drop(link);
        let mut gone = false;
        for _ in 0..200 {
            let st = std::fs::read_to_string(format!("/proc/{pid}/stat")).unwrap_or_default();
            if st.is_empty() || st.contains(") Z") {
                gone = true;
                break;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        assert!(gone, "child {pid} outlived its link");
    }

    #[tokio::test]
    async fn redial_after_a_drop_gives_a_working_link() {
        let dir = tempfile::tempdir().unwrap();
        let sock = dir.path().join("s");
        let listener = unix_listen(&sock).unwrap();
        let first = unix_connect(&sock).await.unwrap();
        let mut server1 = listener.accept().await.unwrap();
        drop(first);
        assert!(recv(&mut server1).await.is_none());
        let second = unix_connect(&sock).await.unwrap();
        let mut server2 = listener.accept().await.unwrap();
        let e = env("again");
        second.tx.send(e.clone()).unwrap();
        assert_eq!(recv(&mut server2).await.unwrap(), e);
    }
}
