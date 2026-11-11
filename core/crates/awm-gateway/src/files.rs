//! Files a tool carries in its reply, written to the caller's own node.
//!
//! A tool that returns file content (`social download_attachments`) cannot name a path: it may
//! run on another node, or in a service with a private `/tmp`, and a path there is no path for
//! the caller. It returns `{"files": [{"filename", "b64", ...}]}` instead. The gateway of the node
//! the session runs on turns each `b64` entry into a file in a fresh temp dir and rewrites the
//! reply to name it: `files[].path`, `dir`, and `node` set to this node. The gateway runs
//! outside any private `/tmp`, so the session reads what it wrote.
//!
//! Only a call that starts on this node from a session surface is rewritten. A call that
//! arrives from another node carries its `b64` back to the node that asked.
//!
//! The same holds the other way. A verb that takes a local file (`social send file=<path>`) is
//! read here, by the gateway of the session's node, and its `file` becomes a `files` entry with
//! the bytes inline: the tool may run on another node, or in a unit that cannot read the path.

use std::collections::HashSet;
use std::os::unix::fs::DirBuilderExt;
use std::path::{Path, PathBuf};

use base64::Engine;
use serde_json::{json, Value};

/// What one call carries inline, raw bytes before base64.
pub const MAX_INLINE_BYTES: u64 = 24 << 20;

/// The largest request body `/invoke` takes: a full inline file, base64 and framing.
pub const MAX_REQUEST_BYTES: usize = 64 << 20;

/// The verbs whose `file` argument is a path on the caller's node: `(tool, verb)`.
const FILE_VERBS: &[(&str, &str)] = &[("social", "send"), ("seats", "send"), ("productivity", "draft"), ("productivity", "chat_stage"), ("productivity", "bucket_put")];

/// Read the local files a verb in [`FILE_VERBS`] names, `file` and each path string in `files`,
/// and carry their bytes in `files` instead.
/// `name` and `args` are the `/invoke` body: a tool with a `{verb, args}` envelope, or a flat `<tool>_<verb>`.
pub fn inline_file_arg(name: &str, args: &mut Value) -> Result<(), String> {
    let inner = match FILE_VERBS.iter().find(|(tool, verb)| name == format!("{tool}_{verb}") || (name == *tool && args["verb"] == *verb)) {
        Some(_) if args.get("verb").is_some() => args.get_mut("args"),
        Some(_) => Some(args),
        None => return Ok(()),
    };
    let Some(Value::Object(inner)) = inner else { return Ok(()) };
    if let Some(Value::Array(files)) = inner.get_mut("files") {
        for f in files.iter_mut().filter(|f| f.is_string()) {
            *f = inline(f.as_str().unwrap_or_default())?;
        }
    }
    let Some(Value::String(path)) = inner.remove("file").filter(|f| f.as_str().is_some_and(|s| !s.is_empty())) else { return Ok(()) };
    let entry = inline(&path)?;
    match inner.entry("files").or_insert_with(|| json!([])) {
        Value::Array(files) => files.push(entry),
        other => *other = json!([entry]),
    }
    Ok(())
}

/// One local file as a `files` entry with its bytes inline.
fn inline(path: &str) -> Result<Value, String> {
    let path = Path::new(path);
    if !path.is_absolute() {
        return Err(format!("file must be an absolute path on this node, not {}", path.display()));
    }
    let meta = std::fs::metadata(path).map_err(|e| format!("no local file {}: {e}", path.display()))?;
    if !meta.is_file() {
        return Err(format!("{} is not a file", path.display()));
    }
    if meta.len() > MAX_INLINE_BYTES {
        return Err(format!("{} is {} bytes, over the {MAX_INLINE_BYTES} a call carries", path.display(), meta.len()));
    }
    let bytes = std::fs::read(path).map_err(|e| format!("cannot read {}: {e}", path.display()))?;
    Ok(json!({
        "filename": path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default(),
        "b64": base64::engine::general_purpose::STANDARD.encode(bytes),
    }))
}

/// Rewrite `result` when it holds `files` with inline content. Any other reply comes back as it was.
pub fn localize(result: &str, node: &str) -> String {
    localize_in(result, node, &std::env::temp_dir())
}

fn localize_in(result: &str, node: &str, tmp: &Path) -> String {
    let Ok(Value::Object(mut reply)) = serde_json::from_str::<Value>(result) else { return result.to_string() };
    let Some(Value::Array(files)) = reply.get_mut("files") else { return result.to_string() };
    if !files.iter().any(|f| f.get("b64").is_some_and(Value::is_string)) {
        return result.to_string();
    }
    let dir = match make_dir(tmp) {
        Ok(dir) => dir,
        Err(e) => return json!({"error": format!("cannot make a temp dir for the files in the reply: {e}")}).to_string(),
    };
    let mut used = HashSet::new();
    for (idx, file) in files.iter_mut().enumerate() {
        let Some(entry) = file.as_object_mut() else { continue };
        let Some(Value::String(b64)) = entry.remove("b64") else { continue };
        let name = unique(&safe_name(entry.get("filename").and_then(Value::as_str).unwrap_or(""), idx), &mut used);
        let written = base64::engine::general_purpose::STANDARD
            .decode(b64.trim())
            .map_err(|e| format!("bad base64: {e}"))
            .and_then(|bytes| {
                let path = dir.join(&name);
                std::fs::write(&path, &bytes).map(|_| (path, bytes.len())).map_err(|e| e.to_string())
            });
        match written {
            Ok((path, size)) => {
                entry.insert("path".into(), json!(path.to_string_lossy()));
                entry.insert("size".into(), json!(size));
            }
            Err(e) => {
                entry.insert("error".into(), json!(e));
            }
        }
    }
    reply.insert("dir".into(), json!(dir.to_string_lossy()));
    reply.insert("node".into(), json!(node));
    Value::Object(reply).to_string()
}

fn make_dir(tmp: &Path) -> std::io::Result<PathBuf> {
    let dir = tmp.join(format!("awm-files-{}", &uuid::Uuid::new_v4().simple().to_string()[..12]));
    std::fs::DirBuilder::new().mode(0o700).create(&dir)?;
    Ok(dir)
}

/// A basename the reply's author cannot steer out of the dir: no separators, no leading dot.
fn safe_name(name: &str, idx: usize) -> String {
    let base = name.rsplit(['/', '\\']).next().unwrap_or("");
    let cleaned: String = base.chars().map(|c| if c.is_ascii_alphanumeric() || "._-".contains(c) { c } else { '_' }).collect();
    let cleaned = cleaned.trim_matches(|c| c == '.' || c == '_').to_string();
    if cleaned.is_empty() { format!("file-{idx}") } else { cleaned }
}

fn unique(name: &str, used: &mut HashSet<String>) -> String {
    let (stem, ext) = match name.split_once('.') {
        Some((s, e)) => (s.to_string(), format!(".{e}")),
        None => (name.to_string(), String::new()),
    };
    let mut candidate = name.to_string();
    let mut n = 1;
    while !used.insert(candidate.clone()) {
        candidate = format!("{stem}-{n}{ext}");
        n += 1;
    }
    candidate
}

#[cfg(test)]
mod tests {
    use super::*;

    fn b64(bytes: &[u8]) -> String {
        base64::engine::general_purpose::STANDARD.encode(bytes)
    }

    #[test]
    fn inline_files_become_files_on_this_node() {
        let tmp = tempfile::tempdir().unwrap();
        let reply = json!({"node": "alcor", "count": 1, "files": [{"filename": "a b.csv", "mime": "text/csv", "b64": b64(b"x,y\n1,2\n")}]}).to_string();
        let out: Value = serde_json::from_str(&localize_in(&reply, "altair", tmp.path())).unwrap();
        assert_eq!(out["node"], "altair");
        let file = &out["files"][0];
        assert!(file.get("b64").is_none());
        assert_eq!(file["size"], 8);
        let path = file["path"].as_str().unwrap();
        assert_eq!(std::fs::read(path).unwrap(), b"x,y\n1,2\n");
        assert_eq!(Path::new(path).parent().unwrap(), Path::new(out["dir"].as_str().unwrap()));
        assert!(path.starts_with(tmp.path().to_str().unwrap()));
        assert!(path.ends_with("/a_b.csv"));
    }

    fn send_with(file: &Path, files: Value) -> Value {
        json!({"verb": "send", "args": {"account": "a", "channel": "c", "file": file, "files": files}})
    }

    #[test]
    fn a_local_file_becomes_inline_bytes() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("page.html");
        std::fs::write(&path, b"<p>hi</p>").unwrap();
        let mut args = send_with(&path, json!([{"filename": "n.txt", "text": "t"}]));
        inline_file_arg("social", &mut args).unwrap();
        let inner = &args["args"];
        assert!(inner.get("file").is_none());
        assert_eq!(inner["files"][0]["filename"], "n.txt");
        assert_eq!(inner["files"][1]["filename"], "page.html");
        assert_eq!(inner["files"][1]["b64"], b64(b"<p>hi</p>"));
        // The flat spelling carries the args at the top.
        let mut flat = json!({"account": "a", "file": path});
        inline_file_arg("social_send", &mut flat).unwrap();
        assert_eq!(flat["files"][0]["filename"], "page.html");
        // A path string in `files` is read too, in place.
        let mut listed = json!({"verb": "send", "args": {"files": [path, {"filename": "n.txt", "text": "t"}]}});
        inline_file_arg("seats", &mut listed).unwrap();
        assert_eq!(listed["args"]["files"][0], json!({"filename": "page.html", "b64": b64(b"<p>hi</p>")}));
        assert_eq!(listed["args"]["files"][1]["filename"], "n.txt");
        let mut bad = json!({"verb": "send", "args": {"files": ["/nonexistent/f.md"]}});
        assert!(inline_file_arg("seats", &mut bad).unwrap_err().contains("no local file"));
    }

    #[test]
    fn other_verbs_and_missing_or_relative_files_are_left_or_refused() {
        let mut other = json!({"verb": "fetch", "args": {"file": "/x"}});
        inline_file_arg("social", &mut other).unwrap();
        assert_eq!(other["args"]["file"], "/x");
        let mut tool = json!({"verb": "send", "args": {"file": "/x"}});
        inline_file_arg("journal", &mut tool).unwrap();
        assert_eq!(tool["args"]["file"], "/x");
        let mut none = json!({"verb": "send", "args": {"text": "hi"}});
        inline_file_arg("social", &mut none).unwrap();
        assert_eq!(none["args"], json!({"text": "hi"}));
        let mut gone = json!({"verb": "send", "args": {"file": "/nonexistent/f.csv"}});
        assert!(inline_file_arg("social", &mut gone).unwrap_err().contains("no local file"));
        let mut rel = json!({"verb": "send", "args": {"file": "f.csv"}});
        assert!(inline_file_arg("social", &mut rel).unwrap_err().contains("absolute"));
        let dir = tempfile::tempdir().unwrap();
        let mut folder = json!({"verb": "send", "args": {"file": dir.path()}});
        assert!(inline_file_arg("social", &mut folder).unwrap_err().contains("not a file"));
    }

    #[test]
    fn a_reply_is_localized_safely_and_one_without_inline_files_is_untouched() {
        for text in ["not json", r#"{"files": [{"path": "/x"}]}"#, r#"{"count": 0}"#, r#"["b64"]"#] {
            assert_eq!(localize_in(text, "altair", Path::new("/nonexistent")), text);
        }
        let tmp = tempfile::tempdir().unwrap();
        let reply = json!({"files": [
            {"filename": "../../etc/passwd", "b64": b64(b"1")},
            {"filename": "d.txt", "b64": b64(b"2")},
            {"filename": "d.txt", "b64": b64(b"3")},
            {"filename": "", "b64": b64(b"4")},
            {"filename": "a", "b64": "!!"},
        ]}).to_string();
        let out: Value = serde_json::from_str(&localize_in(&reply, "n", tmp.path())).unwrap();
        let files = out["files"].as_array().unwrap();
        let dir = out["dir"].as_str().unwrap();
        let names: Vec<&str> = files[..4].iter().map(|f| f["path"].as_str().unwrap().strip_prefix(&format!("{dir}/")).unwrap()).collect();
        assert_eq!(names, ["passwd", "d.txt", "d-1.txt", "file-3"], "a name cannot leave the dir and a clash keeps both");
        assert!(files[4]["error"].as_str().unwrap().contains("base64") && files[4].get("path").is_none());
    }
}
