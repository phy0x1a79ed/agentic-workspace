//! awm-harness holds only what Claude Code and harness@1 own: no caller's names, state or
//! environment.

use std::path::Path;

fn walk(dir: &Path, out: &mut Vec<std::path::PathBuf>) {
    for entry in std::fs::read_dir(dir).unwrap().flatten() {
        let p = entry.path();
        if p.is_dir() {
            walk(&p, out);
        } else if p.extension().is_some_and(|e| e == "rs") {
            out.push(p);
        }
    }
}

#[test]
fn source_names_no_caller() {
    let mut files = Vec::new();
    walk(&Path::new(env!("CARGO_MANIFEST_DIR")).join("src"), &mut files);
    assert!(!files.is_empty());
    let banned = ["cch", "AWM_CX", "/cx/", "AWM_"];
    let mut hits = Vec::new();
    for f in &files {
        let text = std::fs::read_to_string(f).unwrap();
        for (n, line) in text.lines().enumerate() {
            for b in banned {
                if line.contains(b) {
                    hits.push(format!("{}:{}: {b}", f.display(), n + 1));
                }
            }
        }
    }
    assert!(hits.is_empty(), "caller names in awm-harness:\n{}", hits.join("\n"));
    let manifest = std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("Cargo.toml")).unwrap();
    let plugin_dep = manifest.lines().any(|l| l.contains("awm-plugin-") && !l.contains("awm-plugin-sdk"));
    assert!(!plugin_dep, "awm-harness depends on a plugin crate");
}
