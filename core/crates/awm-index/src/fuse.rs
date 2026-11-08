//! Min-max fusion of a dense and a keyword ranking, as `persistence.embeddings.fuse`.

use indexmap::IndexMap;

/// Weight of the dense ranking.
pub const ALPHA: f64 = 0.4;
/// How deep each ranked list reaches before fusion.
pub const FUSION_DEPTH: usize = 100;

/// Words left out of a keyword query, unless nothing else is left.
pub const STOPWORDS: &[&str] = &[
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "by", "from", "at", "as", "is", "are", "was",
    "were", "be", "been", "it", "its", "this", "that", "what", "which", "who", "how", "why", "when", "where", "do",
    "does", "did", "can", "i", "my", "me", "we", "you", "they", "not",
];

/// Scores normalised over their top `depth`, keeping only those at or above the cut.
pub fn minmax_top(scores: &IndexMap<String, f64>, depth: usize) -> IndexMap<String, f64> {
    let mut top: Vec<f64> = scores.values().copied().collect();
    top.sort_by(|a, b| b.total_cmp(a));
    top.truncate(depth);
    let (Some(&hi), Some(&lo)) = (top.first(), top.last()) else {
        return IndexMap::new();
    };
    scores
        .iter()
        .filter(|(_, &v)| v >= lo)
        .map(|(d, &v)| (d.clone(), if hi > lo { (v - lo) / (hi - lo) } else { 1.0 }))
        .collect()
}

/// `alpha * dense + (1 - alpha) * keyword`, each min-max normalised over its top `depth`.
pub fn fuse(dense: &IndexMap<String, f64>, keyword: &IndexMap<String, f64>, alpha: f64, depth: usize) -> IndexMap<String, f64> {
    let a = minmax_top(dense, depth);
    let b = minmax_top(keyword, depth);
    let mut out = IndexMap::new();
    for d in a.keys().chain(b.keys()) {
        if !out.contains_key(d) {
            out.insert(d.clone(), alpha * a.get(d).copied().unwrap_or(0.0) + (1.0 - alpha) * b.get(d).copied().unwrap_or(0.0));
        }
    }
    out
}

/// Ids best first by score, ties by id, as `sorted(fused, key=lambda k: (-fused[k], k))`.
pub fn ranked(scores: &IndexMap<String, f64>) -> Vec<String> {
    let mut ids: Vec<(&String, f64)> = scores.iter().map(|(k, &v)| (k, v)).collect();
    ids.sort_by(|a, b| b.1.total_cmp(&a.1).then_with(|| a.0.cmp(b.0)));
    ids.into_iter().map(|(k, _)| k.clone()).collect()
}
