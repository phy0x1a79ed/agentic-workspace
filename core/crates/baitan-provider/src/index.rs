//! The declared-identity cache: an inverted index from `key=value` pairs to the entries holding
//! them. A request hits an entry whose pairs contain all of the request's pairs.

use baitan_proto::{Id, PromiseId};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::time::Instant;

pub type Pair = (String, String);

#[derive(Debug, Clone)]
pub struct Flight {
    pub since: Instant,
    pub est: f64,
    pub owner: PromiseId,
}

/// A result's expanded property set and the instance id of each of its slots. A running job
/// has `flight` set; only finished entries persist.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Entry {
    pub pairs: Vec<Pair>,
    pub slots: BTreeMap<String, Id>,
    #[serde(skip)]
    pub flight: Option<Flight>,
}

pub enum Found {
    Done(BTreeMap<String, Id>),
    Flying { eid: u64, remaining: f64, slots: BTreeMap<String, Id> },
}

#[derive(Default)]
pub struct Index {
    entries: BTreeMap<u64, Entry>,
    postings: HashMap<Pair, BTreeSet<u64>>,
    next: u64,
}

pub fn normalize(pairs: impl IntoIterator<Item = Pair>) -> Vec<Pair> {
    pairs.into_iter().collect::<BTreeSet<_>>().into_iter().collect()
}

impl Index {
    pub fn from_entries(entries: Vec<Entry>) -> Self {
        let mut ix = Index::default();
        for e in entries {
            ix.insert(e);
        }
        ix
    }

    pub fn finished(&self) -> Vec<&Entry> {
        self.entries.values().filter(|e| e.flight.is_none()).collect()
    }

    pub fn insert(&mut self, e: Entry) -> u64 {
        let eid = self.next;
        self.next += 1;
        for p in &e.pairs {
            self.postings.entry(p.clone()).or_default().insert(eid);
        }
        self.entries.insert(eid, e);
        eid
    }

    pub fn remove(&mut self, eid: u64) {
        if let Some(e) = self.entries.remove(&eid) {
            for p in &e.pairs {
                if let Some(set) = self.postings.get_mut(p) {
                    set.remove(&eid);
                    if set.is_empty() {
                        self.postings.remove(p);
                    }
                }
            }
        }
    }

    pub fn finish(&mut self, eid: u64) {
        if let Some(e) = self.entries.get_mut(&eid) {
            e.flight = None;
        }
    }

    pub fn drop_ids(&mut self, ids: &[Id]) {
        let gone: Vec<u64> = self
            .entries
            .iter()
            .filter(|(_, e)| e.slots.values().any(|i| ids.contains(i)))
            .map(|(k, _)| *k)
            .collect();
        for eid in gone {
            self.remove(eid);
        }
    }

    pub fn remove_owned(&mut self, owner: &str) -> Vec<u64> {
        let gone: Vec<u64> = self
            .entries
            .iter()
            .filter(|(_, e)| e.flight.as_ref().is_some_and(|f| f.owner == owner))
            .map(|(k, _)| *k)
            .collect();
        for eid in &gone {
            self.remove(*eid);
        }
        gone
    }

    /// Intersects the postings of the request's pairs, smallest first. A finished entry beats a
    /// running one; among equals the newest wins. The entry must also hold every requested slot.
    pub fn lookup(&self, pairs: &[Pair], slots: &[String]) -> Option<Found> {
        let mut lists: Vec<&BTreeSet<u64>> = Vec::with_capacity(pairs.len());
        for p in pairs {
            lists.push(self.postings.get(p)?);
        }
        lists.sort_by_key(|l| l.len());
        let (first, rest) = lists.split_first()?;
        let mut best: Option<(bool, u64)> = None;
        for &eid in first.iter() {
            if !rest.iter().all(|l| l.contains(&eid)) {
                continue;
            }
            let e = &self.entries[&eid];
            if !slots.iter().all(|s| e.slots.contains_key(s)) {
                continue;
            }
            let rank = (e.flight.is_none(), eid);
            if best.is_none_or(|b| rank > b) {
                best = Some(rank);
            }
        }
        let (_, eid) = best?;
        let e = &self.entries[&eid];
        Some(match &e.flight {
            None => Found::Done(e.slots.clone()),
            Some(f) => Found::Flying {
                eid,
                remaining: (f.est - f.since.elapsed().as_secs_f64()).max(0.0),
                slots: e.slots.clone(),
            },
        })
    }
}
