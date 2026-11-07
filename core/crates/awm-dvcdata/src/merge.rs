//! The git merge driver for `.dvc` pins: two branches that each changed one directory
//! output merge by combining their listings, not by conflicting on a hash line.
//!
//! The result matches `dvc git-hook merge-driver` with every change type allowed. A path
//! that one side changed and the other did not takes the change. A path both sides
//! changed the same way takes it once. Any other path both touched is a conflict, and so
//! is a path both deleted, as in DVC.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::Path;

use serde_yaml::Value;

use crate::odb::{Entry, Manifest};
use crate::pin::{Out, Pin};
use crate::pyjson::py_tuple_repr;
use crate::scope::cache_dir_for;

#[derive(Debug, thiserror::Error)]
pub enum MergeError {
    #[error("{0}")]
    Refused(String),
    #[error(transparent)]
    Pin(#[from] crate::pin::PinError),
    #[error(transparent)]
    Manifest(#[from] crate::odb::ManifestError),
    #[error("{0}")]
    Io(#[from] std::io::Error),
}

fn refused(msg: impl Into<String>) -> MergeError {
    MergeError::Refused(msg.into())
}

/// The keys that may differ between two sides of one output.
const MERGED_KEYS: [&str; 4] = ["md5", "size", "nfiles", "hash"];

fn options(o: &Out) -> serde_yaml::Mapping {
    let mut m = o.raw.clone();
    for k in MERGED_KEYS {
        m.remove(k);
    }
    m
}

fn check_stage(p: &Pin) -> Result<(), MergeError> {
    if !p.is_data_source() {
        return Err(refused("unable to auto-merge DVC files that weren't created by `dvc add`"));
    }
    Ok(())
}

fn check_out(ours: &Out, other: &Out, pin: &Path) -> Result<(), MergeError> {
    other.require_md5(pin)?;
    if options(ours) != options(other) {
        return Err(refused("unable to auto-merge outputs with different options"));
    }
    if !other.is_dir() {
        return Err(refused("unable to auto-merge outputs that are not directories"));
    }
    Ok(())
}

type Value3 = (String, BTreeMap<String, serde_json::Value>);

/// Three-way merge of manifests, keyed by relpath.
pub fn merge_trees(ancestor: &Manifest, ours: &Manifest, theirs: &Manifest) -> Result<Manifest, MergeError> {
    let map = |m: &Manifest| -> BTreeMap<String, Value3> { m.entries.iter().map(|e| (e.relpath.clone(), e.value())).collect() };
    let (a, o, t) = (map(ancestor), map(ours), map(theirs));
    if o == a {
        return Ok(theirs.clone());
    }
    if t == a {
        return Ok(ours.clone());
    }
    let keys: BTreeSet<&String> = a.keys().chain(o.keys()).chain(t.keys()).collect();
    let mut merged = vec![];
    let mut conflicts = vec![];
    for k in keys {
        let (va, vo, vt) = (a.get(k), o.get(k), t.get(k));
        if va.is_some() && vo.is_none() && vt.is_none() {
            let parts: Vec<&str> = k.split('/').collect();
            return Err(refused(format!("unable to auto-merge the following paths:\nboth deleted: {}", py_tuple_repr(&parts))));
        }
        let pick = if vo == va {
            vt
        } else if vt == va || vo == vt {
            vo
        } else {
            conflicts.push(k.clone());
            continue;
        };
        if let Some((md5, rest)) = pick {
            merged.push(Entry { relpath: k.clone(), md5: md5.clone(), rest: rest.clone() });
        }
    }
    if !conflicts.is_empty() {
        return Err(refused(format!("unable to auto-merge the following paths:\n{}", conflicts.join("\n"))));
    }
    Ok(Manifest::from_entries(merged))
}

/// Run the driver as git calls it: merge `their` into `our` in place, with `ancestor` the
/// common base. `repo` is the worktree, whose cache holds the manifests.
pub fn merge_driver(repo: &Path, ancestor: &Path, our: &Path, their: &Path) -> Result<(), MergeError> {
    let cache = cache_dir_for(repo);
    let load = |p: &Path| -> Result<Pin, MergeError> {
        let text = fs::read_to_string(p)?;
        Ok(Pin::parse(&text, p)?)
    };
    let (anc, ours, theirs) = (load(ancestor)?, load(our)?, load(their)?);
    if theirs.outs.is_empty() {
        return Ok(());
    }
    if ours.outs.is_empty() {
        fs::copy(their, our)?;
        return Ok(());
    }
    check_stage(&anc)?;
    check_stage(&ours)?;
    check_stage(&theirs)?;
    let our_out = &ours.outs[0];
    our_out.require_md5(our)?;
    let anc_out = anc.outs.first();
    if let Some(a) = anc_out {
        check_out(our_out, a, ancestor)?;
    }
    check_out(our_out, our_out, our)?;
    check_out(our_out, &theirs.outs[0], their)?;

    let tree = |o: &Out| -> Result<Manifest, MergeError> { Ok(Manifest::load(&cache, o.md5.as_deref().unwrap_or_default())?) };
    let base = match anc_out {
        Some(a) => tree(a)?,
        None => Manifest::default(),
    };
    let merged = merge_trees(&base, &tree(our_out)?, &tree(&theirs.outs[0])?)?;
    let oid = merged.save(&cache)?;
    let text = fs::read_to_string(our)?;
    fs::write(our, rewrite_first_out(&text, &oid, merged.du(&cache), merged.entries.len() as u64))?;
    Ok(())
}

/// Set the first output's `md5`, `size` and `nfiles` in a pin's text and keep every other
/// line, as DVC's round-trip dump does. An absent `size` drops its line. A key the
/// output lacks goes at the end of its mapping.
pub fn rewrite_first_out(text: &str, md5: &str, size: Option<u64>, nfiles: u64) -> String {
    let lines: Vec<&str> = text.split_inclusive('\n').collect();
    let Some(outs_at) = lines.iter().position(|l| l.trim_end() == "outs:") else { return text.into() };
    let Some(item_at) = (outs_at + 1..lines.len()).find(|&i| lines[i].trim_start().starts_with("- ")) else { return text.into() };
    let dash_indent = lines[item_at].len() - lines[item_at].trim_start().len();
    let key_indent = dash_indent + 2;
    let mut end = item_at + 1;
    while end < lines.len() {
        let l = lines[end];
        let ind = l.len() - l.trim_start().len();
        if l.trim().is_empty() || ind < key_indent || l.trim_start().starts_with("- ") && ind == dash_indent {
            break;
        }
        end += 1;
    }
    let want: [(&str, Option<String>); 3] = [("md5", Some(md5.into())), ("size", size.map(|s| s.to_string())), ("nfiles", Some(nfiles.to_string()))];
    let mut seen = [false; 3];
    let mut out = String::new();
    for l in &lines[..item_at] {
        out.push_str(l);
    }
    for (i, l) in lines[item_at..end].iter().enumerate() {
        let body = if i == 0 { &l[dash_indent + 2..] } else { l.trim_start() };
        let prefix = &l[..l.len() - body.len()];
        let key = body.split(':').next().unwrap_or_default();
        let nl = if l.ends_with('\n') { "\n" } else { "" };
        let mut line = l.to_string();
        if let Some(k) = want.iter().position(|(name, _)| *name == key) {
            seen[k] = true;
            match &want[k].1 {
                Some(v) => line = format!("{prefix}{key}: {v}{nl}"),
                // The dash line keeps a stale value rather than lose its dash.
                None if i == 0 => {}
                None => line = String::new(),
            }
        }
        out.push_str(&line);
    }
    let pad = " ".repeat(key_indent);
    for (k, (name, v)) in want.iter().enumerate() {
        if let (false, Some(v)) = (seen[k], v) {
            if !out.ends_with('\n') {
                out.push('\n');
            }
            out.push_str(&format!("{pad}{name}: {v}\n"));
        }
    }
    for l in &lines[end..] {
        out.push_str(l);
    }
    out
}

/// The value at `key` of the first output, for tests and reports.
pub fn first_out_value(text: &str, key: &str) -> Option<Value> {
    let v: Value = serde_yaml::from_str(text).ok()?;
    v.get("outs")?.as_sequence()?.first()?.get(key).cloned()
}
