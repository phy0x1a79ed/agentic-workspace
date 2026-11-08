//! What tests need from the `dvc` CLI and this crate does not serve: `dvc add` and
//! `dvc unprotect`, with DVC 3's layout. Not for production use: awm leaves `dvc add` to
//! the hand tool.

use std::fs;
use std::io;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

use crate::odb::{self, Entry, Manifest};
use crate::scope::cache_dir_for;
use crate::status::{walk_out, Repo};

/// Save `rel` (a file or directory under `repo`) into the repo's cache, relink it by
/// hardlink, write `<rel>.dvc`, and gitignore `rel` beside it. Returns the output's id.
pub fn add(repo: &Path, rel: &str) -> io::Result<String> {
    let cache = cache_dir_for(repo);
    let path = repo.join(rel);
    let name = path.file_name().expect("a named path").to_string_lossy().into_owned();
    let (oid, size, nfiles) = if path.is_dir() {
        let r = Repo::open(repo);
        let mut entries = vec![];
        let mut size = 0;
        for (relpath, abs) in walk_out(&r, &path)? {
            let md5 = save_file(&cache, &abs)?;
            size += fs::metadata(&abs)?.len();
            entries.push(Entry::new(relpath, md5));
        }
        let m = Manifest::from_entries(entries);
        let n = m.entries.len();
        (m.save(&cache)?, size, Some(n))
    } else {
        let size = fs::metadata(&path)?.len();
        (save_file(&cache, &path)?, size, None)
    };
    let mut pin = format!("outs:\n- md5: {oid}\n  size: {size}\n");
    if let Some(n) = nfiles {
        pin.push_str(&format!("  nfiles: {n}\n"));
    }
    pin.push_str(&format!("  hash: md5\n  path: {name}\n"));
    fs::write(path.with_file_name(format!("{name}.dvc")), pin)?;
    let gi = path.with_file_name(".gitignore");
    let prior = fs::read_to_string(&gi).unwrap_or_default();
    let line = format!("/{name}");
    if !prior.lines().any(|l| l == line) {
        fs::write(&gi, format!("{prior}{line}\n"))?;
    }
    Ok(oid)
}

fn save_file(cache: &Path, path: &Path) -> io::Result<String> {
    let md5 = odb::md5_file(path)?;
    let obj = odb::object_path(cache, &md5);
    if !obj.is_file() {
        odb::save_bytes(cache, &md5, &fs::read(path)?)?;
    }
    let tmp = path.with_file_name(format!(".{}.add", path.file_name().unwrap_or_default().to_string_lossy()));
    fs::hard_link(&obj, &tmp)?;
    fs::rename(&tmp, path)?;
    Ok(md5)
}

/// Replace every file under `path` with a private, writable copy, as `dvc unprotect`.
pub fn unprotect(path: &Path) -> io::Result<()> {
    let files: Vec<std::path::PathBuf> = if path.is_dir() {
        let mut v = vec![];
        let mut stack = vec![path.to_path_buf()];
        while let Some(d) = stack.pop() {
            for e in fs::read_dir(&d)? {
                let e = e?;
                if e.file_type()?.is_dir() {
                    stack.push(e.path());
                } else {
                    v.push(e.path());
                }
            }
        }
        v
    } else {
        vec![path.to_path_buf()]
    };
    for f in files {
        let bytes = fs::read(&f)?;
        let tmp = f.with_file_name(format!(".{}.unprotect", f.file_name().unwrap_or_default().to_string_lossy()));
        fs::write(&tmp, bytes)?;
        fs::set_permissions(&tmp, fs::Permissions::from_mode(0o644))?;
        fs::rename(&tmp, &f)?;
    }
    Ok(())
}
