//! difflib's `SequenceMatcher` (no junk function, autojunk on), `ratio` and `unified_diff`.
//!
//! The matching follows CPython 3.12 step for step: a different longest-match tie-break gives a
//! different, equally valid diff, and journal stores the diff text.

use crate::pystr::splitlines;
use std::collections::HashMap;
use std::hash::Hash;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct Match {
    pub a: usize,
    pub b: usize,
    pub size: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tag {
    Replace,
    Delete,
    Insert,
    Equal,
}

impl Tag {
    pub fn as_str(self) -> &'static str {
        match self {
            Tag::Replace => "replace",
            Tag::Delete => "delete",
            Tag::Insert => "insert",
            Tag::Equal => "equal",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Opcode {
    pub tag: Tag,
    pub i1: usize,
    pub i2: usize,
    pub j1: usize,
    pub j2: usize,
}

/// `SequenceMatcher(None, a, b)`.
pub struct SequenceMatcher<'s, T> {
    a: &'s [T],
    b: &'s [T],
    b2j: HashMap<&'s T, Vec<usize>>,
}

impl<'s, T: Eq + Hash> SequenceMatcher<'s, T> {
    pub fn new(a: &'s [T], b: &'s [T]) -> Self {
        let mut b2j: HashMap<&T, Vec<usize>> = HashMap::new();
        for (i, elt) in b.iter().enumerate() {
            b2j.entry(elt).or_default().push(i);
        }
        let n = b.len();
        if n >= 200 {
            let ntest = n / 100 + 1;
            b2j.retain(|_, idxs| idxs.len() <= ntest);
        }
        SequenceMatcher { a, b, b2j }
    }

    pub fn find_longest_match(&self, alo: usize, ahi: usize, blo: usize, bhi: usize) -> Match {
        let (a, b) = (self.a, self.b);
        let (mut besti, mut bestj, mut bestsize) = (alo, blo, 0);
        let mut j2len: HashMap<usize, usize> = HashMap::new();
        for (i, ai) in a.iter().enumerate().take(ahi).skip(alo) {
            let mut newj2len = HashMap::new();
            if let Some(js) = self.b2j.get(ai) {
                for &j in js {
                    if j < blo {
                        continue;
                    }
                    if j >= bhi {
                        break;
                    }
                    let k = j.checked_sub(1).and_then(|p| j2len.get(&p)).copied().unwrap_or(0) + 1;
                    newj2len.insert(j, k);
                    if k > bestsize {
                        besti = i + 1 - k;
                        bestj = j + 1 - k;
                        bestsize = k;
                    }
                }
            }
            j2len = newj2len;
        }
        while besti > alo && bestj > blo && a[besti - 1] == b[bestj - 1] {
            besti -= 1;
            bestj -= 1;
            bestsize += 1;
        }
        while besti + bestsize < ahi && bestj + bestsize < bhi && a[besti + bestsize] == b[bestj + bestsize] {
            bestsize += 1;
        }
        Match { a: besti, b: bestj, size: bestsize }
    }

    pub fn get_matching_blocks(&self) -> Vec<Match> {
        let (la, lb) = (self.a.len(), self.b.len());
        let mut queue = vec![(0, la, 0, lb)];
        let mut blocks = Vec::new();
        while let Some((alo, ahi, blo, bhi)) = queue.pop() {
            let m = self.find_longest_match(alo, ahi, blo, bhi);
            if m.size > 0 {
                blocks.push(m);
                if alo < m.a && blo < m.b {
                    queue.push((alo, m.a, blo, m.b));
                }
                if m.a + m.size < ahi && m.b + m.size < bhi {
                    queue.push((m.a + m.size, ahi, m.b + m.size, bhi));
                }
            }
        }
        blocks.sort();
        let mut merged = Vec::new();
        let (mut i1, mut j1, mut k1) = (0, 0, 0);
        for m in blocks {
            if i1 + k1 == m.a && j1 + k1 == m.b {
                k1 += m.size;
            } else {
                if k1 > 0 {
                    merged.push(Match { a: i1, b: j1, size: k1 });
                }
                (i1, j1, k1) = (m.a, m.b, m.size);
            }
        }
        if k1 > 0 {
            merged.push(Match { a: i1, b: j1, size: k1 });
        }
        merged.push(Match { a: la, b: lb, size: 0 });
        merged
    }

    pub fn get_opcodes(&self) -> Vec<Opcode> {
        let (mut i, mut j) = (0, 0);
        let mut out = Vec::new();
        for m in self.get_matching_blocks() {
            let tag = if i < m.a && j < m.b {
                Some(Tag::Replace)
            } else if i < m.a {
                Some(Tag::Delete)
            } else if j < m.b {
                Some(Tag::Insert)
            } else {
                None
            };
            if let Some(tag) = tag {
                out.push(Opcode { tag, i1: i, i2: m.a, j1: j, j2: m.b });
            }
            (i, j) = (m.a + m.size, m.b + m.size);
            if m.size > 0 {
                out.push(Opcode { tag: Tag::Equal, i1: m.a, i2: i, j1: m.b, j2: j });
            }
        }
        out
    }

    pub fn get_grouped_opcodes(&self, n: usize) -> Vec<Vec<Opcode>> {
        let mut codes = self.get_opcodes();
        if codes.is_empty() {
            codes.push(Opcode { tag: Tag::Equal, i1: 0, i2: 1, j1: 0, j2: 1 });
        }
        if codes[0].tag == Tag::Equal {
            let c = &mut codes[0];
            c.i1 = c.i1.max(c.i2.saturating_sub(n));
            c.j1 = c.j1.max(c.j2.saturating_sub(n));
        }
        let last = codes.len() - 1;
        if codes[last].tag == Tag::Equal {
            let c = &mut codes[last];
            c.i2 = c.i2.min(c.i1 + n);
            c.j2 = c.j2.min(c.j1 + n);
        }
        let nn = n + n;
        let mut groups = Vec::new();
        let mut group = Vec::new();
        for mut c in codes {
            if c.tag == Tag::Equal && c.i2 - c.i1 > nn {
                group.push(Opcode { tag: Tag::Equal, i1: c.i1, i2: c.i2.min(c.i1 + n), j1: c.j1, j2: c.j2.min(c.j1 + n) });
                groups.push(std::mem::take(&mut group));
                c.i1 = c.i1.max(c.i2.saturating_sub(n));
                c.j1 = c.j1.max(c.j2.saturating_sub(n));
            }
            group.push(c);
        }
        if !group.is_empty() && !(group.len() == 1 && group[0].tag == Tag::Equal) {
            groups.push(group);
        }
        groups
    }

    pub fn ratio(&self) -> f64 {
        let matches: usize = self.get_matching_blocks().iter().map(|m| m.size).sum();
        let length = self.a.len() + self.b.len();
        if length == 0 {
            1.0
        } else {
            2.0 * matches as f64 / length as f64
        }
    }
}

/// `SequenceMatcher(None, a, b).ratio()` over the characters of two strings.
pub fn ratio(a: &str, b: &str) -> f64 {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    SequenceMatcher::new(&a, &b).ratio()
}

fn format_range(start: usize, stop: usize) -> String {
    let mut beginning = start + 1;
    let length = stop - start;
    if length == 1 {
        return beginning.to_string();
    }
    if length == 0 {
        beginning -= 1;
    }
    format!("{beginning},{length}")
}

/// `"".join(unified_diff(a, b, fromfile, tofile))` over lines that keep their ends.
pub fn unified_diff(a: &[&str], b: &[&str], fromfile: &str, tofile: &str) -> String {
    let mut out = String::new();
    let groups = SequenceMatcher::new(a, b).get_grouped_opcodes(3);
    for (gi, group) in groups.iter().enumerate() {
        if gi == 0 {
            out.push_str(&format!("--- {fromfile}\n+++ {tofile}\n"));
        }
        let (first, last) = (group[0], group[group.len() - 1]);
        out.push_str(&format!(
            "@@ -{} +{} @@\n",
            format_range(first.i1, last.i2),
            format_range(first.j1, last.j2)
        ));
        for c in group {
            if c.tag == Tag::Equal {
                for line in &a[c.i1..c.i2] {
                    out.push(' ');
                    out.push_str(line);
                }
                continue;
            }
            if matches!(c.tag, Tag::Replace | Tag::Delete) {
                for line in &a[c.i1..c.i2] {
                    out.push('-');
                    out.push_str(line);
                }
            }
            if matches!(c.tag, Tag::Replace | Tag::Insert) {
                for line in &b[c.j1..c.j2] {
                    out.push('+');
                    out.push_str(line);
                }
            }
        }
    }
    out
}

/// journal's stored edit diff: `unified_diff(old.splitlines(True), new.splitlines(True), ...)`.
pub fn unified_diff_text(old: &str, new: &str, fromfile: &str, tofile: &str) -> String {
    unified_diff(&splitlines(old, true), &splitlines(new, true), fromfile, tofile)
}
