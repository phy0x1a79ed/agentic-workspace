//! `.dvc` pins and `dvc.lock` stages: the outputs a commit names by hash.

use std::path::{Path, PathBuf};

use serde_yaml::{Mapping, Value};

pub const PIN_SUFFIX: &str = ".dvc";
pub const LOCK_NAME: &str = "dvc.lock";

#[derive(Debug, thiserror::Error)]
pub enum PinError {
    #[error("cannot read pin {path}: {source}")]
    Io { path: PathBuf, source: std::io::Error },
    #[error("cannot parse pin {path}: {detail}")]
    Parse { path: PathBuf, detail: String },
    #[error("{path}: {out} is a legacy pin with no `hash: md5`; refresh it with `dvc add` before awm serves it")]
    Legacy { path: PathBuf, out: String },
}

/// One hashed output of a pin.
#[derive(Debug, Clone, PartialEq)]
pub struct Out {
    /// As written in the pin, relative to the pin's directory.
    pub path: String,
    pub md5: Option<String>,
    pub hash: Option<String>,
    pub size: Option<u64>,
    pub nfiles: Option<u64>,
    /// The output's YAML mapping, every key included.
    pub raw: Mapping,
}

impl Out {
    fn from_yaml(v: &Value) -> Option<Out> {
        let m = v.as_mapping()?;
        let s = |k: &str| m.get(k).and_then(Value::as_str).map(str::to_string);
        let u = |k: &str| m.get(k).and_then(Value::as_u64);
        Some(Out { path: s("path")?, md5: s("md5").map(|h| h.to_lowercase()), hash: s("hash"), size: u("size"), nfiles: u("nfiles"), raw: m.clone() })
    }

    pub fn is_dir(&self) -> bool {
        self.md5.as_deref().is_some_and(|h| h.ends_with(crate::odb::DIR_SUFFIX))
    }

    /// Refuse a pin that DVC 2 wrote: its file ids are md5 over dos2unix text, not bytes.
    pub fn require_md5(&self, pin: &Path) -> Result<(), PinError> {
        if self.md5.is_some() && self.hash.as_deref() != Some("md5") {
            return Err(PinError::Legacy { path: pin.to_path_buf(), out: self.path.clone() });
        }
        Ok(())
    }
}

/// A parsed `.dvc` file.
#[derive(Debug, Clone)]
pub struct Pin {
    pub path: PathBuf,
    pub doc: Mapping,
    pub outs: Vec<Out>,
}

impl Pin {
    pub fn parse(text: &str, path: &Path) -> Result<Pin, PinError> {
        let doc: Value = serde_yaml::from_str(text).map_err(|e| PinError::Parse { path: path.into(), detail: e.to_string() })?;
        let doc = match doc {
            Value::Null => Mapping::new(),
            Value::Mapping(m) => m,
            _ => return Err(PinError::Parse { path: path.into(), detail: "not a mapping".into() }),
        };
        let mut outs: Vec<Out> = doc.get("outs").and_then(Value::as_sequence).map(|s| s.iter().filter_map(Out::from_yaml).collect()).unwrap_or_default();
        fix_numeric_md5(&mut outs, text, path)?;
        Ok(Pin { path: path.into(), doc, outs })
    }

    pub fn load(path: &Path) -> Result<Pin, PinError> {
        let text = std::fs::read_to_string(path).map_err(|e| PinError::Io { path: path.into(), source: e })?;
        Pin::parse(&text, path)
    }

    /// The directory output paths resolve against: the pin's directory, then its `wdir`.
    pub fn base(&self) -> PathBuf {
        let dir = self.path.parent().map(Path::to_path_buf).unwrap_or_default();
        match self.doc.get("wdir").and_then(Value::as_str) {
            Some(w) => dir.join(w),
            None => dir,
        }
    }

    pub fn out_path(&self, out: &Out) -> PathBuf {
        normalize(&self.base().join(&out.path))
    }

    /// Whether `dvc add` wrote this pin: no command, no deps, at most one output.
    pub fn is_data_source(&self) -> bool {
        let deps = self.doc.get("deps").and_then(Value::as_sequence).is_some_and(|d| !d.is_empty());
        !self.doc.contains_key("cmd") && !deps && self.outs.len() <= 1
    }
}

/// YAML reads an all-digit hash as a number and drops its leading zeros. Take such a hash
/// from the text instead, and only when the text holds exactly one, so it is never misread.
fn fix_numeric_md5(outs: &mut [Out], text: &str, path: &Path) -> Result<(), PinError> {
    let numeric = |o: &Out| o.md5.is_none() && o.raw.get("md5").is_some_and(|v| !v.is_null());
    if !outs.iter().any(numeric) {
        return Ok(());
    }
    let tokens = raw_md5_tokens(text);
    if outs.len() != 1 || tokens.len() != 1 {
        return Err(PinError::Parse { path: path.into(), detail: "an md5 is not a string and the text holds more than one".into() });
    }
    outs[0].md5 = Some(tokens[0].to_lowercase());
    Ok(())
}

/// Every `md5:` value in a pin's text, as written.
pub fn raw_md5_tokens(text: &str) -> Vec<String> {
    text.lines()
        .filter_map(|l| {
            let t = l.trim_start().trim_start_matches("- ").trim_start();
            t.strip_prefix("md5:").map(|v| v.trim().trim_matches(['"', '\'']).to_string())
        })
        .filter(|v| !v.is_empty())
        .collect()
}

/// The single hashed output's md5 without its `.dir` suffix, as the hooks name a shared copy.
pub fn pin_md5(pin: &Path) -> Option<String> {
    let p = Pin::load(pin).ok()?;
    if p.outs.len() != 1 {
        return None;
    }
    let md5 = p.outs[0].md5.clone()?;
    let md5 = md5.strip_suffix(crate::odb::DIR_SUFFIX).map(str::to_string).unwrap_or(md5);
    (!md5.is_empty()).then_some(md5)
}

/// The stages of a `dvc.lock`, as (stage name, outputs). Schema 2 nests them under `stages`.
pub fn lock_stages(text: &str, path: &Path) -> Result<Vec<(String, Vec<Out>)>, PinError> {
    let doc: Value = serde_yaml::from_str(text).map_err(|e| PinError::Parse { path: path.into(), detail: e.to_string() })?;
    let Some(doc) = doc.as_mapping() else { return Ok(vec![]) };
    let stages = if doc.contains_key("schema") { doc.get("stages").and_then(Value::as_mapping).cloned().unwrap_or_default() } else { doc.clone() };
    Ok(stages
        .iter()
        .filter_map(|(k, v)| {
            let name = k.as_str()?.to_string();
            let outs = v.as_mapping()?.get("outs").and_then(Value::as_sequence).map(|s| s.iter().filter_map(Out::from_yaml).collect()).unwrap_or_default();
            Some((name, outs))
        })
        .collect())
}

/// Collapse `.` and `..` without touching the filesystem.
pub fn normalize(p: &Path) -> PathBuf {
    use std::path::Component;
    let mut out = PathBuf::new();
    for c in p.components() {
        match c {
            Component::CurDir => {}
            Component::ParentDir => {
                if !out.pop() {
                    out.push("..");
                }
            }
            c => out.push(c.as_os_str()),
        }
    }
    out
}
