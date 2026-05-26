use crate::frames::Frame;
use std::process::Stdio;
use tokio::io::{AsyncRead, AsyncReadExt, BufReader};
use tokio::process::Command;
use tokio::sync::mpsc;

const CHUNK_SIZE: usize = 16 * 1024;

/// Spawn `sh -c "<cmd>"`, pump stdout/stderr to `out` as `Stdout`/`Stderr`
/// frames, emit a final `Exit` (or `Error` on spawn/wait failure).
pub async fn run_exec(id: u64, cmd: String, out: mpsc::Sender<Frame>) {
    let mut child = match Command::new("sh")
        .arg("-c")
        .arg(&cmd)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(Stdio::null())
        .spawn()
    {
        Ok(c) => c,
        Err(e) => {
            let _ = out
                .send(Frame::Error {
                    id,
                    message: format!("spawn failed: {e}"),
                })
                .await;
            return;
        }
    };

    let stdout = child.stdout.take().expect("stdout was piped");
    let stderr = child.stderr.take().expect("stderr was piped");

    let stdout_tx = out.clone();
    let stdout_task = tokio::spawn(async move {
        pump(id, stdout, &stdout_tx, true).await;
    });

    let stderr_tx = out.clone();
    let stderr_task = tokio::spawn(async move {
        pump(id, stderr, &stderr_tx, false).await;
    });

    let wait_res = child.wait().await;
    // Drain pumps before sending Exit so Stdout/Stderr land first.
    let _ = stdout_task.await;
    let _ = stderr_task.await;

    let frame = match wait_res {
        Ok(status) => Frame::Exit {
            id,
            code: status.code(),
            signal: signal_of(&status),
        },
        Err(e) => Frame::Error {
            id,
            message: format!("wait failed: {e}"),
        },
    };
    let _ = out.send(frame).await;
}

async fn pump<R: AsyncRead + Unpin>(id: u64, reader: R, out: &mpsc::Sender<Frame>, is_stdout: bool) {
    let mut reader = BufReader::new(reader);
    let mut buf = vec![0u8; CHUNK_SIZE];
    loop {
        let n = match reader.read(&mut buf).await {
            Ok(0) => return,
            Ok(n) => n,
            Err(_) => return,
        };
        let frame = if is_stdout {
            Frame::Stdout {
                id,
                data: buf[..n].to_vec(),
            }
        } else {
            Frame::Stderr {
                id,
                data: buf[..n].to_vec(),
            }
        };
        if out.send(frame).await.is_err() {
            return;
        }
    }
}

#[cfg(unix)]
fn signal_of(status: &std::process::ExitStatus) -> Option<i32> {
    use std::os::unix::process::ExitStatusExt;
    status.signal()
}

#[cfg(not(unix))]
fn signal_of(_status: &std::process::ExitStatus) -> Option<i32> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::time::{timeout, Duration};

    async fn collect(cmd: &str) -> Vec<Frame> {
        let (tx, mut rx) = mpsc::channel(64);
        let task = tokio::spawn(run_exec(1, cmd.to_string(), tx));
        let mut frames = Vec::new();
        timeout(Duration::from_secs(5), async {
            while let Some(f) = rx.recv().await {
                let is_terminal = matches!(f, Frame::Exit { .. } | Frame::Error { .. });
                frames.push(f);
                if is_terminal {
                    break;
                }
            }
        })
        .await
        .expect("exec timed out");
        let _ = task.await;
        frames
    }

    fn collect_stdout(frames: &[Frame]) -> Vec<u8> {
        let mut out = Vec::new();
        for f in frames {
            if let Frame::Stdout { data, .. } = f {
                out.extend_from_slice(data);
            }
        }
        out
    }

    fn collect_stderr(frames: &[Frame]) -> Vec<u8> {
        let mut out = Vec::new();
        for f in frames {
            if let Frame::Stderr { data, .. } = f {
                out.extend_from_slice(data);
            }
        }
        out
    }

    #[tokio::test]
    async fn echo_produces_stdout_and_clean_exit() {
        let frames = collect("echo hi").await;
        assert_eq!(collect_stdout(&frames), b"hi\n");
        assert_eq!(collect_stderr(&frames), b"");
        assert!(matches!(
            frames.last(),
            Some(Frame::Exit {
                code: Some(0),
                ..
            })
        ));
    }

    #[tokio::test]
    async fn exit_code_propagates() {
        let frames = collect("exit 7").await;
        assert!(matches!(
            frames.last(),
            Some(Frame::Exit {
                code: Some(7),
                ..
            })
        ));
    }

    #[tokio::test]
    async fn stderr_routes_correctly() {
        let frames = collect("printf err >&2").await;
        assert_eq!(collect_stderr(&frames), b"err");
        assert_eq!(collect_stdout(&frames), b"");
        assert!(matches!(
            frames.last(),
            Some(Frame::Exit {
                code: Some(0),
                ..
            })
        ));
    }

    #[tokio::test]
    async fn stdout_and_stderr_both_captured() {
        let frames = collect("printf out; printf err >&2").await;
        assert_eq!(collect_stdout(&frames), b"out");
        assert_eq!(collect_stderr(&frames), b"err");
    }

    #[tokio::test]
    async fn large_output_chunked() {
        // 50 KiB > CHUNK_SIZE; should produce multiple Stdout frames.
        let frames = collect("yes x | head -c 51200").await;
        let count = frames
            .iter()
            .filter(|f| matches!(f, Frame::Stdout { .. }))
            .count();
        assert!(count >= 3, "expected ≥3 stdout chunks for 50 KiB, got {count}");
        assert_eq!(collect_stdout(&frames).len(), 51200);
    }
}
