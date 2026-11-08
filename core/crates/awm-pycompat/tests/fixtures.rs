//! Each function against the Python 3.12 output that tests/capture.py recorded.

use awm_pycompat::{difflib, json, pystr, re, repr, time};
use serde_json::Value;

fn load(name: &str) -> Value {
    let path = format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR"));
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn strs(v: &Value) -> Vec<&str> {
    v.as_array().unwrap().iter().map(|x| x.as_str().unwrap()).collect()
}

fn check<T: PartialEq + std::fmt::Debug>(failures: &mut Vec<String>, what: &str, input: &impl std::fmt::Debug, got: T, want: T) {
    if got != want {
        failures.push(format!("{what}({input:?}): got {got:?}, want {want:?}"));
    }
}

fn finish(failures: Vec<String>) {
    if !failures.is_empty() {
        let shown: Vec<_> = failures.iter().take(20).collect();
        panic!("{} mismatches:\n{}", failures.len(), shown.iter().map(|s| s.as_str()).collect::<Vec<_>>().join("\n"));
    }
}

#[test]
fn strings() {
    let mut f = Vec::new();
    for case in load("strings.json").as_array().unwrap() {
        let s = case["s"].as_str().unwrap();
        check(&mut f, "len", &s, pystr::len(s), case["len"].as_u64().unwrap() as usize);
        check(&mut f, "split", &s, pystr::split(s), strs(&case["split"]));
        check(&mut f, "strip", &s, pystr::strip(s), case["strip"].as_str().unwrap());
        check(&mut f, "lstrip", &s, pystr::lstrip(s), case["lstrip"].as_str().unwrap());
        check(&mut f, "rstrip", &s, pystr::rstrip(s), case["rstrip"].as_str().unwrap());
        check(&mut f, "splitlines", &s, pystr::splitlines(s, false), strs(&case["splitlines"]));
        check(&mut f, "splitlines_keepends", &s, pystr::splitlines(s, true), strs(&case["splitlines_keepends"]));
        check(&mut f, "lower", &s, pystr::lower(s), case["lower"].as_str().unwrap().to_string());
        check(&mut f, "norm", &s, pystr::norm(s), case["norm"].as_str().unwrap().to_string());
        check(&mut f, "isdigit", &s, pystr::isdigit(s), case["isdigit"].as_bool().unwrap());
        check(&mut f, "head200", &s, pystr::head(s, 200), case["head200"].as_str().unwrap());
        check(&mut f, "head3", &s, pystr::head(s, 3), case["head3"].as_str().unwrap());
        check(&mut f, "tokens", &s, re::tokens(s), strs(&case["tokens"]));
        let low = pystr::lower(s);
        check(&mut f, "words_lower", &s, re::words(&low), strs(&case["words_lower"]));
        let squash = re::sub_nonword(&low, " ");
        check(&mut f, "nonword_squash", &s, pystr::strip(&squash), case["nonword_squash"].as_str().unwrap());
        let sentences: Vec<&str> = re::sentence_split(s).into_iter().filter(|p| !pystr::strip(p).is_empty()).collect();
        check(&mut f, "sentences", &s, sentences, strs(&case["sentences"]));
        check(&mut f, "ascii_fold", &s, pystr::ascii_fold(s), case["ascii_fold"].as_str().unwrap().to_string());
    }
    finish(f);
}

#[test]
fn datetimes() {
    let mut f = Vec::new();
    let d = load("datetime.json");
    for case in d["ms_to_iso"].as_array().unwrap() {
        let ms = case["ms"].as_i64().unwrap();
        check(&mut f, "ms_to_iso", &ms, time::ms_to_iso(ms), Some(case["iso"].as_str().unwrap().to_string()));
    }
    for case in d["ms_date"].as_array().unwrap() {
        let ms = case["ms"].as_i64().unwrap();
        check(&mut f, "ms_to_date", &ms, time::ms_to_date(ms), Some(case["date"].as_str().unwrap().to_string()));
    }
    for case in d["parse"].as_array().unwrap() {
        let s = case["s"].as_str().unwrap();
        check(&mut f, "iso_to_ms", &s, time::iso_to_ms(s), case["iso_to_ms"].as_i64());
        let want = case["fromisoformat"].as_str().map(str::to_string);
        check(&mut f, "fromisoformat", &s, time::fromisoformat(s).map(|dt| dt.isoformat()), want);
    }
    for case in d["aware"].as_array().unwrap() {
        let us = case["us"].as_i64().unwrap();
        let dt = time::DateTime::from_utc_us(us);
        check(&mut f, "isoformat", &us, dt.isoformat(), case["iso"].as_str().unwrap().to_string());
        check(&mut f, "zulu", &us, time::zulu(&dt), case["zulu"].as_str().unwrap().to_string());
        check(&mut f, "date", &us, time::date_iso(&dt), case["date"].as_str().unwrap().to_string());
        check(&mut f, "timestamp_ms", &us, time::timestamp_ms(us), case["ms"].as_i64().unwrap());
        let back = time::fromisoformat(case["iso"].as_str().unwrap()).map(|p| p.utc_us());
        check(&mut f, "round trip", &us, back, Some(us));
    }
    finish(f);
}

#[test]
fn json_dumps() {
    let mut f = Vec::new();
    for case in load("json.json").as_array().unwrap() {
        let input = case["input"].as_str().unwrap();
        let v: Value = serde_json::from_str(input).unwrap();
        let opts = |sort_keys, indent, ensure_ascii| json::Dumps { sort_keys, indent, ensure_ascii };
        check(&mut f, "dumps", &input, json::dumps(&v), case["dumps"].as_str().unwrap().to_string());
        check(&mut f, "sort_keys", &input, json::dumps_with(&v, opts(true, None, true)), case["sort_keys"].as_str().unwrap().to_string());
        check(&mut f, "indent1", &input, json::dumps_with(&v, opts(false, Some(1), true)), case["indent1"].as_str().unwrap().to_string());
        check(&mut f, "unicode", &input, json::dumps_with(&v, opts(false, None, false)), case["unicode"].as_str().unwrap().to_string());
    }
    finish(f);
}

fn untag(v: &Value) -> repr::Py {
    use repr::Py;
    match v {
        Value::Array(items) => Py::List(items.iter().map(untag).collect()),
        Value::Object(m) if m.contains_key("$tuple") => Py::Tuple(m["$tuple"].as_array().unwrap().iter().map(untag).collect()),
        Value::Object(m) if m.contains_key("$dict") => Py::Dict(
            m["$dict"].as_array().unwrap().iter().map(|kv| (untag(&kv[0]), untag(&kv[1]))).collect(),
        ),
        Value::Object(m) if m.contains_key("$float") => Py::Float(match m["$float"].as_str().unwrap() {
            "nan" => f64::NAN,
            "inf" => f64::INFINITY,
            _ => f64::NEG_INFINITY,
        }),
        Value::Object(m) if m.contains_key("$int") => Py::Int(m["$int"].as_str().unwrap().parse().unwrap()),
        other => Py::from(other),
    }
}

#[test]
fn reprs() {
    let mut f = Vec::new();
    for case in load("repr.json").as_array().unwrap() {
        let py = untag(&case["value"]);
        check(&mut f, "repr", &case["value"], py.repr(), case["repr"].as_str().unwrap().to_string());
    }
    finish(f);
}

#[test]
fn difflib_ratio_and_diff() {
    let mut f = Vec::new();
    let d = load("difflib.json");
    for case in d["ratio"].as_array().unwrap() {
        let (a, b) = (case["a"].as_str().unwrap(), case["b"].as_str().unwrap());
        let got = repr::float_repr(difflib::ratio(a, b));
        check(&mut f, "ratio", &(a, b), got, case["ratio"].as_str().unwrap().to_string());
        if let Some(want) = case.get("opcodes") {
            let ac: Vec<char> = a.chars().collect();
            let bc: Vec<char> = b.chars().collect();
            let got: Vec<Value> = difflib::SequenceMatcher::new(&ac, &bc)
                .get_opcodes()
                .iter()
                .map(|o| serde_json::json!([o.tag.as_str(), o.i1, o.i2, o.j1, o.j2]))
                .collect();
            check(&mut f, "opcodes", &(a, b), Value::Array(got), want.clone());
        }
    }
    for case in d["unified_diff"].as_array().unwrap() {
        let (old, new) = (case["old"].as_str().unwrap(), case["new"].as_str().unwrap());
        let got = difflib::unified_diff_text(old, new, case["fromfile"].as_str().unwrap(), case["tofile"].as_str().unwrap());
        check(&mut f, "unified_diff", &case["fromfile"], got, case["diff"].as_str().unwrap().to_string());
    }
    finish(f);
}

#[test]
fn translated_patterns_match_as_python() {
    let words = regex::Regex::new(&re::translate(r"\w+", re::Dollar::Refuse).unwrap()).unwrap();
    let tokens = regex::Regex::new(&re::translate(r"\w+|[^\w\s]", re::Dollar::Refuse).unwrap()).unwrap();
    let heading = re::translate(r"^(#{1,6}\s+.+|\*\*[^*\n]{2,80}\*\*:?\s*$)", re::Dollar::EndOfText).unwrap();
    regex::Regex::new(&heading).unwrap();
    let mut f = Vec::new();
    for case in load("strings.json").as_array().unwrap() {
        let s = case["s"].as_str().unwrap();
        let low = pystr::lower(s);
        let got: Vec<&str> = words.find_iter(&low).map(|m| m.as_str()).collect();
        check(&mut f, "regex words", &s, got, strs(&case["words_lower"]));
        let got: Vec<&str> = tokens.find_iter(s).map(|m| m.as_str()).collect();
        check(&mut f, "regex tokens", &s, got, strs(&case["tokens"]));
    }
    for refused in [r"\bword", r"a(?=b)", r"(?<=x)y", r"(a)\1", "end$"] {
        assert!(re::translate(refused, re::Dollar::Refuse).is_err(), "{refused}");
    }
    finish(f);
}
