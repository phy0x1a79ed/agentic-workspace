//! A `$contains` tree compiled to a Lance full-text query of phrases.
//!
//! A tree that only excludes cannot run as a full-text query on its own, so it compiles to its
//! positive opposite with `negated` set, and the caller keeps the entries it does not match.

use crate::grammar::Node;
use awm_pycompat::chars::is_word;
use awm_pycompat::pystr::lower;
use lancedb::index::scalar::{BooleanQuery, FtsQuery, MultiMatchQuery, Occur, PhraseQuery};

/// `text` as the full-text index tokenizes it: lowercase runs of letters and digits.
pub fn words(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    for c in text.chars() {
        if c != '_' && is_word(c) {
            cur.push(c);
        } else if !cur.is_empty() {
            out.push(lower(&cur));
            cur.clear();
        }
    }
    if !cur.is_empty() {
        out.push(lower(&cur));
    }
    out
}

/// A compiled tree: matches `query`, or with `negated` everything `query` does not.
#[derive(Debug, Clone)]
pub struct Tree {
    pub query: FtsQuery,
    pub negated: bool,
}

fn should(mut queries: Vec<FtsQuery>) -> FtsQuery {
    if queries.len() == 1 {
        return queries.pop().unwrap();
    }
    BooleanQuery::new(queries.into_iter().map(|q| (Occur::Should, q))).into()
}

fn boolean(clauses: Vec<(Occur, FtsQuery)>) -> FtsQuery {
    BooleanQuery::new(clauses).into()
}

/// The full-text query for a `$contains` tree over the `content` columns.
pub fn compile_tree(node: &Node, content: &[&str]) -> Tree {
    match node {
        Node::Lit(text) => {
            let phrase = words(text).join(" ");
            let leaves = content.iter().map(|c| PhraseQuery::new(phrase.clone()).with_column(Some(c.to_string())).into()).collect();
            Tree { query: should(leaves), negated: false }
        }
        Node::Op(op, items) => {
            let items: Vec<Tree> = items.iter().map(|x| compile_tree(x, content)).collect();
            let (neg, pos): (Vec<Tree>, Vec<Tree>) = items.into_iter().partition(|t| t.negated);
            let pos: Vec<FtsQuery> = pos.into_iter().map(|t| t.query).collect();
            let neg: Vec<FtsQuery> = neg.into_iter().map(|t| t.query).collect();
            match op.as_str() {
                "nor" if !neg.is_empty() => {
                    // NOT (p OR NOT n) = n AND NOT p
                    let mut c: Vec<_> = neg.into_iter().map(|q| (Occur::Must, q)).collect();
                    c.extend(pos.into_iter().map(|q| (Occur::MustNot, q)));
                    Tree { query: boolean(c), negated: false }
                }
                "nor" => Tree { query: should(pos), negated: true },
                "and" if !pos.is_empty() => {
                    let mut c: Vec<_> = pos.into_iter().map(|q| (Occur::Must, q)).collect();
                    c.extend(neg.into_iter().map(|q| (Occur::MustNot, q)));
                    Tree { query: boolean(c), negated: false }
                }
                // NOT a AND NOT b = NOT (a OR b)
                "and" => Tree { query: should(neg), negated: true },
                _ if neg.is_empty() => Tree { query: should(pos), negated: false },
                _ => {
                    // p OR NOT n = NOT (n AND NOT p)
                    let mut inner: Vec<(Occur, FtsQuery)> = if neg.len() == 1 {
                        neg.into_iter().map(|q| (Occur::Must, q)).collect()
                    } else {
                        vec![(Occur::Must, boolean(neg.into_iter().map(|q| (Occur::Must, q)).collect()))]
                    };
                    if !pos.is_empty() {
                        inner.push((Occur::MustNot, should(pos)));
                    }
                    Tree { query: boolean(inner), negated: true }
                }
            }
        }
    }
}

/// The any-word BM25 query of an `$about`: its non-stopword words, or all its words.
pub fn about_query(about: &str, content: &[&str]) -> Option<FtsQuery> {
    let all = words(about);
    let mut terms: Vec<String> = all.iter().filter(|w| !crate::fuse::STOPWORDS.contains(&w.as_str())).cloned().collect();
    if terms.is_empty() {
        terms = all;
    }
    if terms.is_empty() {
        return None;
    }
    let mut seen = std::collections::HashSet::new();
    terms.retain(|t| seen.insert(t.clone()));
    MultiMatchQuery::try_new(terms.join(" "), content.iter().map(|c| c.to_string()).collect()).ok().map(Into::into)
}
