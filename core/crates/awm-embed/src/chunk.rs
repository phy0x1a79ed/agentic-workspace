//! The chunker and chunk headers of `persistence.embeddings`, byte for byte.
//!
//! A chunk's text and header feed the content hash and the embedded text, so any difference
//! here re-embeds every row and moves every vector.

use awm_pycompat::pystr::{head, split, splitlines, strip};
use awm_pycompat::re::{sentence_split, tokens, translate, Dollar};
use regex::Regex;
use sha1::{Digest, Sha1};
use std::sync::LazyLock;

/// The token budget of one chunk.
pub const CHUNK_TOKENS: usize = 128;
/// Part of every content hash: bump it when chunking or headers change.
pub const CHUNKER_VERSION: u32 = 1;

static HEADING: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&translate(r"^(#{1,6}\s+.+|\*\*[^*\n]{2,80}\*\*:?\s*$)", Dollar::EndOfText).unwrap()).unwrap()
});
static LIST_ITEM: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&translate(r"^([-*+]|\d+[.)])\s", Dollar::Refuse).unwrap()).unwrap());

/// One searchable item: `body` is chunked, the rest forms each chunk's header.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Document {
    pub source_id: String,
    pub body: String,
    pub title: String,
    pub context: String,
    pub date: String,
}

/// Counts the tokens of each text.
pub type TokenCount<'a> = &'a dyn Fn(&[&str]) -> Vec<usize>;

/// `len(re.findall(r"\w+|[^\w\s]", t))`: the token count used when no model is loaded.
pub fn approx_tokens(texts: &[&str]) -> Vec<usize> {
    texts.iter().map(|t| tokens(t).len()).collect()
}

/// Split into `(section, text)`: headings open sections, blank lines and list starts split.
fn blocks(body: &str) -> Vec<(String, String)> {
    let mut section = String::new();
    let mut out = Vec::new();
    let mut cur: Vec<&str> = Vec::new();
    let flush = |cur: &mut Vec<&str>, out: &mut Vec<(String, String)>, section: &str| {
        let text = strip(&cur.join("\n")).to_string();
        if !text.is_empty() {
            out.push((section.to_string(), text));
        }
        cur.clear();
    };
    for line in splitlines(body, false) {
        let s = strip(line);
        if HEADING.is_match(s) {
            flush(&mut cur, &mut out, &section);
            section = strip(s.trim_matches(|c| matches!(c, '#' | '*' | ' ' | ':'))).to_string();
            continue;
        }
        if s.is_empty() {
            flush(&mut cur, &mut out, &section);
            continue;
        }
        if let Some(last) = cur.last() {
            if LIST_ITEM.is_match(s) && !LIST_ITEM.is_match(strip(last)) {
                flush(&mut cur, &mut out, &section);
            }
        }
        cur.push(line);
    }
    flush(&mut cur, &mut out, &section);
    out
}

fn pack(units: &[&str], counts: &[usize], target: usize, sep: &str) -> Vec<(String, usize)> {
    let mut out = Vec::new();
    let mut cur: Vec<&str> = Vec::new();
    let mut n = 0;
    for (u, &c) in units.iter().zip(counts) {
        if !cur.is_empty() && n + c > target {
            out.push((cur.join(sep), n));
            cur.clear();
            n = 0;
        }
        cur.push(u);
        n += c;
    }
    if !cur.is_empty() {
        out.push((cur.join(sep), n));
    }
    out
}

/// Split an over-budget block at sentence ends, then any over-budget sentence at words.
fn split_long(text: &str, target: usize, ntok: TokenCount) -> Vec<(String, usize)> {
    let sents: Vec<&str> = sentence_split(text).into_iter().filter(|s| !strip(s).is_empty()).collect();
    let mut pieces: Vec<(String, usize)> = Vec::new();
    for (sent, n) in sents.iter().zip(ntok(&sents)) {
        if n <= target {
            pieces.push((sent.to_string(), n));
        } else {
            let words = split(sent);
            pieces.extend(pack(&words, &ntok(&words), target, " "));
        }
    }
    let units: Vec<&str> = pieces.iter().map(|(p, _)| p.as_str()).collect();
    let counts: Vec<usize> = pieces.iter().map(|&(_, n)| n).collect();
    pack(&units, &counts, target, " ")
}

/// Pack structural blocks into `(section, text)` chunks of at most `target` tokens.
///
/// A block never merges across a section change once the chunk is half full, and a chunk only
/// starts mid-block when the block alone is over budget.
pub fn chunk(body: &str, target: usize, ntok: TokenCount) -> Vec<(String, String)> {
    let blocks = blocks(body);
    if blocks.is_empty() {
        return Vec::new();
    }
    let texts: Vec<&str> = blocks.iter().map(|(_, t)| t.as_str()).collect();
    let mut chunks = Vec::new();
    let mut cur: Vec<String> = Vec::new();
    let mut cur_sec = String::new();
    let mut cur_n = 0;
    for ((sec, text), n) in blocks.iter().zip(ntok(&texts)) {
        let parts = if n <= target { vec![(text.clone(), n)] } else { split_long(text, target, ntok) };
        for (part, pn) in parts {
            if !cur.is_empty() && (cur_n + pn > target || (*sec != cur_sec && cur_n > target / 2)) {
                chunks.push((cur_sec.clone(), cur.join("\n")));
                cur.clear();
                cur_n = 0;
            }
            if cur.is_empty() {
                cur_sec = sec.clone();
            }
            cur.push(part);
            cur_n += pn;
        }
    }
    if !cur.is_empty() {
        chunks.push((cur_sec, cur.join("\n")));
    }
    chunks
}

/// `title — context · date — section`, omitting empty parts.
pub fn header(doc: &Document, section: &str) -> String {
    let mut parts: Vec<String> = Vec::new();
    let title = strip(&doc.title);
    if !title.is_empty() {
        parts.push(head(title, 200).to_string());
    }
    let meta: Vec<&str> = [doc.context.as_str(), doc.date.as_str()].into_iter().filter(|x| !x.is_empty()).collect();
    if !meta.is_empty() {
        parts.push(meta.join(" \u{b7} "));
    }
    if !section.is_empty() {
        parts.push(head(section, 120).to_string());
    }
    parts.join(" \u{2014} ")
}

/// The hash that decides whether a document's chunks are stale.
pub fn content_hash(doc: &Document) -> String {
    let raw = [CHUNKER_VERSION.to_string(), CHUNK_TOKENS.to_string(), doc.title.clone(), doc.context.clone(), doc.date.clone(), doc.body.clone()]
        .join("\0");
    hex(&Sha1::digest(raw.as_bytes()))
}

/// The key of one embedded text in a chunks table: `sha1("1\0" + text)`.
pub fn text_hash(text: &str) -> String {
    hex(&Sha1::digest(format!("1\0{text}").as_bytes()))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// `(header, text)` per chunk. A document without a body indexes its title alone.
pub fn chunks_of(doc: &Document, ntok: TokenCount) -> Vec<(String, String)> {
    let parts = chunk(&doc.body, CHUNK_TOKENS, ntok);
    if parts.is_empty() {
        let title = strip(&doc.title);
        return if title.is_empty() { Vec::new() } else { vec![(String::new(), title.to_string())] };
    }
    parts.iter().map(|(sec, text)| (header(doc, sec), text.clone())).collect()
}

/// The text a chunk embeds: its header, a newline and its text, or the text alone.
pub fn joined(head: &str, text: &str) -> String {
    if head.is_empty() {
        text.to_string()
    } else {
        format!("{head}\n{text}")
    }
}
