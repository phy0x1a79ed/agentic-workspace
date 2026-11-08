//! JSON as Python's `json` module reads and writes the land state: objects keep their key
//! order, and [`dumps`] writes `json.dumps(v, indent=1)` byte for byte (ASCII only, `\uXXXX`
//! escapes). A ticket, batch or lease either side writes reads the same on the other.

use std::fmt::Write as _;

use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};

#[derive(Debug, Clone, PartialEq)]
pub enum J {
    Null,
    Bool(bool),
    Num(serde_json::Number),
    Str(String),
    Arr(Vec<J>),
    Obj(Vec<(String, J)>),
}

impl J {
    pub fn obj() -> J {
        J::Obj(vec![])
    }

    pub fn get(&self, key: &str) -> Option<&J> {
        match self {
            J::Obj(m) => m.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    pub fn get_mut(&mut self, key: &str) -> Option<&mut J> {
        match self {
            J::Obj(m) => m.iter_mut().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    /// `d[key] = v`: in place when the key exists, else appended, as a Python dict does.
    pub fn set(&mut self, key: &str, v: impl Into<J>) {
        let v = v.into();
        if let J::Obj(m) = self {
            match m.iter_mut().find(|(k, _)| k == key) {
                Some(slot) => slot.1 = v,
                None => m.push((key.to_string(), v)),
            }
        }
    }

    /// `d.pop(key, None)`.
    pub fn remove(&mut self, key: &str) -> Option<J> {
        match self {
            J::Obj(m) => m.iter().position(|(k, _)| k == key).map(|i| m.remove(i).1),
            _ => None,
        }
    }

    pub fn str(&self, key: &str) -> &str {
        match self.get(key) {
            Some(J::Str(s)) => s,
            _ => "",
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            J::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_f64(&self) -> Option<f64> {
        match self {
            J::Num(n) => n.as_f64(),
            _ => None,
        }
    }

    pub fn as_arr(&self) -> &[J] {
        match self {
            J::Arr(a) => a,
            _ => &[],
        }
    }

    pub fn as_arr_mut(&mut self) -> Option<&mut Vec<J>> {
        match self {
            J::Arr(a) => Some(a),
            _ => None,
        }
    }

    /// Python's truthiness.
    pub fn truthy(&self) -> bool {
        match self {
            J::Null => false,
            J::Bool(b) => *b,
            J::Num(n) => n.as_f64() != Some(0.0),
            J::Str(s) => !s.is_empty(),
            J::Arr(a) => !a.is_empty(),
            J::Obj(m) => !m.is_empty(),
        }
    }
}

impl From<&str> for J {
    fn from(s: &str) -> J {
        J::Str(s.to_string())
    }
}
impl From<String> for J {
    fn from(s: String) -> J {
        J::Str(s)
    }
}
impl From<bool> for J {
    fn from(b: bool) -> J {
        J::Bool(b)
    }
}
impl From<f64> for J {
    fn from(f: f64) -> J {
        serde_json::Number::from_f64(f).map(J::Num).unwrap_or(J::Null)
    }
}
impl From<i64> for J {
    fn from(i: i64) -> J {
        J::Num(i.into())
    }
}
impl From<Vec<J>> for J {
    fn from(a: Vec<J>) -> J {
        J::Arr(a)
    }
}
impl<const N: usize> From<[(&str, J); N]> for J {
    fn from(kv: [(&str, J); N]) -> J {
        J::Obj(kv.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
    }
}

impl<'de> Deserialize<'de> for J {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<J, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = J;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("JSON")
            }
            fn visit_unit<E>(self) -> Result<J, E> {
                Ok(J::Null)
            }
            fn visit_none<E>(self) -> Result<J, E> {
                Ok(J::Null)
            }
            fn visit_bool<E>(self, b: bool) -> Result<J, E> {
                Ok(J::Bool(b))
            }
            fn visit_i64<E>(self, i: i64) -> Result<J, E> {
                Ok(J::Num(i.into()))
            }
            fn visit_u64<E>(self, u: u64) -> Result<J, E> {
                Ok(J::Num(u.into()))
            }
            fn visit_f64<E: de::Error>(self, f: f64) -> Result<J, E> {
                serde_json::Number::from_f64(f).map(J::Num).ok_or_else(|| E::custom("not a finite number"))
            }
            fn visit_str<E>(self, s: &str) -> Result<J, E> {
                Ok(J::Str(s.to_string()))
            }
            fn visit_string<E>(self, s: String) -> Result<J, E> {
                Ok(J::Str(s))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut a: A) -> Result<J, A::Error> {
                let mut v = vec![];
                while let Some(x) = a.next_element()? {
                    v.push(x);
                }
                Ok(J::Arr(v))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut a: A) -> Result<J, A::Error> {
                let mut m: Vec<(String, J)> = vec![];
                while let Some((k, v)) = a.next_entry::<String, J>()? {
                    // A repeated key keeps its first place and its last value, as in Python.
                    match m.iter_mut().find(|(x, _)| *x == k) {
                        Some(slot) => slot.1 = v,
                        None => m.push((k, v)),
                    }
                }
                Ok(J::Obj(m))
            }
        }
        d.deserialize_any(V)
    }
}

pub fn loads(text: &str) -> serde_json::Result<J> {
    serde_json::from_str(text)
}

fn string(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut units = [0u16; 2];
                for u in c.encode_utf16(&mut units) {
                    let _ = write!(out, "\\u{u:04x}");
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn number(out: &mut String, n: &serde_json::Number) {
    if n.is_f64() {
        let f = n.as_f64().unwrap_or(0.0);
        // Python's repr: shortest round trip, `.0` on a whole number, `e+XX` past 1e16.
        if f.fract() == 0.0 && f.abs() < 1e16 {
            let _ = write!(out, "{f:.1}");
        } else if f.abs() >= 1e16 || (f != 0.0 && f.abs() < 1e-4) {
            let s = format!("{f:e}");
            let (m, e) = s.split_once('e').unwrap_or((&s, "0"));
            let e: i32 = e.parse().unwrap_or(0);
            let _ = write!(out, "{m}e{}{:02}", if e < 0 { '-' } else { '+' }, e.abs());
        } else {
            let _ = write!(out, "{f}");
        }
    } else {
        let _ = write!(out, "{n}");
    }
}

fn dump(out: &mut String, v: &J, level: usize) {
    let pad = |out: &mut String, n: usize| out.extend(std::iter::repeat_n(' ', n));
    match v {
        J::Null => out.push_str("null"),
        J::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        J::Num(n) => number(out, n),
        J::Str(s) => string(out, s),
        J::Arr(a) if a.is_empty() => out.push_str("[]"),
        J::Obj(m) if m.is_empty() => out.push_str("{}"),
        J::Arr(a) => {
            out.push_str("[\n");
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push_str(",\n");
                }
                pad(out, level + 1);
                dump(out, x, level + 1);
            }
            out.push('\n');
            pad(out, level);
            out.push(']');
        }
        J::Obj(m) => {
            out.push_str("{\n");
            for (i, (k, x)) in m.iter().enumerate() {
                if i > 0 {
                    out.push_str(",\n");
                }
                pad(out, level + 1);
                string(out, k);
                out.push_str(": ");
                dump(out, x, level + 1);
            }
            out.push('\n');
            pad(out, level);
            out.push('}');
        }
    }
}

/// `json.dumps(v, indent=1)`.
pub fn dumps(v: &J) -> String {
    let mut out = String::new();
    dump(&mut out, v, 0);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_what_python_writes() {
        // python3 -c 'import json; print(json.dumps({...}, indent=1))'
        let text = r#"{"id": "abc", "brief": "fix → now 😀\n\t\"q\"", "restart": [], "n": {}, "until": 1794094602.9187448, "t": 1800.0, "k": 3, "steps": {"pins": true, "check": false}, "x": null, "nodes": [{"node": "mizar", "ok": true}]}"#;
        let want = "{\n \"id\": \"abc\",\n \"brief\": \"fix \\u2192 now \\ud83d\\ude00\\n\\t\\\"q\\\"\",\n \"restart\": [],\n \"n\": {},\n \"until\": 1794094602.9187448,\n \"t\": 1800.0,\n \"k\": 3,\n \"steps\": {\n  \"pins\": true,\n  \"check\": false\n },\n \"x\": null,\n \"nodes\": [\n  {\n   \"node\": \"mizar\",\n   \"ok\": true\n  }\n ]\n}";
        assert_eq!(dumps(&loads(text).unwrap()), want);
    }

    #[test]
    fn keeps_key_order_on_update() {
        let mut t = loads(r#"{"b": 1, "a": 2}"#).unwrap();
        t.set("b", "x");
        t.set("c", true);
        assert_eq!(dumps(&t), "{\n \"b\": \"x\",\n \"a\": 2,\n \"c\": true\n}");
        assert!(t.remove("a").is_some() && t.get("a").is_none());
    }
}
