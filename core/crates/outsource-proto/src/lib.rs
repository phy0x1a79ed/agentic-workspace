//! outsource's wire contract, shared by the market, providers, surfaces and the Nextflow executor.
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
    Import(ImportArgs),
    #[serde(rename = "property_add")]
    PropertyAdd(PropertyAddArgs),
    Properties(PropertiesArgs),
    Review(ReviewArgs),
    Watch(WatchArgs),
    // Provider verbs.
    Register(RegisterArgs),
    Bid(BidArgs),
    Started(StartedArgs),
    Settle(SettleArgs),
    Transfer(TransferArgs),
    // Market to provider and market to watcher.
    Award(AwardArgs),
    Event(PromiseStatus),
    // Replies.
    Return(Value),
    Raise(RaiseArgs),
    DoesNotUnderstand(DnuArgs),
}

pub const SELECTORS: &[&str] = &[
    "offer", "status", "cancel", "dispose", "import", "property_add", "properties", "review", "watch", "register", "bid", "started", "settle", "transfer",
    "award", "event", "return", "raise", "doesNotUnderstand",
];

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OfferArgs {
    pub set: OfferSet,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub promise: Option<PromiseId>,
    /// Market to provider: the providers whose registered routes reach the recipient, so a holder
    /// it cannot reach can still push to it.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub inbound: Vec<ProviderId>,
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

/// Registers material that already sits at provider `to`. The market sends it straight there,
/// with no bid round. A material with a `ty` is held under the id of that property set; any
/// other is held under its stat or commit identity.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ImportArgs {
    pub to: ProviderId,
    pub materials: Vec<ImportItem>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ImportItem {
    pub path: String,
    #[serde(default, rename = "type", skip_serializing_if = "Option::is_none")]
    pub ty: Option<PropSet>,
}

/// Adds a key to the market's property vocabulary, or extends an existing one: the description is
/// replaced, and `values` are added to its allowed list (a key with no list accepts any value;
/// naming values on such a key closes it to them).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PropertyAddArgs {
    pub name: String,
    pub description: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub values: Option<Vec<String>>,
}

/// Reads the property vocabulary: each key with its description and allowed values, if listed.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PropertiesArgs {}

/// A type's values are case-folded before its id is taken; keys are exact.
pub fn fold_type(ty: &PropSet) -> PropSet {
    ty.iter().map(|(k, v)| (k.clone(), v.to_lowercase())).collect()
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

/// `props` describe the provider. An offer's place type must be a subset of them. `routes` names
/// the providers its rsync reaches.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RegisterArgs {
    pub provider: ProviderId,
    pub versions: [u32; 2],
    #[serde(default)]
    pub props: PropSet,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub routes: Vec<ProviderId>,
}

/// Copies `src` on provider `from` into the directory `dest` on provider `to`: under its base name,
/// or, with `files`, each listed relative path under `src` to the same path under `dest`. One end
/// asks the market, which asks the other end to run rsync over its own route to the asker.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TransferArgs {
    pub from: ProviderId,
    pub to: ProviderId,
    pub src: String,
    pub dest: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub files: Vec<String>,
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

/// What running a promise's protocol cost, as the provider measured it. Every field is `None`
/// when the backend could not measure it; a result that was reused rather than run has no stats.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunStats {
    /// Peak resident memory of the protocol and its children, in KiB.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_rss_kb: Option<u64>,
    /// User plus system CPU seconds.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cpu_s: Option<f64>,
    /// Wall seconds of the protocol alone, queue wait excluded.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub wall_s: Option<f64>,
}

impl RunStats {
    /// Folds the stats of a later run step in: peak memory is the larger, times add.
    pub fn merge(&mut self, o: &RunStats) {
        self.max_rss_kb = self.max_rss_kb.max(o.max_rss_kb);
        self.cpu_s = sum(self.cpu_s, o.cpu_s);
        self.wall_s = sum(self.wall_s, o.wall_s);
    }
}

fn sum(a: Option<f64>, b: Option<f64>) -> Option<f64> {
    match (a, b) {
        (Some(a), Some(b)) => Some(a + b),
        (a, b) => a.or(b),
    }
}

/// `Kept` carries the facts made true, `(id, at, provider)`, each id's true size in bytes, and,
/// for an instance that is a directory, the relative paths of its files, and the run's stats.
/// `Broken` creates no instance.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub enum Outcome {
    Kept {
        facts: Vec<Fact>,
        sizes: BTreeMap<Id, u64>,
        #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
        listing: BTreeMap<Id, Vec<String>>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        stats: Option<RunStats>,
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
        #[serde(default, skip_serializing_if = "Option::is_none")]
        stats: Option<RunStats>,
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
/// `place` narrows providers to those whose registered props hold it. `to` runs the bid round
/// with that one provider alone.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OfferSet {
    pub steps: Vec<Step>,
    #[serde(default)]
    pub start: Vec<Fact>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub place: Option<PropSet>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub to: Option<ProviderId>,
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

/// `op` names the job kind; `docs/outsource/JOBS.md` defines each kind's `spec` and slots. `props` is the declared request set:
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

    /// A step that only moves data: a placement or a `prep`.
    pub fn is_move(&self) -> bool {
        self.is_placement() || self.op == "prep"
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

const OFFER_SUMMARY: &str = "Post one job to the compute market; returns a promise id at once (poll `status`). \
`requires`: a path on this node (absolute or relative to the cwd; file or folder) that appears in the work dir under its base name, or an object `{path?, type?, name?}`. \
`type` is a property set (string to string) that is the material's identity in place of its stat or commit: every copy of it, at any path on any provider, has one id, and a provider that holds it fetches nothing. \
Each key must be in the market's vocabulary (list it with `properties`, extend it with `property_add`) and each value in its key's list if it has one; values are lower-cased. A bad key or value is refused with the closest known ones. \
An entry with a `type` and no `path` names material that already sits at a provider (the market finds the holder); it appears under `name`, default the type's values joined by `-`. \
`protocol`: one bash command run in the work dir. `produces`: a path relative to the work dir, copied back into `dest` (default the cwd) under the same relative path, or `{path, type}`: the output stays at the provider and is held under that type for later offers to require. \
`to`: a provider id; only that provider is asked to bid. \
A `.env` file in `requires` (YAML with `container: docker://...`) is the environment: the protocol runs inside it with apptainer; at most one. \
Pass each git repo as its own `requires` path. A result is reused when the protocol and every material are unchanged (a file by mtime, a repo by HEAD commit, any other folder by its own mtime); \
a repo with uncommitted changes still counts as its commit and the reply warns. Example: \
{\"requires\":[\"/data/iphop.env\",\"/data/counts.tsv\",{\"type\":{\"db\":\"uniref50\",\"release\":\"2024_01\"},\"name\":\"uniref50\"}],\"produces\":[\"out/fit.rds\"],\"protocol\":\"mkdir out && Rscript fit.R counts.tsv uniref50 out/fit.rds\",\"to\":\"sockeye\",\"resources\":{\"cpus\":8,\"mem_mb\":32000}}. \
A remote download is the same verb with a typed output: {\"requires\":[],\"produces\":[{\"path\":\"uniref50.dmnd\",\"type\":{\"db\":\"uniref50\",\"release\":\"2024_01\"}}],\"protocol\":\"wget -q https://example.org/uniref50.fa.gz && diamond makedb --in uniref50.fa.gz -d uniref50\",\"to\":\"sockeye\"}";

const OFFER_SCHEMA: &str = r#"{"type":"object","properties":{"requires":{"type":"array","items":{"oneOf":[{"type":"string"},{"type":"object","properties":{"path":{"type":"string"},"type":{"type":"object","additionalProperties":{"type":"string"}},"name":{"type":"string"}},"additionalProperties":false}]},"description":"Inputs: a path on the caller's node (file or folder, lands in the work dir under its base name), or {path?, type?, name?}. One may be a .env file."},"produces":{"type":"array","items":{"oneOf":[{"type":"string"},{"type":"object","properties":{"path":{"type":"string"},"type":{"type":"object","additionalProperties":{"type":"string"}}},"required":["path","type"],"additionalProperties":false}]},"description":"Paths relative to the work dir to copy back (a folder brings its files), or {path, type} to keep at the provider under that type."},"protocol":{"type":"string","description":"One bash command, run in the work dir."},"to":{"type":"string","description":"Provider id: only it is asked to bid."},"metrics":{"type":"array","items":{"type":"string"},"description":"Axes bids are scored on; default [\"time\"]."},"resources":{"type":"object","description":"{cpus, mem_mb} the job needs; default {cpus:1, mem_mb:2048}."},"dest":{"type":"string","description":"Directory the produces are copied into; default the caller's cwd."}},"required":["requires","produces","protocol"],"additionalProperties":false}"#;

const PREP_SUMMARY: &str = "Stage materials at one provider ahead of the jobs that need them: the data-transfer form of a directed offer, with no protocol. \
The provider fetches each material it lacks (rsync) and pulls a `.env` image into its cache; material it holds, or has in flight, costs nothing. Returns a promise id like `offer`. \
`requires` takes the same entries as `offer`. Example: \
{\"to\":\"sockeye\",\"requires\":[\"/data/iphop.env\",\"/data/counts.tsv\",{\"type\":{\"db\":\"uniref50\",\"release\":\"2024_01\"}}]}";

const PREP_SCHEMA: &str = r#"{"type":"object","properties":{"requires":{"type":"array","items":{"oneOf":[{"type":"string"},{"type":"object","properties":{"path":{"type":"string"},"type":{"type":"object","additionalProperties":{"type":"string"}},"name":{"type":"string"}},"additionalProperties":false}]},"description":"Materials to stage, as in `offer`."},"to":{"type":"string","description":"Provider id that stages them."}},"required":["requires","to"],"additionalProperties":false}"#;

const IMPORT_SUMMARY: &str = "Register material that already sits at a provider; it checks, where `prep` moves. Goes straight to that provider with no bid round. \
Each path (absolute, on the provider) must exist, else the call fails. A material with a `type` is held under that property set (keys and values are checked against `properties`), so later offers can require it by type; one without is held under its stat or commit identity. \
Returns the ids and the holder facts. Example: \
{\"to\":\"sockeye\",\"materials\":[{\"path\":\"/scratch/db/uniref50.dmnd\",\"type\":{\"db\":\"uniref50\",\"release\":\"2024_01\"}}]}";

const IMPORT_SCHEMA: &str = r#"{"type":"object","properties":{"to":{"type":"string","description":"Provider id that holds the paths."},"materials":{"type":"array","items":{"type":"object","properties":{"path":{"type":"string"},"type":{"type":"object","additionalProperties":{"type":"string"}}},"required":["path"],"additionalProperties":false}}},"required":["to","materials"],"additionalProperties":false}"#;

const PROPERTY_ADD_SUMMARY: &str = "Add a key to the market's property vocabulary, or extend one; the vocabulary decides which `type` keys and values `offer`, `prep` and `import` accept. \
Check `properties` first: reuse a key before adding a near twin. `values` is the allowed list (omit it to accept any value); naming values on an existing key adds them to its list. Values are lower-cased. Example: \
{\"name\":\"organism\",\"description\":\"source organism of a reference set\",\"values\":[\"human\",\"mouse\"]}";

const PROPERTY_ADD_SCHEMA: &str = r#"{"type":"object","properties":{"name":{"type":"string","description":"The property key."},"description":{"type":"string","description":"What the key means, so the next agent reuses it."},"values":{"type":"array","items":{"type":"string"},"description":"Allowed values; omit to accept any."}},"required":["name","description"],"additionalProperties":false}"#;

const PROPERTIES_SUMMARY: &str = "List the property vocabulary for typed materials and outputs: each key with its description and, when it has one, its allowed values. Read it before typing a material. Example reply: \
{\"properties\":{\"db\":{\"description\":\"reference database name\"},\"format\":{\"description\":\"file format\",\"values\":[\"fasta\",\"tsv\"]}}}";

/// The single catalog the CLI, MCP and HTTP surfaces are generated from. Provider verbs are absent.
pub const CALLER_VERBS: &[VerbSpec] = &[
    VerbSpec {
        name: "offer",
        summary: OFFER_SUMMARY,
        cli_only: false,
        schema: OFFER_SCHEMA,
    },
    VerbSpec {
        name: "prep",
        summary: PREP_SUMMARY,
        cli_only: false,
        schema: PREP_SCHEMA,
    },
    VerbSpec {
        name: "import",
        summary: IMPORT_SUMMARY,
        cli_only: false,
        schema: IMPORT_SCHEMA,
    },
    VerbSpec {
        name: "property_add",
        summary: PROPERTY_ADD_SUMMARY,
        cli_only: false,
        schema: PROPERTY_ADD_SCHEMA,
    },
    VerbSpec {
        name: "properties",
        summary: PROPERTIES_SUMMARY,
        cli_only: false,
        schema: r#"{"type":"object","properties":{},"additionalProperties":false}"#,
    },
    VerbSpec {
        name: "status",
        summary: "A promise's phase; once settled, its facts, true sizes and run stats (`stats`: max_rss_kb, cpu_s user+sys, wall_s of the protocol; a field is absent when the backend could not measure it, and a reused result has none).",
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
