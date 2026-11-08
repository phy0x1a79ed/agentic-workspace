//! Reciprocal rank fusion over the ranked lists one source's answer is built from.
//!
//! A live source answers in two sections whose scores do not compare. Rank position is the one
//! thing both lists have, so it is all that is fused.

use indexmap::IndexMap;
use serde_json::{Map, Value};

pub const RRF_K: f64 = 60.0;

/// The best `limit` hits across `lists`, each ordered best first. A hit seen in several lists
/// keeps its first appearance and gathers every list's share. Ties go to the newer hit.
pub fn fuse(lists: &[Vec<Map<String, Value>>], limit: usize) -> Vec<Map<String, Value>> {
    let mut scores: IndexMap<String, f64> = IndexMap::new();
    let mut first: IndexMap<String, &Map<String, Value>> = IndexMap::new();
    for hits in lists {
        for (rank, hit) in hits.iter().enumerate() {
            let id = hit.get("id").map(crate::hits::py_str).unwrap_or_default();
            *scores.entry(id.clone()).or_insert(0.0) += 1.0 / (RRF_K + rank as f64 + 1.0);
            first.entry(id).or_insert(hit);
        }
    }
    let ts = |k: &String| first[k].get("ts").and_then(Value::as_str).unwrap_or("").to_string();
    let mut ordered: Vec<&String> = scores.keys().collect();
    // Two stable sorts, as the Python: newest first, then by score.
    ordered.sort_by_key(|k| std::cmp::Reverse(ts(k)));
    ordered.sort_by(|a, b| scores[*b].total_cmp(&scores[*a]));
    ordered.into_iter().take(limit).map(|k| first[k].clone()).collect()
}
