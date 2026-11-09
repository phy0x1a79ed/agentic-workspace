//! Built files a release pins, and their push to the nodes that cannot fetch.
//!
//! A pin is a `key=value` file in the project's tree that names one file in the landing node's
//! artifact store (`<AWM_DIR>/artifacts`) and its sha256 (install.md R115 item 4).
//! `deploy.toml`'s `[artifacts] pins` lists the pins. The landing node pushes each pinned file
//! to every push node that names an `artifacts` directory, before that node refreshes. The
//! node never fetches it (Tony, 2026-11-05: "mizar pushes rather than deneb updates"). The
//! install on the node checks the sha256 again.

use std::collections::HashMap;
use std::io::Read;
use std::path::Path;
use std::time::Duration;

use sha2::{Digest, Sha256};

use super::deployconf;
use super::sh::{self, Ran};
use crate::timers::quote;

#[derive(Debug, Clone, PartialEq)]
pub struct Artifact {
    pub pin: String,
    pub file: String,
    pub sha256: String,
}

pub fn parse_pin(text: &str, pin: &str) -> Result<Artifact, String> {
    let mut fields = HashMap::new();
    for line in text.lines() {
        if line.trim_start().starts_with('#') {
            continue;
        }
        if let Some((k, v)) = line.split_once('=') {
            fields.insert(k, v);
        }
    }
    let field = |k: &str| fields.get(k).map(|v| v.trim().to_string()).unwrap_or_default();
    let (name, sha) = (field("file"), field("sha256"));
    if name.is_empty() || sha.is_empty() {
        return Err(format!("{pin} needs file and sha256"));
    }
    if name.contains('/') || name.starts_with('.') {
        return Err(format!("{pin}: file must be a bare file name, not '{name}'"));
    }
    Ok(Artifact { pin: pin.into(), file: name, sha256: sha })
}

pub fn show(bare: &Path, spec: &str) -> Option<String> {
    let r = sh::git(bare, &["show", spec], &HashMap::new(), None);
    // Python's text mode strips nothing from `git show`, and proc.git strips the ends.
    (r.code == 0).then(|| r.stdout.trim().to_string())
}

/// The artifacts `rev` pins, read from the commit rather than a checkout.
pub fn pinned(bare: &Path, rev: &str) -> Result<Vec<Artifact>, String> {
    let Some(conf) = show(bare, &format!("{rev}:{}", deployconf::FILE)) else { return Ok(vec![]) };
    let cfg = deployconf::parse(&conf).map_err(|e| e.0)?;
    let mut out = vec![];
    for pin in &cfg.artifact_pins {
        let short: String = rev.chars().take(12).collect();
        let text = show(bare, &format!("{rev}:{pin}")).ok_or_else(|| format!("{} lists {pin}, which {short} does not hold", deployconf::FILE))?;
        out.push(parse_pin(&text, pin)?);
    }
    Ok(out)
}

pub fn sha256_file(path: &Path) -> std::io::Result<String> {
    let mut f = std::fs::File::open(path)?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(h.finalize().iter().map(|b| format!("{b:02x}")).collect())
}

/// Refuse a pinned file that is missing here or whose bytes differ from the pin.
pub fn check(store: &Path, art: &Artifact) -> Result<(), String> {
    let path = store.join(&art.file);
    if !path.is_file() {
        return Err(format!("{} pins {}, which {} does not hold", art.pin, art.file, store.display()));
    }
    let got = sha256_file(&path).map_err(|e| format!("{}: {e}", path.display()))?;
    if got != art.sha256 {
        return Err(format!("{} has sha256 {got}, and {} names {}", path.display(), art.pin, art.sha256));
    }
    Ok(())
}

/// Copy one pinned file into `dest` (`<host>:<dir>`). rsync writes a temp file and renames
/// it, so the node never sees half a file.
pub fn push(store: &Path, art: &Artifact, dest: &str) -> Ran {
    let src = quote(&store.join(&art.file).to_string_lossy());
    let to = quote(&format!("{}/{}", dest.trim_end_matches('/'), art.file));
    sh::bash(&format!("rsync -t --mkpath {src} {to}"), store, Duration::from_secs(900), &HashMap::new(), Duration::from_secs(5))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_pin_names_a_bare_file_and_its_hash() {
        assert_eq!(parse_pin("version=v1\nsha256=ab\n", "svc/server.pin").unwrap_err(), "svc/server.pin needs file and sha256");
        assert!(parse_pin("file=../x.tar.xz\nsha256=ab\n", "svc/server.pin").unwrap_err().contains("bare file name"));
        let a = parse_pin("# c\nversion=v1\nfile=awm-x\nsha256= ab \n", "p").unwrap();
        assert_eq!((a.file.as_str(), a.sha256.as_str()), ("awm-x", "ab"));
    }

    #[test]
    fn check_and_push() {
        let tmp = tempfile::tempdir().unwrap();
        let (store, dest) = (tmp.path().join("store"), tmp.path().join("dest"));
        std::fs::create_dir_all(&store).unwrap();
        std::fs::write(store.join("f"), b"hello").unwrap();
        let art = Artifact { pin: "p.pin".into(), file: "f".into(), sha256: "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824".into() };
        check(&store, &art).unwrap();
        assert!(check(&store, &Artifact { sha256: "00".into(), ..art.clone() }).unwrap_err().contains("has sha256"));
        assert!(check(&store, &Artifact { file: "g".into(), ..art.clone() }).unwrap_err().contains("does not hold"));
        if Path::new("/usr/bin/rsync").exists() {
            let r = push(&store, &art, &dest.to_string_lossy());
            assert!(r.ok(), "{}", r.stdout);
            assert_eq!(std::fs::read(dest.join("f")).unwrap(), b"hello");
        }
    }
}
