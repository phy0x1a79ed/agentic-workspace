//! Embed JSON-string lines from stdin with the model in `<dir>`, one JSON vector per line.
//! Timings go to stderr. The T6 parity check runs this against stored Python vectors.

use awm_embed::{Embedder, MiniLm};
use std::io::{BufRead, Write};
use std::time::Instant;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let dir = std::env::args().nth(1).ok_or("usage: embed <model-dir> < texts")?;
    let t0 = Instant::now();
    let model = MiniLm::load_dir(dir.as_ref())?;
    let load_ms = t0.elapsed().as_secs_f64() * 1e3;
    let texts: Vec<String> = std::io::stdin().lock().lines().map(|l| Ok(serde_json::from_str(&l?)?)).collect::<Result<_, Box<dyn std::error::Error>>>()?;
    let refs: Vec<&str> = texts.iter().map(String::as_str).collect();
    let t1 = Instant::now();
    let vecs = model.encode(&refs)?;
    let secs = t1.elapsed().as_secs_f64();
    let mut out = std::io::BufWriter::new(std::io::stdout().lock());
    for v in vecs {
        serde_json::to_writer(&mut out, &v)?;
        out.write_all(b"\n")?;
    }
    eprintln!("load_ms={load_ms:.0} texts={} secs={secs:.2} texts_per_s={:.1}", texts.len(), texts.len() as f64 / secs);
    Ok(())
}
