//! Move aged sessions into the archive, put one back, and prune the archive.
//!
//! The session is the unit, never the file: a transcript and its sidecar move together, so a
//! sweep cannot orphan a sidecar the way the shell script it replaced did.
//!
//! **CAUTION** The sweep deletes the live files. Each archived copy is decompressed and
//! compared with its source before any source of that session goes. A session with a failed
//! check keeps every live file and counts a failure.

use std::collections::HashSet;
use std::fs::{File, FileTimes, OpenOptions};
use std::io::{BufReader, Read, Write};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::time::Instant;

use flate2::read::MultiGzDecoder;
use flate2::write::GzEncoder;
use flate2::Compression;
use serde_json::{json, Value};

use crate::layout::{sessions, walk_files, Session, Side, Tree};
use crate::Fail;

pub const DEFAULT_RETENTION_DAYS: f64 = 7.0;
const FILE_MODE: u32 = 0o600;
const DIR_MODE: u32 = 0o700;

pub fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
}

/// Python's `round(x, 2)`, near enough for a duration.
fn round2(x: f64) -> f64 {
    (x * 100.0).round() / 100.0
}

/// Sessions under `root` whose newest file is older than `days`.
pub fn aged(root: &Path, side: Side, days: f64, hide: Option<&dyn Fn(&str) -> bool>) -> Vec<Session> {
    let cutoff = now() - days * 86400.0;
    sessions(root, side, hide)
        .into_iter()
        .filter(|s| {
            let m = s.mtime();
            0.0 < m && m < cutoff
        })
        .collect()
}

/// An `OSError` as Python words it: `[Errno 13] Permission denied: '/x'`.
pub fn os_error(e: &std::io::Error, path: &Path) -> String {
    let text = e.to_string();
    let text = text.split(" (os error").next().unwrap_or(&text);
    match e.raw_os_error() {
        Some(n) => format!("[Errno {n}] {text}: {}", awm_pycompat::repr::str_repr(&path.to_string_lossy())),
        None => text.to_string(),
    }
}

fn mkdirs(dir: &Path) -> std::io::Result<()> {
    std::fs::DirBuilder::new().recursive(true).mode(DIR_MODE).create(dir)
}

fn tmp_of(dest: &Path) -> PathBuf {
    let mut s = dest.as_os_str().to_owned();
    s.push(".tmp");
    PathBuf::from(s)
}

fn times_of(meta: &std::fs::Metadata) -> FileTimes {
    let mut t = FileTimes::new();
    if let Ok(a) = meta.accessed() {
        t = t.set_accessed(a);
    }
    if let Ok(m) = meta.modified() {
        t = t.set_modified(m);
    }
    t
}

enum PackError {
    /// The source went between the listing and the copy: a race, not a fault.
    Vanished,
    Io(String),
    Check(String),
}

/// Whether two readers yield the same bytes.
fn same(mut a: impl Read, mut b: impl Read) -> std::io::Result<bool> {
    let (mut x, mut y) = (vec![0u8; 1 << 16], vec![0u8; 1 << 16]);
    loop {
        let n = read_full(&mut a, &mut x)?;
        let m = read_full(&mut b, &mut y)?;
        if n != m || x[..n] != y[..m] {
            return Ok(false);
        }
        if n == 0 {
            return Ok(true);
        }
    }
}

fn read_full(r: &mut impl Read, buf: &mut [u8]) -> std::io::Result<usize> {
    let mut n = 0;
    while n < buf.len() {
        match r.read(&mut buf[n..]) {
            Ok(0) => break,
            Ok(k) => n += k,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    }
    Ok(n)
}

/// Gzip `src` into `dest`'s temp file with `src`'s times, and check it decompresses to `src`.
/// The caller renames it into place. Returns the temp path and its size.
fn pack(src: &Path, dest: &Path) -> Result<(PathBuf, u64), PackError> {
    let io = |e: std::io::Error, p: &Path| if e.kind() == std::io::ErrorKind::NotFound { PackError::Vanished } else { PackError::Io(os_error(&e, p)) };
    let input = File::open(src).map_err(|e| io(e, src))?;
    let meta = input.metadata().map_err(|e| io(e, src))?;
    if let Some(dir) = dest.parent() {
        mkdirs(dir).map_err(|e| PackError::Io(os_error(&e, dir)))?;
    }
    let tmp = tmp_of(dest);
    let fail = |e: std::io::Error| {
        let _ = std::fs::remove_file(&tmp);
        PackError::Io(os_error(&e, &tmp))
    };
    let out = OpenOptions::new().write(true).create(true).truncate(true).mode(FILE_MODE).open(&tmp).map_err(|e| PackError::Io(os_error(&e, &tmp)))?;
    let mut enc = GzEncoder::new(out, Compression::best());
    std::io::copy(&mut BufReader::with_capacity(1 << 20, input), &mut enc).map_err(fail)?;
    let out = enc.finish().map_err(fail)?;
    out.set_times(times_of(&meta)).map_err(fail)?;
    let size = out.metadata().map_err(fail)?.len();
    drop(out);
    let checked = File::open(&tmp).and_then(|z| File::open(src).and_then(|s| same(MultiGzDecoder::new(BufReader::new(z)), BufReader::new(s))));
    match checked {
        Ok(true) => Ok((tmp, size)),
        Ok(false) => {
            let _ = std::fs::remove_file(&tmp);
            Err(PackError::Check(format!("{} changed or did not round-trip through gzip; kept it", src.display())))
        }
        Err(e) => {
            let _ = std::fs::remove_file(&tmp);
            Err(if e.kind() == std::io::ErrorKind::NotFound { PackError::Vanished } else { PackError::Check(format!("{}: the gzip check failed: {e}", src.display())) })
        }
    }
}

/// What one session's archiving did.
struct Moved {
    bytes_before: u64,
    bytes_after: u64,
    files: u64,
    vanished: u64,
    orphan: bool,
}

/// Archive one session's transcript and sidecar together, all or nothing: every file is packed
/// and checked first, and only then do the copies go into place and the sources go.
fn archive_one(tree: &Tree, s: &Session, dry_run: bool) -> Result<Moved, String> {
    let mut moved = Moved { bytes_before: s.bytes(), bytes_after: 0, files: 0, vanished: 0, orphan: s.transcript.is_none() };
    if dry_run {
        return Ok(moved);
    }
    let base = tree.archive.join(&s.project);
    let mut plan: Vec<(PathBuf, PathBuf)> = vec![];
    if let Some(t) = &s.transcript {
        plan.push((t.clone(), base.join(format!("{}.jsonl.gz", s.session_id))));
    }
    if let Some(d) = &s.sidecar {
        for f in walk_files(d) {
            let rel = f.strip_prefix(d).unwrap_or(&f);
            let mut dest = base.join(&s.session_id).join(rel).into_os_string();
            dest.push(".gz");
            plan.push((f, dest.into()));
        }
    }
    let mut staged: Vec<(PathBuf, PathBuf, PathBuf, u64)> = vec![];
    let unstage = |staged: &[(PathBuf, PathBuf, PathBuf, u64)]| {
        for (_, tmp, _, _) in staged {
            let _ = std::fs::remove_file(tmp);
        }
    };
    for (src, dest) in plan {
        match pack(&src, &dest) {
            Ok((tmp, size)) => staged.push((src, tmp, dest, size)),
            Err(PackError::Vanished) => moved.vanished += 1,
            Err(PackError::Io(e) | PackError::Check(e)) => {
                unstage(&staged);
                return Err(e);
            }
        }
    }
    for (i, (_, tmp, dest, _)) in staged.iter().enumerate() {
        if let Err(e) = std::fs::rename(tmp, dest) {
            unstage(&staged[i..]);
            return Err(os_error(&e, dest));
        }
    }
    for (src, _, _, size) in &staged {
        moved.bytes_after += size;
        moved.files += 1;
        let _ = std::fs::remove_file(src);
    }
    if let Some(d) = &s.sidecar {
        remove_empty_dirs(d);
    }
    Ok(moved)
}

/// Remove `dir` and the directories under it that are empty, deepest first. A file written
/// into the sidecar after the listing keeps its directory.
fn remove_empty_dirs(dir: &Path) {
    let mut dirs = vec![];
    let mut stack = vec![dir.to_path_buf()];
    while let Some(d) = stack.pop() {
        for e in std::fs::read_dir(&d).into_iter().flatten().flatten() {
            if e.file_type().is_ok_and(|t| t.is_dir()) {
                stack.push(e.path());
            }
        }
        dirs.push(d);
    }
    for d in dirs.iter().rev() {
        let _ = std::fs::remove_dir(d);
    }
}

/// Archive every live session older than `days` that no Claude Code process holds. A failure
/// on one session is logged and the sweep goes on.
pub fn archive(tree: &Tree, days: f64, dry_run: bool, live: &HashSet<String>, hide: Option<&dyn Fn(&str) -> bool>) -> Value {
    archive_sessions(tree, days, dry_run, live, hide).0
}

/// [`archive`], with the `(project, session)` of each transcript it archived and checked.
pub fn archive_sessions(tree: &Tree, days: f64, dry_run: bool, live: &HashSet<String>, hide: Option<&dyn Fn(&str) -> bool>) -> (Value, Vec<(String, String)>) {
    let started = Instant::now();
    let mut done: Vec<Moved> = vec![];
    let mut moved: Vec<(String, String)> = vec![];
    let mut failed: Vec<Value> = vec![];
    for s in aged(&tree.projects, Side::Live, days, hide).into_iter().filter(|s| !live.contains(&s.session_id)) {
        match archive_one(tree, &s, dry_run) {
            Ok(m) => {
                if !dry_run && !m.orphan {
                    moved.push((s.project.clone(), s.session_id.clone()));
                }
                done.push(m)
            }
            Err(error) => {
                tracing::warn!("transcripts: could not archive {}/{}: {error}", s.project, s.session_id);
                failed.push(json!({"project": s.project, "session": s.session_id, "error": error}));
            }
        }
    }
    let out = json!({
        "dry_run": dry_run,
        "retention_days": days,
        "sessions": done.len(),
        "orphans": done.iter().filter(|d| d.orphan).count(),
        "files": done.iter().map(|d| d.files).sum::<u64>(),
        "vanished": done.iter().map(|d| d.vanished).sum::<u64>(),
        "bytes_before": done.iter().map(|d| d.bytes_before).sum::<u64>(),
        "bytes_after": done.iter().map(|d| d.bytes_after).sum::<u64>(),
        "failed": failed,
        "seconds": round2(started.elapsed().as_secs_f64()),
    });
    (out, moved)
}

/// Delete archived sessions older than `days`: the one irreversible operation here.
pub fn prune(tree: &Tree, days: f64, dry_run: bool, hide: Option<&dyn Fn(&str) -> bool>) -> Value {
    prune_sessions(tree, days, dry_run, hide).0
}

/// [`prune`], with the `(project, session)` of each session it deleted.
pub fn prune_sessions(tree: &Tree, days: f64, dry_run: bool, hide: Option<&dyn Fn(&str) -> bool>) -> (Value, Vec<(String, String)>) {
    let started = Instant::now();
    let victims = aged(&tree.archive, Side::Archive, days, hide);
    let freed: u64 = victims.iter().map(Session::bytes).sum();
    if !dry_run {
        for s in &victims {
            if let Some(t) = &s.transcript {
                let _ = std::fs::remove_file(t);
            }
            if let Some(d) = &s.sidecar {
                let _ = std::fs::remove_dir_all(d);
            }
        }
    }
    let out = json!({
        "dry_run": dry_run,
        "retention_days": days,
        "sessions": victims.len(),
        "bytes_freed": freed,
        "seconds": round2(started.elapsed().as_secs_f64()),
    });
    let gone = if dry_run { vec![] } else { victims.into_iter().map(|s| (s.project, s.session_id)).collect() };
    (out, gone)
}

/// A name that stays inside its directory.
pub fn plain_name(s: &str) -> bool {
    !s.is_empty() && s != "." && s != ".." && !s.contains('/') && !s.contains('\0')
}

pub fn not_archived(project: &str, session: &str) -> Fail {
    Fail::new("FileNotFoundError", format!("{project}/{session} is not in the archive"))
}

fn unpack(src: &Path, dst: &Path) -> std::io::Result<()> {
    if let Some(dir) = dst.parent() {
        mkdirs(dir)?;
    }
    let tmp = tmp_of(dst);
    let written = (|| {
        let mut out = OpenOptions::new().write(true).create(true).truncate(true).mode(FILE_MODE).open(&tmp)?;
        let input = File::open(src)?;
        let meta = input.metadata()?;
        std::io::copy(&mut MultiGzDecoder::new(BufReader::with_capacity(1 << 20, input)), &mut out)?;
        out.flush()?;
        out.set_times(times_of(&meta))?;
        std::fs::rename(&tmp, dst)
    })();
    if written.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    written
}

/// Put one archived session back into the live tree. Refuses rather than overwrites a live copy:
/// a restore that clobbered a live conversation would destroy what the archive preserves.
pub fn restore(tree: &Tree, project: &str, session: &str) -> Result<Value, Fail> {
    if !plain_name(project) || !plain_name(session) {
        return Err(not_archived(project, session));
    }
    let src_t = tree.archive.join(project).join(format!("{session}.jsonl.gz"));
    let src_d = tree.archive.join(project).join(session);
    if !src_t.exists() && !src_d.is_dir() {
        return Err(not_archived(project, session));
    }
    let dst_t = tree.projects.join(project).join(format!("{session}.jsonl"));
    let dst_d = tree.projects.join(project).join(session);
    if dst_t.exists() || dst_d.exists() {
        return Err(Fail::new("FileExistsError", format!("{project}/{session} already exists in the live tree; refusing to overwrite it")));
    }
    let oserr = |e: std::io::Error, p: &Path| Fail::new("OSError", os_error(&e, p));
    let mut files = 0;
    if src_t.exists() {
        unpack(&src_t, &dst_t).map_err(|e| oserr(e, &src_t))?;
        files += 1;
    }
    if src_d.is_dir() {
        for src in walk_files(&src_d) {
            let rel = src.strip_prefix(&src_d).unwrap_or(&src);
            let Some(plain) = rel.to_str().and_then(|r| r.strip_suffix(".gz")) else { continue };
            unpack(&src, &dst_d.join(plain)).map_err(|e| oserr(e, &src))?;
            files += 1;
        }
    }
    Ok(json!({"project": project, "session": session, "files": files, "path": dst_t.to_string_lossy()}))
}
