//! Lenient readers of verb arguments: the CLI passes every value as a string.

use serde_json::Value;

/// `true`, `"true"`, `"True"`, `"1"` and `1` are true. A missing or null value is `default`.
pub fn bool_lenient(v: Option<&Value>, default: bool) -> bool {
    match v {
        None | Some(Value::Null) => default,
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => matches!(s.as_str(), "true" | "True" | "1"),
        Some(Value::Number(n)) => n.as_i64() == Some(1),
        _ => false,
    }
}

/// An integer, or an integer-valued string. Anything else is `default`.
pub fn int_lenient(v: Option<&Value>, default: i64) -> i64 {
    match v {
        Some(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)).unwrap_or(default),
        Some(Value::String(s)) => s.trim().parse().unwrap_or(default),
        Some(Value::Bool(b)) => *b as i64,
        _ => default,
    }
}

/// A non-empty trimmed string.
pub fn text(v: Option<&Value>) -> Option<String> {
    let s = match v? {
        Value::String(s) => s.trim().to_string(),
        Value::Number(n) => n.to_string(),
        _ => return None,
    };
    (!s.is_empty()).then_some(s)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn bools_and_ints_accept_cli_strings() {
        for t in [json!(true), json!("true"), json!("True"), json!("1"), json!(1)] {
            assert!(bool_lenient(Some(&t), false));
        }
        for f in [json!(false), json!("false"), json!("0"), json!(0), json!("yes")] {
            assert!(!bool_lenient(Some(&f), true));
        }
        assert!(bool_lenient(None, true));
        assert!(bool_lenient(Some(&Value::Null), true));


        assert_eq!(int_lenient(Some(&json!("42")), 0), 42);
        assert_eq!(int_lenient(Some(&json!(7)), 0), 7);
        assert_eq!(int_lenient(Some(&json!("x")), 3), 3);
        assert_eq!(int_lenient(None, 5), 5);
    }
}
