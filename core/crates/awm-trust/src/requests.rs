//! Ask Tony for a grant through the approver on alcor (fleet.md R97, R100, R103).
//!
//! Twin of `awm.trustpolicy.requests`. The caller supplies the call to this node's
//! `operations` relay, which carries it to alcor and records the calling session as the
//! requester. A request runs from 15 minutes to 24 hours.

use std::future::Future;

use serde_json::{json, Value};

pub const MIN_MINUTES: u32 = 15;
pub const MAX_MINUTES: u32 = 24 * 60;
/// The approver holds one `approval_wait` at most this long.
pub const WAIT_STEP_S: u64 = 55;

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum RequestError {
    #[error("{0}")]
    Invalid(String),
    /// Tony denied the request, or it expired, was cancelled or had no answer in time.
    #[error("{0}")]
    Denied(String),
    /// The call to the approver failed.
    #[error("{0}")]
    Call(String),
}

/// One grant request.
#[derive(Debug, Clone)]
pub struct Request<'a> {
    pub kind: &'a str,
    pub node: &'a str,
    pub reason: &'a str,
    pub minutes: u32,
    pub payload: Value,
}

impl Request<'_> {
    fn filing(&self) -> Result<Value, RequestError> {
        if self.reason.trim().is_empty() {
            return Err(RequestError::Invalid("a request needs a reason Tony can read".into()));
        }
        if !(MIN_MINUTES..=MAX_MINUTES).contains(&self.minutes) {
            return Err(RequestError::Invalid(format!("a grant lasts {MIN_MINUTES} to {MAX_MINUTES} minutes, not {}", self.minutes)));
        }
        let payload = if self.payload.is_null() { json!({}) } else { self.payload.clone() };
        Ok(json!({"kind": self.kind, "node": self.node, "reason": self.reason, "minutes": self.minutes, "payload": payload}))
    }
}

/// File `req` without waiting for the answer. Returns its `request_id`.
pub async fn file_request<F, Fut>(req: &Request<'_>, mut call: F) -> Result<String, RequestError>
where
    F: FnMut(&'static str, Value) -> Fut,
    Fut: Future<Output = Result<Value, String>>,
{
    let filed = call("approval_request", req.filing()?).await.map_err(RequestError::Call)?;
    filed
        .get("request_id")
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| RequestError::Call("the approver returned no request_id".into()))
}

/// File `req` and wait up to `wait_s` for Tony. Returns the approver's final record.
/// With no answer in time, the request is cancelled.
pub async fn request<F, Fut>(req: &Request<'_>, mut wait_s: u64, mut call: F) -> Result<Value, RequestError>
where
    F: FnMut(&'static str, Value) -> Fut,
    Fut: Future<Output = Result<Value, String>>,
{
    let rid = file_request(req, &mut call).await?;
    let (kind, node) = (req.kind, req.node);
    loop {
        let step = wait_s.min(WAIT_STEP_S);
        let rec = call("approval_wait", json!({"request_id": rid, "wait_s": step})).await.map_err(RequestError::Call)?;
        let state = rec.get("status").or_else(|| rec.get("state")).and_then(Value::as_str).unwrap_or("");
        match state {
            "approved" => return Ok(rec),
            "denied" | "expired" | "cancelled" => return Err(RequestError::Denied(format!("request {rid} for {kind} on {node} was {state}"))),
            _ => {}
        }
        wait_s -= step;
        if wait_s == 0 {
            call("approval_cancel", json!({"request_id": rid})).await.map_err(RequestError::Call)?;
            return Err(RequestError::Denied(format!("request {rid} for {kind} on {node} had no answer in time")));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    struct Fake {
        states: RefCell<Vec<&'static str>>,
        calls: RefCell<Vec<(&'static str, Value)>>,
    }

    impl Fake {
        fn new(states: &[&'static str]) -> Self {
            Fake { states: RefCell::new(states.iter().rev().copied().collect()), calls: RefCell::default() }
        }

        async fn call(&self, verb: &'static str, args: Value) -> Result<Value, String> {
            self.calls.borrow_mut().push((verb, args.clone()));
            Ok(match verb {
                "approval_request" => json!({"request_id": "r1", "task_id": "t1"}),
                "approval_wait" => {
                    assert!(args["wait_s"].as_u64().unwrap() <= WAIT_STEP_S);
                    json!({"status": self.states.borrow_mut().pop().unwrap()})
                }
                _ => json!({}),
            })
        }

        fn verbs(&self) -> Vec<&'static str> {
            self.calls.borrow().iter().map(|(v, _)| *v).collect()
        }
    }

    fn req(minutes: u32, reason: &str) -> Request<'_> {
        Request { kind: "trust_raise", node: "home", reason, minutes, payload: Value::Null }
    }

    fn run<T>(f: impl Future<Output = T>) -> T {
        tokio::runtime::Builder::new_current_thread().build().unwrap().block_on(f)
    }

    #[test]
    fn minutes_and_reason_are_checked() {
        for bad in [5, MAX_MINUTES + 1] {
            let fake = Fake::new(&[]);
            assert!(matches!(run(request(&req(bad, "r"), 60, |v, a| fake.call(v, a))), Err(RequestError::Invalid(_))));
        }
        let fake = Fake::new(&[]);
        let err = run(request(&req(15, " "), 60, |v, a| fake.call(v, a))).unwrap_err();
        assert!(err.to_string().contains("reason"));
        assert!(fake.verbs().is_empty());
    }

    #[test]
    fn waits_through_pending_then_returns() {
        let fake = Fake::new(&["pending", "approved"]);
        let rec = run(request(&req(15, "r"), 3600, |v, a| fake.call(v, a))).unwrap();
        assert_eq!(rec["status"], "approved");
        assert_eq!(fake.verbs(), ["approval_request", "approval_wait", "approval_wait"]);
        assert_eq!(fake.calls.borrow()[0].1["payload"], json!({}));
    }

    #[test]
    fn denied_and_timed_out() {
        let fake = Fake::new(&["denied"]);
        assert!(run(request(&req(15, "r"), 60, |v, a| fake.call(v, a))).unwrap_err().to_string().contains("was denied"));
        let fake = Fake::new(&["pending", "pending"]);
        let err = run(request(&req(15, "r"), 60, |v, a| fake.call(v, a))).unwrap_err();
        assert!(err.to_string().contains("no answer in time"));
        assert_eq!(fake.verbs(), ["approval_request", "approval_wait", "approval_wait", "approval_cancel"]);
    }

    #[test]
    fn file_only() {
        let fake = Fake::new(&[]);
        assert_eq!(run(file_request(&req(30, "r"), |v, a| fake.call(v, a))).unwrap(), "r1");
        assert_eq!(fake.calls.borrow()[0].1["minutes"], 30);
    }
}
