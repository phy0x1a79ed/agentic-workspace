//! Bid pickers: robust z-scores with a ratio fallback for small rounds.

use baitan_proto::Axis;
use std::collections::BTreeMap;

pub const PICKERS: &[&str] = &["robust-z", "ratio"];
const Z_CLAMP: f64 = 2.0;
const EPS_FRAC: f64 = 0.05;

/// Index of the winning bid. Bids hold already-corrected values; the first best bid wins ties.
pub fn pick(picker: &str, axes: &[Axis], bids: &[BTreeMap<String, f64>]) -> usize {
    let robust = picker == "robust-z" && bids.len() >= 3;
    let mut score = vec![0.0; bids.len()];
    for axis in axes {
        let vals = column(axis, bids);
        let per = if robust { robust_z(axis, &vals) } else { ratio(axis, &vals) };
        for (s, x) in score.iter_mut().zip(per) {
            *s += axis.weight * x;
        }
    }
    let mut best = 0;
    for i in 1..score.len() {
        if score[i] > score[best] {
            best = i;
        }
    }
    best
}

/// A bid that omits an axis is scored at the worst value any bid gave.
fn column(axis: &Axis, bids: &[BTreeMap<String, f64>]) -> Vec<f64> {
    let present: Vec<f64> = bids.iter().filter_map(|b| b.get(&axis.name).copied()).collect();
    let worst = if axis.higher_better { present.iter().copied().fold(f64::INFINITY, f64::min) } else { present.iter().copied().fold(f64::NEG_INFINITY, f64::max) };
    let worst = if present.is_empty() { 0.0 } else { worst };
    bids.iter().map(|b| b.get(&axis.name).copied().unwrap_or(worst)).collect()
}

fn median(v: &[f64]) -> f64 {
    let mut s = v.to_vec();
    s.sort_by(|a, b| a.total_cmp(b));
    let n = s.len();
    if n % 2 == 1 { s[n / 2] } else { (s[n / 2 - 1] + s[n / 2]) / 2.0 }
}

fn robust_z(axis: &Axis, vals: &[f64]) -> Vec<f64> {
    let med = median(vals);
    let dev: Vec<f64> = vals.iter().map(|v| (v - med).abs()).collect();
    let mut scale = 1.4826 * median(&dev);
    if scale == 0.0 {
        scale = 1.253314 * dev.iter().sum::<f64>() / dev.len() as f64;
    }
    if scale == 0.0 {
        return vec![0.0; vals.len()];
    }
    let sign = if axis.higher_better { -1.0 } else { 1.0 };
    vals.iter().map(|v| (sign * (med - v) / scale).clamp(-Z_CLAMP, Z_CLAMP)).collect()
}

fn ratio(axis: &Axis, vals: &[f64]) -> Vec<f64> {
    if axis.higher_better {
        let best = vals.iter().copied().fold(f64::NEG_INFINITY, f64::max);
        return vals.iter().map(|v| if best > 0.0 { v / best } else { 1.0 }).collect();
    }
    let best = vals.iter().copied().fold(f64::INFINITY, f64::min);
    let eps = (EPS_FRAC * vals.iter().copied().fold(0.0, f64::max)).max(1e-9);
    vals.iter().map(|v| (best + eps) / (v + eps)).collect()
}
