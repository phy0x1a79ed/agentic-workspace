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

    const PLACEHOLDER: &str = "This message contains interactive elements.";

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

    #[test]
    fn no_blocks_no_attachments_is_the_text() {
        assert_eq!(slack_text(&json!({"text": "plain"})), "plain");
        assert_eq!(slack_text(&json!({})), "");
    }

    #[test]
    fn placeholder_is_replaced_by_rich_text() {
        let m = json!({"text": PLACEHOLDER, "blocks": [rich(vec![section(vec![
            txt("see "), json!({"type": "link", "url": "https://x.org/a", "text": "the doc"}),
            txt(" cc "), json!({"type": "user", "user_id": "U1"}),
            txt(" in "), json!({"type": "channel", "channel_id": "C1"}),
            txt(" "), json!({"type": "emoji", "name": "tada"}),
            txt(" "), json!({"type": "broadcast", "range": "here"}),
        ])])]});
        assert_eq!(slack_text(&m), "see the doc (https://x.org/a) cc <@U1> in <#C1> :tada: @here");
    }

    #[test]
    fn a_bare_link_is_its_url() {
        let m = json!({"text": "", "blocks": [rich(vec![section(vec![json!({"type": "link", "url": "https://x.org"})])])]});
        assert_eq!(slack_text(&m), "https://x.org");
    }

    #[test]
    fn lists_preformatted_and_quotes() {
        let m = json!({"text": PLACEHOLDER, "blocks": [rich(vec![
            section(vec![txt("steps:")]),
            json!({"type": "rich_text_list", "style": "ordered", "elements": [section(vec![txt("one")]), section(vec![txt("two")])]}),
            json!({"type": "rich_text_list", "style": "bullet", "indent": 1, "elements": [section(vec![txt("nested")])]}),
            json!({"type": "rich_text_preformatted", "elements": [txt("a,b\n1,2")]}),
            json!({"type": "rich_text_quote", "elements": [txt("q1\nq2")]}),
        ])]});
        assert_eq!(slack_text(&m), ["steps:", "1. one", "2. two", "  • nested", "```\na,b\n1,2\n```", "> q1", "> q2"].join("\n"));
    }

    #[test]
    fn table_block_cells_raw_and_rich() {
        let table = json!({"type": "table", "rows": [
            [{"type": "raw_text", "text": "name"}, {"type": "raw_text", "text": "n"}],
            [rich(vec![section(vec![txt("ab")])]), {"type": "raw_text", "text": "3"}],
        ]});
        assert_eq!(slack_text(&json!({"text": PLACEHOLDER, "blocks": [table]})), "name | n\nab | 3");
    }

    #[test]
    fn table_is_appended_to_real_text() {
        let table = json!({"type": "table", "rows": [[{"type": "raw_text", "text": "k"}]]});
        assert_eq!(slack_text(&json!({"text": "results:", "blocks": [table]})), "results:\nk");
    }

    #[test]
    fn section_header_context_and_markdown_blocks() {
        let m = json!({"text": "", "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "Report"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*body*"},
             "fields": [{"type": "mrkdwn", "text": "a"}, {"type": "mrkdwn", "text": "b"}]},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "note"}, {"type": "image", "alt_text": "icon"}]},
            {"type": "divider"},
            {"type": "actions", "elements": [{"type": "button"}]},
            {"type": "markdown", "text": "# md"},
        ]});
        assert_eq!(slack_text(&m), "Report\n*body*\na\nb\nnote icon\n# md");
    }

    #[test]
    fn attachments_text_then_fallback() {
        let m = json!({"text": "", "attachments": [
            {"pretext": "pre", "title": "T", "text": "body", "fields": [{"title": "k", "value": "v"}], "fallback": "ignored"},
            {"fallback": "only the fallback"}]});
        assert_eq!(slack_text(&m), "pre\nT\nbody\nk: v\nonly the fallback");
    }

    #[test]
    fn a_placeholder_with_nothing_to_show_stays_the_placeholder() {
        assert_eq!(slack_text(&json!({"text": PLACEHOLDER, "blocks": [{"type": "divider"}]})), PLACEHOLDER);
    }
}
