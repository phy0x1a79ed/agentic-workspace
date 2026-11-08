//! Readable text for a Slack message whose content lives in `blocks` (`slack_blocks.py`).
//!
//! Slack fills `text` with a placeholder ("This message contains interactive elements.") when a
//! message carries a block type it cannot flatten, such as a table or an actions row, and with
//! nothing at all for a bot message built only from legacy `attachments`. [`slack_text`] returns
//! `text` when it is real, and otherwise renders the blocks and attachments in the order Slack
//! shows them.

use serde_json::Value;

const PLACEHOLDERS: [&str; 2] = ["this message contains interactive elements", "this content can't be displayed"];

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}

fn list<'a>(v: &'a Value, k: &str) -> &'a [Value] {
    v.get(k).and_then(Value::as_array).map(Vec::as_slice).unwrap_or(&[])
}

/// Python's `str(x or "")` for a JSON value.
fn truthy_str(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) | Some(Value::Bool(false)) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(true)) => "True".into(),
        Some(Value::Number(n)) if n.as_f64() == Some(0.0) => String::new(),
        Some(Value::Array(a)) if a.is_empty() => String::new(),
        Some(Value::Object(o)) if o.is_empty() => String::new(),
        Some(other) => other.to_string(),
    }
}

fn is_placeholder(text: &str) -> bool {
    let t = text.trim().trim_end_matches('.').to_lowercase();
    t.is_empty() || PLACEHOLDERS.contains(&t.as_str())
}

fn inline(el: &Value) -> String {
    match s(el, "type") {
        "text" => s(el, "text").to_string(),
        "link" => {
            let (url, label) = (s(el, "url"), s(el, "text"));
            if !label.is_empty() && label != url {
                format!("{label} ({url})")
            } else {
                url.to_string()
            }
        }
        "user" => format!("<@{}>", s(el, "user_id")),
        "usergroup" => format!("<!subteam^{}>", s(el, "usergroup_id")),
        "channel" => format!("<#{}>", s(el, "channel_id")),
        "emoji" => format!(":{}:", s(el, "name")),
        "broadcast" => format!("@{}", el.get("range").and_then(Value::as_str).unwrap_or("here")),
        "date" => s(el, "fallback").to_string(),
        "color" => s(el, "value").to_string(),
        _ => s(el, "text").to_string(),
    }
}

fn runs(elements: &[Value]) -> String {
    elements.iter().filter(|e| e.is_object()).map(inline).collect()
}

fn rich(elements: &[Value]) -> Vec<String> {
    let mut lines = vec![];
    for el in elements.iter().filter(|e| e.is_object()) {
        let sub = list(el, "elements");
        match s(el, "type") {
            "rich_text_section" => lines.push(runs(sub)),
            "rich_text_preformatted" => lines.push(format!("```\n{}\n```", runs(sub))),
            "rich_text_quote" => lines.extend(runs(sub).split('\n').map(|ln| format!("> {ln}"))),
            "rich_text_list" => {
                let ordered = s(el, "style") == "ordered";
                let indent = el.get("indent").and_then(Value::as_u64).unwrap_or(0) as usize;
                let pad = "  ".repeat(indent);
                for (n, item) in sub.iter().enumerate() {
                    let mark = if ordered { format!("{}.", n + 1) } else { "•".to_string() };
                    lines.push(format!("{pad}{mark} {}", runs(list(item, "elements"))));
                }
            }
            _ => {}
        }
    }
    lines
}

/// A Slack text object (`{type, text}`) or a bare string.
fn plain(obj: Option<&Value>) -> String {
    match obj {
        Some(o @ Value::Object(_)) => truthy_str(o.get("text")),
        other => truthy_str(other),
    }
}

fn cell(c: &Value) -> String {
    if !c.is_object() {
        return plain(Some(c));
    }
    if s(c, "type") == "rich_text" {
        return rich(list(c, "elements")).join(" ").trim().to_string();
    }
    plain(Some(c))
}

fn table(block: &Value) -> String {
    list(block, "rows")
        .iter()
        .map(|row| row.as_array().map(|cells| cells.iter().map(cell).collect::<Vec<_>>().join(" | ")).unwrap_or_default())
        .collect::<Vec<_>>()
        .join("\n")
}

fn join_nonempty(parts: impl IntoIterator<Item = String>, sep: &str) -> String {
    parts.into_iter().filter(|p| !p.is_empty()).collect::<Vec<_>>().join(sep)
}

fn block(b: &Value) -> String {
    match s(b, "type") {
        "rich_text" => rich(list(b, "elements")).join("\n"),
        "table" => table(b),
        "section" | "header" => {
            let mut parts = vec![plain(b.get("text"))];
            parts.extend(list(b, "fields").iter().map(|f| plain(Some(f))));
            join_nonempty(parts, "\n")
        }
        "markdown" => s(b, "text").to_string(),
        "context" => join_nonempty(
            list(b, "elements").iter().filter(|e| e.is_object()).map(|e| if s(e, "type") != "image" { plain(Some(e)) } else { s(e, "alt_text").to_string() }),
            " ",
        ),
        "image" => Some(plain(b.get("title"))).filter(|t| !t.is_empty()).unwrap_or_else(|| s(b, "alt_text").to_string()),
        _ => String::new(),
    }
}

/// A legacy attachment's text: its `fallback` only when nothing else says more.
fn attachment(a: &Value) -> String {
    let mut parts = vec![s(a, "pretext").to_string(), s(a, "title").to_string(), s(a, "text").to_string()];
    parts.extend(
        list(a, "fields")
            .iter()
            .filter(|f| f.is_object())
            .map(|f| format!("{}: {}", s(f, "title"), s(f, "value")).trim_matches(|c| c == ':' || c == ' ').to_string()),
    );
    parts.extend(list(a, "blocks").iter().filter(|b| b.is_object()).map(block));
    let text = join_nonempty(parts, "\n");
    if text.is_empty() {
        s(a, "fallback").to_string()
    } else {
        text
    }
}

/// The message's readable text. A real `text` wins, with any table block appended, since
/// Slack's `text` never carries a table.
pub fn slack_text(m: &Value) -> String {
    let text = s(m, "text");
    let blocks: Vec<&Value> = list(m, "blocks").iter().filter(|b| b.is_object()).collect();
    if !is_placeholder(text) {
        let mut out = vec![text.to_string()];
        out.extend(blocks.iter().filter(|b| s(b, "type") == "table").map(|b| table(b)).filter(|t| !t.is_empty()));
        return out.join("\n");
    }
    let mut parts: Vec<String> = blocks.iter().map(|b| block(b)).collect();
    parts.extend(list(m, "attachments").iter().filter(|a| a.is_object()).map(attachment));
    let out = join_nonempty(parts, "\n").trim().to_string();
    if out.is_empty() {
        text.to_string()
    } else {
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn rich(elements: Vec<Value>) -> Value {
        json!({"type": "rich_text", "elements": elements})
    }

    fn section(runs: Vec<Value>) -> Value {
        json!({"type": "rich_text_section", "elements": runs})
    }

    fn txt(s: &str) -> Value {
        json!({"type": "text", "text": s})
    }

    #[test]
    fn real_text_wins_over_blocks() {
        let m = json!({"text": "hello *there*", "blocks": [rich(vec![section(vec![txt("hello there")])])]});
        assert_eq!(slack_text(&m), "hello *there*");
    }

}
