//! Check `Store::init` against the Python services' real schemas.
//!
//! Usage: initcheck <specs.json> [<dir of db copies>]
//!
//! specs.json lists each service's `schema`, `version`, `migrations` and the `sqlite_master` a
//! fresh Python init made (`python_fresh_master`). For each one, a fresh Rust init must make the
//! same `sqlite_master`, and `<dir>/<name>.db`, when present, must come through init unchanged.
//! Run it on copies only: init writes.

use awm_store::{InitOutcome, Migration, Store, StoreError};
use serde_json::Value;

fn master(store: &Store) -> Vec<Value> {
    store
        .blocking_read(|c| {
            let mut stmt = c.prepare("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")?;
            let rows = stmt
                .query_map([], |r| {
                    Ok(serde_json::json!([r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?, r.get::<_, Option<String>>(3)?]))
                })?
                .collect::<Result<Vec<_>, _>>()?;
            Ok::<_, StoreError>(rows)
        })
        .expect("read sqlite_master")
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let specs: Vec<Value> = serde_json::from_str(&std::fs::read_to_string(&args[1]).expect("read specs")).expect("parse specs");
    let copies = args.get(2).map(std::path::PathBuf::from);
    let mut failed = 0;
    for spec in &specs {
        let name = spec["name"].as_str().unwrap();
        if let Some(e) = spec.get("error") {
            println!("{name}: skipped, the Python failed to load: {e}");
            continue;
        }
        let schema = spec["schema"].as_str().unwrap();
        let version = spec["version"].as_i64().unwrap();
        let steps: Vec<Migration> = spec["migrations"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| Migration { from: m["from"].as_i64().unwrap(), sql: Box::leak(m["sql"].as_str().unwrap().to_string().into_boxed_str()) })
            .collect();

        let dir = tempfile::tempdir().unwrap();
        let fresh = Store::open(dir.path().join("fresh.db")).unwrap();
        let outcome = fresh.init(schema, version, &steps);
        let same = outcome.is_ok() && Value::Array(master(&fresh)) == spec["python_fresh_master"];
        println!("{name}: fresh init {outcome:?}, sqlite_master {}", if same { "matches the Python" } else { "DIFFERS" });
        failed += (!same) as usize;

        let Some(copy) = copies.as_ref().map(|d| d.join(format!("{name}.db"))).filter(|p| p.exists()) else { continue };
        let live = Store::open(&copy).unwrap();
        let before = master(&live);
        let outcome = live.init(schema, version, &steps);
        let unchanged = before == master(&live);
        let ok = matches!(outcome, Ok(InitOutcome::Current { .. })) && unchanged;
        println!("{name}: live copy {outcome:?}, sqlite_master {}", if unchanged { "unchanged" } else { "CHANGED" });
        failed += (!ok) as usize;
    }
    if failed > 0 {
        eprintln!("{failed} checks failed");
        std::process::exit(1);
    }
}
