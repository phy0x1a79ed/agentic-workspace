//! The device-token and session-cookie codecs, against the vectors the Python wrote
//! (tests/vectors/make_tokens.py), and the codec tests of awm.config.usertoken.

use awm_plugin_auth::tokens::{self, Shape};
use base64::Engine;
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("../vectors/tokens.json")).expect("tokens.json")
}

fn s(v: &Value) -> &str {
    v.as_str().expect("a string")
}

// -- the Python tests --------------------------------------------------------------------------

#[test]
fn test_a_minted_token_has_the_pinned_shape() {
    let t = tokens::mint();
    assert_eq!(t.token, format!("awmut1.{}.{}", t.id, t.secret));
    assert_eq!(t.id.len(), 16);
    assert!(u64::from_str_radix(&t.id, 16).is_ok());
    assert!(!t.secret.contains('='));
    assert_eq!(base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(&t.secret).unwrap().len(), 32);
    assert_eq!(tokens::parse(&t.token), Ok((t.id.as_str(), t.secret.as_str())));
    assert_ne!(t, tokens::mint());
}

// -- the scope shapes (httpsfront test_scoped_device_token) -------------------------------------

// -- the vectors -------------------------------------------------------------------------------

#[test]
fn the_scopes_are_pythons() {
    let v = vectors();
    let want: Vec<&str> = v["device"]["scopes"].as_array().unwrap().iter().map(s).collect();
    assert_eq!(tokens::SCOPES.to_vec(), want);
}

#[test]
fn a_device_token_is_minted_byte_for_byte() {
    let v = vectors();
    for m in v["device"]["mint"].as_array().unwrap() {
        let id: [u8; 8] = hex::decode(s(&m["id_bytes"])).unwrap().try_into().unwrap();
        let secret: [u8; 32] = hex::decode(s(&m["secret_bytes"])).unwrap().try_into().unwrap();
        let t = tokens::mint_with(id, secret);
        assert_eq!((t.id.as_str(), t.secret.as_str(), t.token.as_str()), (s(&m["id"]), s(&m["secret"]), s(&m["token"])));
        assert_eq!(tokens::digest(&t.secret), s(&m["digest"]));
    }
}

#[test]
fn a_device_token_parses_as_python_parses_it() {
    let v = vectors();
    for p in v["device"]["parse"].as_array().unwrap() {
        let token = s(&p["token"]);
        assert_eq!(tokens::looks_like_token(token), p["looks"].as_bool().unwrap(), "{token:?}");
        let got = tokens::parse(token).ok().map(|(a, b)| vec![a.to_string(), b.to_string()]);
        let want = p["parsed"].as_array().map(|a| a.iter().map(|x| s(x).to_string()).collect::<Vec<_>>());
        assert_eq!(got, want, "{token:?}");
    }
}

#[test]
fn a_digest_matches_as_python_matches_it() {
    let v = vectors();
    for m in v["device"]["matches"].as_array().unwrap() {
        assert_eq!(tokens::matches(s(&m["secret"]), s(&m["stored"])), m["ok"].as_bool().unwrap(), "{m}");
    }
}

#[test]
fn the_scope_matrix_is_pythons() {
    let v = vectors();
    let cases = v["scope_admits"].as_array().unwrap();
    assert!(cases.len() > 1000);
    for c in cases {
        let raw = hex::decode(s(&c["raw"])).unwrap();
        let query = hex::decode(s(&c["query"])).unwrap();
        let sh = Shape { method: s(&c["method"]), path: s(&c["path"]), raw: &raw, query: &query, websocket: c["websocket"].as_bool().unwrap() };
        assert_eq!(tokens::scope_admits(s(&c["scope"]), &sh), c["ok"].as_bool().unwrap(), "{c}");
    }
}

#[test]
fn a_session_token_is_signed_byte_for_byte() {
    let v = vectors();
    for m in v["session"]["mint"].as_array().unwrap() {
        let got = tokens::session_mint(s(&m["secret"]), s(&m["sub"]), m["ttl"].as_f64().unwrap(), m["sat"].as_i64(), m["now"].as_f64().unwrap());
        assert_eq!(got, s(&m["token"]), "{m}");
    }
}

#[test]
fn a_session_token_verifies_as_python_verifies_it() {
    let v = vectors();
    for c in v["session"]["verify"].as_array().unwrap() {
        let got = tokens::session_verify(s(&c["secret"]), s(&c["token"]), c["now"].as_f64().unwrap()).map(Value::Object);
        let want = if c["claims"].is_null() { None } else { Some(c["claims"].clone()) };
        assert_eq!(got, want, "{c}");
    }
}

#[test]
fn every_minted_session_verifies_until_its_exp() {
    let v = vectors();
    for m in v["session"]["mint"].as_array().unwrap() {
        let (now, ttl) = (m["now"].as_f64().unwrap(), m["ttl"].as_f64().unwrap());
        let exp = (now + ttl).trunc();
        let token = s(&m["token"]);
        let claims = tokens::session_verify(s(&m["secret"]), token, exp - 0.5);
        assert_eq!(claims.is_some(), exp - 0.5 < exp);
        assert!(tokens::session_verify(s(&m["secret"]), token, exp).is_none());
        if let Some(c) = claims {
            assert_eq!(tokens::session_sub(&c), if s(&m["sub"]).is_empty() { "operator" } else { s(&m["sub"]) });
        }
    }
}
