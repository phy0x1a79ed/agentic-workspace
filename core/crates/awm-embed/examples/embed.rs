//! Embed JSON-string lines from stdin with `<model>` (minilm | bge-large) in `<dir>`, one JSON
//! vector per line. Timings go to stderr: load, bulk passages per second, and query p50/p95.

use awm_embed::{Embedder, Onnx, BGE_LARGE, MINILM};
use std::io::{BufRead, Write};
use std::time::Instant;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args().skip(1);
    let (Some(name), Some(dir)) = (args.next(), args.next()) else {
        return Err("usage: embed <minilm|bge-large> <model-dir> < texts".into());
    };
    let model = match name.as_str() {
        "minilm" => &MINILM,
        "bge-large" => &BGE_LARGE,
        _ => return Err(format!("unknown model {name}").into()),
    };
    let t0 = Instant::now();
    let model = Onnx::load_dir(dir.as_ref(), model)?;
    let load_ms = t0.elapsed().as_secs_f64() * 1e3;
    let texts: Vec<String> = std::io::stdin().lock().lines().map(|l| Ok(serde_json::from_str(&l?)?)).collect::<Result<_, Box<dyn std::error::Error>>>()?;
    let refs: Vec<&str> = texts.iter().map(String::as_str).collect();
    let t1 = Instant::now();
    let vecs = model.encode(&refs)?;
    let secs = t1.elapsed().as_secs_f64();
    let mut query_ms: Vec<f64> = refs
        .iter()
        .take(20)
        .map(|q| {
            let t = Instant::now();
            model.encode_query(q).map(|_| t.elapsed().as_secs_f64() * 1e3)
        })
        .collect::<Result<_, _>>()?;
    query_ms.sort_by(f64::total_cmp);
    let pct = |p: f64| query_ms.get(((query_ms.len() as f64 - 1.0) * p) as usize).copied().unwrap_or(0.0);
    let mut out = std::io::BufWriter::new(std::io::stdout().lock());
    for v in vecs {
        serde_json::to_writer(&mut out, &v)?;
        out.write_all(b"\n")?;
    }
    eprintln!(
        "load_ms={load_ms:.0} texts={} secs={secs:.2} texts_per_s={:.2} query_p50_ms={:.0} query_p95_ms={:.0}",
        texts.len(),
        texts.len() as f64 / secs,
        pct(0.5),
        pct(0.95)
    );
    Ok(())
}
