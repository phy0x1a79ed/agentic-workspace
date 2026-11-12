//! A room's files, moved straight between this node and the public edge.
//!
//! The bytes never cross the relay's door or the gateway: this node uploads to the room's link
//! as any guest does, and a message then names the file's id. The tether relay's how-to is the
//! protocol, so the part size and the sealing here must match its FILES and ENCRYPTION text in
//! `tether-relay/src/room.rs`.
//!
//! An encrypted room seals each file under a fresh key, in [`CHUNK`]-byte chunks. The key rides
//! in the sealed message that names the file, so the relay holds only ciphertext.

use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use std::time::Duration;

use serde_json::Value;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use super::crypto;

/// The plaintext bytes one sealed chunk carries.
pub const CHUNK: usize = 64 * 1024;
const TAG: usize = 16;
/// The plaintext bytes one upload request carries: under the ~100 MB a proxy on the way allows.
const PART: usize = 512 * CHUNK;
const TRIES: u32 = 3;

static CLIENT: LazyLock<reqwest::Client> = LazyLock::new(|| {
    reqwest::Client::builder().connect_timeout(Duration::from_secs(10)).read_timeout(Duration::from_secs(120)).build().unwrap_or_default()
});

/// Upload the file at `path` into the room at `link`, sealed under `key` when there is one.
/// Returns the file's id.
pub async fn upload(link: &str, path: &Path, key: Option<&[u8; 32]>) -> Result<String, String> {
    let mut file = tokio::fs::File::open(path).await.map_err(|e| format!("cannot read {}: {e}", path.display()))?;
    let total = file.metadata().await.map_err(|e| e.to_string())?.len();
    let (mut id, mut offset, mut read, mut index) = (None::<String>, 0u64, 0u64, 0u64);
    loop {
        let mut plain = vec![0u8; PART.min((total - read) as usize)];
        file.read_exact(&mut plain).await.map_err(|e| format!("cannot read {}: {e}", path.display()))?;
        read += plain.len() as u64;
        let last = read == total;
        let body = match key {
            None => plain,
            Some(key) => seal_part(key, &plain, &mut index, last),
        };
        let more = if last { "" } else { "more=1" };
        let url = match &id {
            None => format!("{link}/file?{more}"),
            Some(id) => format!("{link}/file/{id}?offset={offset}&{more}"),
        };
        let answer = post_part(&url, body).await?;
        id = Some(answer["id"].as_str().ok_or("the relay's answer named no file id")?.to_string());
        offset = answer["size"].as_u64().unwrap_or(0);
        if last {
            return Ok(id.unwrap_or_default());
        }
    }
}

fn seal_part(key: &[u8; 32], plain: &[u8], index: &mut u64, last: bool) -> Vec<u8> {
    let chunks: Vec<&[u8]> = if plain.is_empty() { vec![&[]] } else { plain.chunks(CHUNK).collect() };
    let mut out = Vec::with_capacity(plain.len() + chunks.len() * TAG);
    let final_at = chunks.len() - 1;
    for (i, chunk) in chunks.into_iter().enumerate() {
        out.extend(crypto::seal_chunk(key, *index, last && i == final_at, chunk));
        *index += 1;
    }
    out
}

async fn post_part(url: &str, body: Vec<u8>) -> Result<Value, String> {
    let mut why = String::new();
    for attempt in 0..TRIES {
        if attempt > 0 {
            tokio::time::sleep(Duration::from_secs(2 << attempt)).await;
        }
        match CLIENT.post(url).body(body.clone()).send().await {
            Ok(r) if r.status().is_success() => return r.json().await.map_err(|e| format!("the relay's answer is not JSON: {e}")),
            Ok(r) if r.status().is_server_error() || r.status() == reqwest::StatusCode::BAD_REQUEST => why = format!("{}: {}", r.status(), r.text().await.unwrap_or_default().trim()),
            Ok(r) => return Err(format!("the relay refused the file: {}: {}", r.status(), r.text().await.unwrap_or_default().trim())),
            Err(e) => why = e.to_string(),
        }
    }
    Err(format!("the upload failed after {TRIES} tries: {why}"))
}

/// Fetch file `id` from the room at `link` into `dest`, opened under `key` when there is one.
/// Returns its size. Nothing sits at `dest` unless the whole file arrived.
pub async fn download(link: &str, id: &str, dest: &Path, key: Option<&[u8; 32]>) -> Result<u64, String> {
    if parse_id(id).is_none() {
        return Err(format!("{id:?} is not a file id"));
    }
    let mut r = CLIENT.get(format!("{link}/file/{id}")).send().await.map_err(|e| e.to_string())?;
    if !r.status().is_success() {
        return Err(format!("the relay answered {} for the file", r.status()));
    }
    let dir = dest.parent().ok_or("no directory to save into")?;
    tokio::fs::create_dir_all(dir).await.map_err(|e| e.to_string())?;
    let part = dest.with_extension("part");
    let mut out = tokio::fs::File::create(&part).await.map_err(|e| e.to_string())?;
    let written = async {
        let (mut buf, mut index, mut size) = (Vec::new(), 0u64, 0u64);
        while let Some(piece) = r.chunk().await.map_err(|e| e.to_string())? {
            let Some(key) = key else {
                out.write_all(&piece).await.map_err(|e| e.to_string())?;
                size += piece.len() as u64;
                continue;
            };
            buf.extend_from_slice(&piece);
            // A full chunk with more after it is not the last one.
            while buf.len() > CHUNK + TAG {
                let plain = crypto::open_chunk(key, index, false, &buf[..CHUNK + TAG]).map_err(|e| e.0)?;
                out.write_all(&plain).await.map_err(|e| e.to_string())?;
                size += plain.len() as u64;
                buf.drain(..CHUNK + TAG);
                index += 1;
            }
        }
        if let Some(key) = key {
            let plain = crypto::open_chunk(key, index, true, &buf).map_err(|_| "the sealed file is cut short or does not open".to_string())?;
            out.write_all(&plain).await.map_err(|e| e.to_string())?;
            size += plain.len() as u64;
        }
        out.flush().await.map_err(|e| e.to_string())?;
        Ok::<u64, String>(size)
    }
    .await;
    match written {
        Ok(size) => {
            tokio::fs::rename(&part, dest).await.map_err(|e| e.to_string())?;
            Ok(size)
        }
        Err(e) => {
            let _ = tokio::fs::remove_file(&part).await;
            Err(e)
        }
    }
}

/// A file id exactly as the tether relay mints it: 32 lowercase hex digits.
pub fn parse_id(s: &str) -> Option<&str> {
    (s.len() == 32 && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))).then_some(s)
}

/// Where a file from the other side is saved: beside the room's transcript, under the message's
/// number and a name with nothing in it a shell or a path could read as structure.
pub fn saved_path(transcripts: &Path, room: &str, seq: i64, name: &str) -> PathBuf {
    let base = name.rsplit(['/', '\\']).next().unwrap_or("");
    let cleaned: String = base.chars().map(|c| if c.is_ascii_alphanumeric() || "._-".contains(c) { c } else { '_' }).collect();
    let cleaned = cleaned.trim_matches(|c| c == '.' || c == '_');
    let cleaned = if cleaned.is_empty() { "file" } else { cleaned };
    transcripts.join(format!("{room}.files")).join(format!("{seq}-{cleaned}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::Digest;

    #[test]
    fn a_saved_name_keeps_no_path_or_shell_structure() {
        let t = Path::new("/t");
        assert_eq!(saved_path(t, "r", 4, "../../etc/passwd"), Path::new("/t/r.files/4-passwd"));
        assert_eq!(saved_path(t, "r", 5, "a b;$(x).txt"), Path::new("/t/r.files/5-a_b___x_.txt"));
        assert_eq!(saved_path(t, "r", 6, "..."), Path::new("/t/r.files/6-file"));
    }

    /// The sealed layout a guest reproduces from the how-to: 64 KiB chunks, each with its
    /// 16-byte tag, the last one flagged. Pinned by the first bytes and the length.
    #[test]
    fn a_sealed_file_has_the_layout_the_how_to_gives() {
        let key = [7u8; 32];
        let plain: Vec<u8> = (0..CHUNK + 10).map(|i| i as u8).collect();
        let mut index = 0;
        let sealed = seal_part(&key, &plain, &mut index, true);
        assert_eq!((sealed.len(), index), (CHUNK + 10 + 2 * TAG, 2));
        assert_eq!(crypto::open_chunk(&key, 0, false, &sealed[..CHUNK + TAG]).unwrap(), plain[..CHUNK]);
        assert_eq!(crypto::open_chunk(&key, 1, true, &sealed[CHUNK + TAG..]).unwrap(), plain[CHUNK..]);
        assert!(crypto::open_chunk(&key, 1, false, &sealed[CHUNK + TAG..]).is_err(), "the last flag is bound");
        assert_eq!(hex::encode(sha2::Sha256::digest(&sealed)), "c5eb2eee8a0e2ffb4a8e61341952f1dcb772cc09f1590e7980eacae6633c8513");
        let mut index = 0;
        assert_eq!(seal_part(&key, &[], &mut index, true).len(), TAG, "an empty file is one empty last chunk");
    }
}
