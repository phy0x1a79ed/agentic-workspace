//! baitan's wire contract, shared by the market, providers, fronts, surfaces and the Nextflow executor.
//!
//! Design: `docs/dex-poc/DECISIONS.md`. The envelope and reply selectors follow `envelope-mapping`,
//! versions follow `version-policy`, offer sets and facts follow `mutation`, `storage` and
//! `deliverable`, identity follows `cache-identity`, and the verbs follow `tool-surface`.
//! Every struct refuses unknown keys. Nothing here hashes content: ids hash declared properties.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

/// The contract versions this build speaks, inclusive.
pub const SUPPORTED: [u32; 2] = [1, 1];
pub const V: u32 = SUPPORTED[1];

pub type PropSet = BTreeMap<String, String>;
pub type Id = String;
pub type ProviderId = String;
pub type PromiseId = String;

pub mod raise {
    pub const NO_BIDS: &str = "NoBids";
    pub const BROKEN_PROMISE: &str = "BrokenPromise";
    pub const CANCELLED: &str = "Cancelled";
    pub const UNSUPPORTED_VERSION: &str = "UnsupportedVersion";
    pub const BAD_ARGS: &str = "BadArgs";
    pub const UNKNOWN_PROMISE: &str = "UnknownPromise";
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct Envelope {
    pub v: u32,
    pub id: String,
    pub from: String,
    pub to: String,
    pub selector: String,
    pub args: Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reply_to: Option<String>,
}

/// Every selector, with typed args. Replies are `Return`, `Raise` and `DoesNotUnderstand`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "selector", content = "args", rename_all = "camelCase")]
pub enum Msg {
    // Caller verbs. `Offer` also goes market to provider, with `promise` set.
    Offer(OfferArgs),
    Status(PromiseRef),
    Cancel(PromiseRef),
    Dispose(DisposeArgs),
    Review(ReviewArgs),
    Watch(WatchArgs),
    // Provider verbs.
    Register(RegisterArgs),
    Bid(BidArgs),
    Started(StartedArgs),
    Settle(SettleArgs),
    // Market to provider and market to watcher.
    Award(AwardArgs),
    Event(PromiseStatus),
    // Replies.
    Return(Value),
    Raise(RaiseArgs),
    DoesNotUnderstand(DnuArgs),
}

pub const SELECTORS: &[&str] = &[
    "offer", "status", "cancel", "dispose", "review", "watch", "register", "bid", "started", "settle",
    "award", "event", "return", "raise", "doesNotUnderstand",
];

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OfferArgs {
    pub set: OfferSet,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub promise: Option<PromiseId>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PromiseRef {
    pub promise: PromiseId,
}

/// Disposal names exact ids. The market routes each to its holder: `holder` when given, else the
/// provider that settled it. An id nobody holds settles as done.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DisposeArgs {
    pub ids: Vec<Id>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub holder: Option<ProviderId>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReviewArgs {
    pub promise: PromiseId,
    pub scores: BTreeMap<String, f64>,
}

/// An empty list watches every promise.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WatchArgs {
    #[serde(default)]
    pub promises: Vec<PromiseId>,
}

/// `props` describe the provider. An offer's place type must be a subset of them.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RegisterArgs {
    pub provider: ProviderId,
    pub versions: [u32; 2],
    #[serde(default)]
    pub props: PropSet,
}

/// `values` is `None` for a decline. A hit bids `Done`; an in-flight duplicate bids `InFlight`
/// with its time axis set to the remaining time.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BidArgs {
    pub promise: PromiseId,
    pub provider: ProviderId,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub values: Option<BTreeMap<String, f64>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hit: Option<Hit>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum Hit {
    Done,
    InFlight,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StartedArgs {
    pub promise: PromiseId,
    pub provider: ProviderId,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SettleArgs {
    pub promise: PromiseId,
    pub provider: ProviderId,
    pub outcome: Outcome,
}

/// `Kept` carries the facts made true, `(id, at, provider)`, each id's true size in bytes, and,
/// for an instance that is a directory, the relative paths of its files. `Broken` creates no
/// instance.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub enum Outcome {
    Kept {
        facts: Vec<Fact>,
        sizes: BTreeMap<Id, u64>,
        #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
        listing: BTreeMap<Id, Vec<String>>,
    },
    Broken { message: String },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AwardArgs {
    pub promise: PromiseId,
    pub set: OfferSet,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RaiseArgs {
    pub name: String,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub supported: Option<[u32; 2]>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DnuArgs {
    pub selector: String,
}

/// The caller's view of a promise, and the payload of `event`. `life` is the provider's answer
/// to `status` while the work runs: a fact for the caller, never a trigger.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PromiseStatus {
    pub promise: PromiseId,
    pub phase: Phase,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub life: Option<Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub enum Phase {
    Open,
    Awarded { provider: ProviderId },
    Started { provider: ProviderId },
    Settled {
        provider: ProviderId,
        facts: Vec<Fact>,
        sizes: BTreeMap<Id, u64>,
        #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
        listing: BTreeMap<Id, Vec<String>>,
    },
    Failed { name: String, message: String },
}

impl Phase {
    pub fn is_final(&self) -> bool {
        matches!(self, Phase::Settled { .. } | Phase::Failed { .. })
    }
}

/// A knowledge triple. Each element is a type set from its own namespace: subjects name data
/// (`id`), predicates name relations (`rel`), objects name places (`provider`) or data.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Fact {
    pub s: PropSet,
    pub p: PropSet,
    pub o: PropSet,
}

impl Fact {
    pub fn at(id: &str, provider: &str) -> Self {
        Fact {
            s: props([("id", id)]),
            p: props([("rel", "at")]),
            o: props([("provider", provider)]),
        }
    }

    /// An `at` fact that also names where the holder keeps the item, so a fetcher needs no `src`.
    pub fn at_path(id: &str, provider: &str, path: &str) -> Self {
        let mut f = Fact::at(id, provider);
        f.o.insert("path".into(), path.into());
        f
    }

    pub fn path(&self) -> Option<&str> {
        self.o.get("path").map(String::as_str)
    }

    /// `(id, provider)` when this is an `at` fact.
    pub fn as_at(&self) -> Option<(&str, &str)> {
        (self.p.get("rel").map(String::as_str) == Some("at"))
            .then(|| Some((self.s.get("id")?.as_str(), self.o.get("provider")?.as_str())))
            .flatten()
    }
}

pub fn props<'a>(kv: impl IntoIterator<Item = (&'a str, &'a str)>) -> PropSet {
    kv.into_iter().map(|(k, v)| (k.to_string(), v.to_string())).collect()
}

/// One unit of work: a chain of steps sharing the place variable L, bound to the one provider
/// that wins the whole set. `start` lists `(input, at, place)` facts, several per input allowed.
/// `place` narrows providers to those whose registered props hold it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OfferSet {
    pub steps: Vec<Step>,
    #[serde(default)]
    pub start: Vec<Fact>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub place: Option<PropSet>,
    #[serde(default = "default_axes")]
    pub axes: Vec<Axis>,
    #[serde(default = "default_picker")]
    pub picker: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub checker: Option<Box<Step>>,
    #[serde(default = "default_window")]
    pub window_ms: u64,
    #[serde(default)]
    pub resources: Resources,
    /// The requester's estimate of input size in bytes.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub size: Option<u64>,
}

/// `op` names the job kind; `docs/dex-poc/JOBS.md` defines each kind's `spec` and slots. `props` is the declared request set:
/// contract, version when it matters, output-affecting params. `inputs` are exact ids; a
/// placement step names the slot ids of earlier steps. `spec` is the job payload, opaque to the
/// market. `to` is the destination provider of a placement step.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Step {
    pub op: String,
    #[serde(default)]
    pub props: PropSet,
    #[serde(default)]
    pub inputs: Vec<Id>,
    #[serde(default)]
    pub slots: Vec<String>,
    #[serde(default, skip_serializing_if = "Value::is_null")]
    pub spec: Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub to: Option<ProviderId>,
}

impl Step {
    pub fn is_placement(&self) -> bool {
        self.op == "place"
    }

    pub fn key(&self) -> Id {
        offer_key(self)
    }

    pub fn slot_ids(&self) -> Vec<(String, Id)> {
        let key = self.key();
        self.slots.iter().map(|s| (s.clone(), slot_id(&key, s))).collect()
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Axis {
    pub name: String,
    #[serde(default = "one")]
    pub weight: f64,
    /// Most axes (time, cost) are better low.
    #[serde(default)]
    pub higher_better: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Resources {
    pub cpus: u32,
    pub mem_mb: u64,
}

impl Default for Resources {
    fn default() -> Self {
        Resources { cpus: 1, mem_mb: 2048 }
    }
}

pub const TIME_AXIS: &str = "time";

fn default_axes() -> Vec<Axis> {
    vec![Axis { name: TIME_AXIS.into(), weight: 1.0, higher_better: false }]
}
fn default_picker() -> String {
    "robust-z".into()
}
fn default_window() -> u64 {
    500
}
fn one() -> f64 {
    1.0
}

fn h(bytes: &[u8]) -> Id {
    hex::encode(&Sha256::digest(bytes)[..16])
}

/// The id of an instance: the hash of its declared property set. `PropSet` is ordered, so two
/// providers that declare the same set agree without talking.
pub fn instance_id(props: &PropSet) -> Id {
    h(&serde_json::to_vec(props).expect("a string map serializes"))
}

/// The hash of a step's canonical request content. The job payload is part of it.
pub fn offer_key(step: &Step) -> Id {
    let canon = (&step.op, &step.props, &step.inputs, &step.spec, &step.to);
    h(&serde_json::to_vec(&canon).expect("step serializes"))
}

/// `H(offer key, slot)`: lets a later offer consume one slot alone.
pub fn slot_id(offer_key: &str, slot: &str) -> Id {
    h(&serde_json::to_vec(&(offer_key, slot)).expect("strings serialize"))
}

pub fn new_id() -> String {
    uuid::Uuid::new_v4().to_string()
}

/// Why a decoded envelope gets no normal handling. Each maps to one reply.
#[derive(Debug, Clone, PartialEq)]
pub enum Refusal {
    UnsupportedVersion,
    DoesNotUnderstand(String),
    BadArgs(String),
}

impl Envelope {
    pub fn new(from: &str, to: &str, msg: &Msg) -> Self {
        let (selector, args) = split(msg);
        Envelope { v: V, id: new_id(), from: from.into(), to: to.into(), selector, args, reply_to: None }
    }

    /// One line of the carrier. Refuses unknown keys.
    pub fn decode(line: &str) -> Result<Self, String> {
        serde_json::from_str(line).map_err(|e| e.to_string())
    }

    pub fn encode(&self) -> String {
        serde_json::to_string(self).expect("envelope serializes")
    }

    pub fn msg(&self) -> Result<Msg, Refusal> {
        if self.v < SUPPORTED[0] || self.v > SUPPORTED[1] {
            return Err(Refusal::UnsupportedVersion);
        }
        if !SELECTORS.contains(&self.selector.as_str()) {
            return Err(Refusal::DoesNotUnderstand(self.selector.clone()));
        }
        let tagged = serde_json::json!({ "selector": self.selector, "args": self.args });
        serde_json::from_value(tagged).map_err(|e| Refusal::BadArgs(e.to_string()))
    }

    /// A reply carries the `v` of the message it answers.
    pub fn reply(&self, msg: &Msg) -> Envelope {
        let (selector, args) = split(msg);
        Envelope {
            v: self.v,
            id: new_id(),
            from: self.to.clone(),
            to: self.from.clone(),
            selector,
            args,
            reply_to: Some(self.id.clone()),
        }
    }

    pub fn refusal_reply(&self, r: &Refusal) -> Envelope {
        let msg = match r {
            Refusal::UnsupportedVersion => Msg::Raise(RaiseArgs {
                name: raise::UNSUPPORTED_VERSION.into(),
                message: format!("v{} not in {:?}", self.v, SUPPORTED),
                supported: Some(SUPPORTED),
            }),
            Refusal::DoesNotUnderstand(s) => Msg::DoesNotUnderstand(DnuArgs { selector: s.clone() }),
            Refusal::BadArgs(e) => {
                Msg::Raise(RaiseArgs { name: raise::BAD_ARGS.into(), message: e.clone(), supported: None })
            }
        };
        let mut env = self.reply(&msg);
        if matches!(r, Refusal::UnsupportedVersion) {
            env.v = V;
        }
        env
    }
}

pub fn raise_msg(name: &str, message: impl Into<String>) -> Msg {
    Msg::Raise(RaiseArgs { name: name.into(), message: message.into(), supported: None })
}

fn split(msg: &Msg) -> (String, Value) {
    let mut v = serde_json::to_value(msg).expect("msg serializes");
    let selector = v["selector"].as_str().expect("tagged").to_string();
    (selector, v.get_mut("args").map(Value::take).unwrap_or(Value::Null))
}

/// One caller verb of the generated surfaces. `cli_only` verbs are absent from MCP and HTTP.
pub struct VerbSpec {
    pub name: &'static str,
    pub summary: &'static str,
    pub cli_only: bool,
    /// JSON schema of the verb's args.
    pub schema: &'static str,
}

/// The single catalog the CLI, MCP and HTTP surfaces are generated from. Provider verbs are absent.
pub const CALLER_VERBS: &[VerbSpec] = &[
    VerbSpec {
        name: "offer",
        summary: "Post one offer set; returns a promise id at once.",
        cli_only: false,
        schema: r#"{"type":"object","properties":{"set":{"type":"object","description":"Offer set: steps, start, place, axes, picker, checker, window_ms, resources, size."}},"required":["set"],"additionalProperties":false}"#,
    },
    VerbSpec {
        name: "status",
        summary: "A promise's phase; once settled, its facts and true sizes.",
        cli_only: false,
        schema: r#"{"type":"object","properties":{"promise":{"type":"string"}},"required":["promise"],"additionalProperties":false}"#,
    },
    VerbSpec {
        name: "cancel",
        summary: "Withdraw before award; after award, stop the work and break the promise.",
        cli_only: false,
        schema: r#"{"type":"object","properties":{"promise":{"type":"string"}},"required":["promise"],"additionalProperties":false}"#,
    },
    VerbSpec {
        name: "dispose",
        summary: "Delete instances at their holder, with no bid round.",
        cli_only: false,
        schema: r#"{"type":"object","properties":{"ids":{"type":"array","items":{"type":"string"}},"holder":{"type":"string"}},"required":["ids"],"additionalProperties":false}"#,
    },
    VerbSpec {
        name: "review",
        summary: "Score a settled promise per axis; never fails the result.",
        cli_only: false,
        schema: r#"{"type":"object","properties":{"promise":{"type":"string"},"scores":{"type":"object","additionalProperties":{"type":"number"}}},"required":["promise","scores"],"additionalProperties":false}"#,
    },
    VerbSpec {
        name: "watch",
        summary: "Stream events for promise ids, or all promises when none are named.",
        cli_only: true,
        schema: r#"{"type":"object","properties":{"promises":{"type":"array","items":{"type":"string"}}},"additionalProperties":false}"#,
    },
];

#[cfg(test)]
mod tests;
