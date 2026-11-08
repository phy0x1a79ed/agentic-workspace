//! One verb to the operator daemon over its control socket: a JSON line in, a JSON line out,
//! one connection per request. The socket sits in this user's 0700 state directory.

use std::path::Path;
use std::time::Duration;

use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;

/// The socket is not there, nothing listens on it, or the daemon closed without a reply.
#[derive(Debug, Clone)]
pub struct Unavailable(pub String);

pub async fn call(socket: &Path, verb: &str, args: Value, timeout: Duration) -> Result<Value, Unavailable> {
    let mut stream = UnixStream::connect(socket).await.map_err(|e| Unavailable(e.to_string()))?;
    let request = json!({"verb": verb, "args": args}).to_string() + "\n";
    stream.write_all(request.as_bytes()).await.map_err(|e| Unavailable(e.to_string()))?;
    let mut line = String::new();
    let read = tokio::time::timeout(timeout, BufReader::new(stream).read_line(&mut line)).await;
    match read {
        Err(_) => Err(Unavailable(format!("the daemon did not answer {verb} within {}s", timeout.as_secs()))),
        Ok(Err(e)) => Err(Unavailable(e.to_string())),
        Ok(Ok(0)) => Err(Unavailable("daemon closed the connection without replying".into())),
        Ok(Ok(_)) => serde_json::from_str(&line).map_err(|e| Unavailable(format!("the daemon answered {verb} with no JSON: {e}"))),
    }
}
