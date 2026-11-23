//! The market's property vocabulary: the keys a typed material may use, each with a description
//! and an optional list of allowed values. Standalone of awm; it persists in a JSON file when asked.

use outsource_proto::PropSet;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::PathBuf;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
struct Property {
    description: String,
    /// `None` accepts any value.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    values: Option<Vec<String>>,
}

#[derive(Debug, Default)]
pub struct Vocab {
    keys: BTreeMap<String, Property>,
    file: Option<PathBuf>,
}

const SEED: [(&str, &str); 5] = [
    ("db", "name of a reference database or sequence collection, e.g. uniref50"),
    ("release", "version or date of a database release, e.g. 2024_01"),
    ("tool", "software the material belongs to or was made by"),
    ("version", "version of the tool or dataset"),
    ("format", "file format, e.g. fasta"),
];

/// Names an error offers.
const SUGGEST: usize = 3;

impl Vocab {
    pub fn seeded() -> Vocab {
        let keys = SEED.iter().map(|(k, d)| (k.to_string(), Property { description: d.to_string(), values: None })).collect();
        Vocab { keys, file: None }
    }

    /// Loads `file` over the seed (its keys win) and saves every later change there.
    pub fn keep(&mut self, file: PathBuf) -> std::io::Result<()> {
        match std::fs::read(&file) {
            Ok(bytes) => self.keys.extend(serde_json::from_slice::<BTreeMap<String, Property>>(&bytes)?),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e),
        }
        self.file = Some(file);
        Ok(())
    }

    pub fn list(&self) -> Value {
        json!({ "properties": self.keys })
    }

    /// Adds `name` or extends it: the description is replaced and `values` join the allowed list.
    pub fn add(&mut self, name: &str, description: &str, values: Option<&[String]>) -> Result<(), String> {
        let name = name.trim();
        if name.is_empty() || name.contains(char::is_whitespace) {
            return Err(format!("property name {name:?} must be one word"));
        }
        let prop = self.keys.entry(name.to_string()).or_insert_with(|| Property { description: String::new(), values: None });
        prop.description = description.to_string();
        if let Some(vs) = values {
            let list = prop.values.get_or_insert_with(Vec::new);
            for v in vs.iter().map(|v| v.trim().to_lowercase()).filter(|v| !v.is_empty()) {
                if !list.contains(&v) {
                    list.push(v);
                }
            }
            list.sort();
        }
        self.save()
    }

    fn save(&self) -> Result<(), String> {
        let Some(file) = &self.file else { return Ok(()) };
        let tmp = file.with_extension("tmp");
        let write = || -> std::io::Result<()> {
            if let Some(dir) = file.parent() {
                std::fs::create_dir_all(dir)?;
            }
            std::fs::write(&tmp, serde_json::to_vec_pretty(&self.keys)?)?;
            std::fs::rename(&tmp, file)
        };
        write().map_err(|e| format!("vocabulary saved in memory only: {}: {e}", file.display()))
    }

    /// Accepts a type whose keys are known and whose values are lowercase and in their key's list.
    pub fn check(&self, ty: &PropSet) -> Result<(), String> {
        for (k, v) in ty {
            let Some(prop) = self.keys.get(k) else {
                return Err(format!("unknown property {k:?}; closest known: {}; see `properties`, add with `property_add`", self.closest(k, self.keys.keys())));
            };
            if *v != v.to_lowercase() {
                return Err(format!("property {k}={v:?} must be lowercase"));
            }
            if let Some(list) = prop.values.as_ref().filter(|l| !l.contains(v)) {
                return Err(format!("{v:?} is not a value of {k}; closest allowed: {}; extend with `property_add`", self.closest(v, list.iter())));
            }
        }
        Ok(())
    }

    fn closest<'a>(&self, word: &str, names: impl Iterator<Item = &'a String>) -> String {
        let mut by: Vec<(usize, &String)> = names.map(|n| (edit_distance(word, n), n)).collect();
        by.sort();
        by.iter().take(SUGGEST).map(|(_, n)| format!("{n:?}")).collect::<Vec<_>>().join(", ")
    }
}

fn edit_distance(a: &str, b: &str) -> usize {
    let b: Vec<char> = b.chars().collect();
    let mut row: Vec<usize> = (0..=b.len()).collect();
    for (i, ca) in a.chars().enumerate() {
        let mut diag = std::mem::replace(&mut row[0], i + 1);
        for (j, cb) in b.iter().enumerate() {
            let next = (row[j] + 1).min(row[j + 1] + 1).min(diag + usize::from(ca != *cb));
            diag = std::mem::replace(&mut row[j + 1], next);
        }
    }
    row[b.len()]
}
