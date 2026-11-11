//! A received message read the way Python's `email` package (compat32) reads it.
//!
//! mail-parser finds the MIME tree. The rest follows `email.message.Message`: the raw header
//! values, `walk()` order, `get_filename()` with its RFC 2231 rules, `get_content_type()`,
//! `get_content_charset()` and `get_payload(decode=True)`. An attachment's `idx` is its ordinal
//! among the file parts in walk order, so it must agree with the Python's for a download by `idx`.

use mail_parser::{Address, Message, MessageParser, PartType};
use serde_json::json;

use awm_chat::{Attachment, Downloaded};

/// One MIME part, with the header values as compat32 returns them.
pub struct Part<'a> {
    headers: Vec<(String, String)>,
    body: Body<'a>,
    default_type: &'static str,
}

enum Body<'a> {
    Bytes(&'a [u8]),
    Parts(Vec<Part<'a>>),
}

/// A parameter value: plain, or RFC 2231 `(charset, language, value)`.
#[derive(Debug, Clone, PartialEq)]
enum Param {
    Plain(String),
    Ext(Option<String>, Option<String>, String),
}

/// What the connector maps onto an `Inbound` (`_row_from_msg`).
#[derive(Debug, Clone, PartialEq)]
pub struct Row {
    pub from_addr: String,
    pub sender_name: String,
    pub message_id: String,
    pub ts: String,
    pub text: String,
    pub attachments: Vec<Attachment>,
}

pub fn parse(raw: &[u8]) -> (Part<'_>, Option<Message<'_>>) {
    match MessageParser::default().parse(raw) {
        Some(msg) => (build(&msg, raw, 0, "text/plain"), Some(msg)),
        None => (Part { headers: vec![], body: Body::Bytes(raw), default_type: "text/plain" }, None),
    }
}

fn build<'a>(msg: &Message<'_>, raw: &'a [u8], id: usize, default_type: &'static str) -> Part<'a> {
    let Some(p) = msg.parts.get(id) else {
        return Part { headers: vec![], body: Body::Bytes(&[]), default_type };
    };
    let headers = p
        .headers
        .iter()
        .map(|h| {
            let v = raw.get(h.offset_start..h.offset_end).unwrap_or_default();
            let v = String::from_utf8_lossy(v);
            (h.name.as_str().to_string(), v.trim_start_matches([' ', '\t']).trim_end_matches(['\r', '\n']).to_string())
        })
        .collect();
    let mut part = Part { headers, body: Body::Bytes(&[]), default_type };
    part.body = match &p.body {
        PartType::Multipart(ids) => {
            let child_default = if part.content_type() == "multipart/digest" { "message/rfc822" } else { "text/plain" };
            Body::Parts(ids.iter().map(|&i| build(msg, raw, i, child_default)).collect())
        }
        PartType::Message(inner) => Body::Parts(vec![build(inner, raw, 0, "text/plain")]),
        _ => Body::Bytes(raw.get(p.offset_body..p.offset_end).unwrap_or_default()),
    };
    part
}

impl<'a> Part<'a> {
    /// The first header of `name`, as `msg.get(name)` returns it.
    pub fn get(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(n, _)| n.eq_ignore_ascii_case(name)).map(|(_, v)| v.as_str())
    }

    fn is_multipart(&self) -> bool {
        matches!(self.body, Body::Parts(_))
    }

    /// The part and every part under it, depth first.
    fn walk(&self) -> Vec<&Part<'a>> {
        let mut out = vec![self];
        if let Body::Parts(ps) = &self.body {
            for p in ps {
                out.extend(p.walk());
            }
        }
        out
    }

    pub fn content_type(&self) -> String {
        let Some(v) = self.get("content-type") else { return self.default_type.to_string() };
        let ctype = v.split(';').next().unwrap_or("").trim().to_lowercase();
        if ctype.matches('/').count() != 1 {
            return "text/plain".into();
        }
        ctype
    }

    fn params(&self, header: &str) -> Option<Vec<(String, Param)>> {
        let value = self.get(header)?;
        let mut raw = vec![];
        for p in parseparam(value) {
            match p.split_once('=') {
                Some((n, v)) => raw.push((n.trim().to_string(), v.trim().to_string())),
                None => raw.push((p.trim().to_string(), String::new())),
            }
        }
        Some(decode_params(raw))
    }

    fn get_param(&self, param: &str, header: &str) -> Option<Param> {
        let params = self.params(header)?;
        params.into_iter().find(|(k, _)| k.to_lowercase() == param.to_lowercase()).map(|(_, v)| match v {
            Param::Plain(s) => Param::Plain(unquote(&s)),
            Param::Ext(c, l, s) => Param::Ext(c, l, unquote(&s)),
        })
    }

    pub fn filename(&self) -> Option<String> {
        let p = self.get_param("filename", "content-disposition").or_else(|| self.get_param("name", "content-type"))?;
        Some(collapse_rfc2231(p).trim().to_string())
    }

    fn content_charset(&self) -> Option<String> {
        let cs = match self.get_param("charset", "content-type")? {
            Param::Plain(s) => s,
            Param::Ext(c, _, v) => {
                let c = c.filter(|c| !c.is_empty()).unwrap_or_else(|| "us-ascii".into());
                decode_bytes(&latin1_bytes(&v), &c, true).unwrap_or(v)
            }
        };
        cs.is_ascii().then(|| cs.to_lowercase())
    }

    /// `get_payload(decode=True)`: None for a multipart, else the bytes with the
    /// Content-Transfer-Encoding undone.
    fn payload(&self) -> Option<Vec<u8>> {
        let Body::Bytes(b) = self.body else { return None };
        let cte = self.get("content-transfer-encoding").unwrap_or("").to_lowercase();
        Some(match cte.as_str() {
            "quoted-printable" => qp_decode(b),
            "base64" => b64_decode(b),
            _ => b.to_vec(),
        })
    }

    fn is_attachment(&self) -> bool {
        if self.filename().is_some_and(|f| !f.is_empty()) {
            return true;
        }
        self.get("content-disposition").unwrap_or("").to_lowercase().starts_with("attachment")
    }

    fn named(&self, ordinal: usize) -> String {
        self.filename().filter(|f| !f.is_empty()).unwrap_or_else(|| format!("attachment-{ordinal}"))
    }

    /// `_body_text`: the first text/plain part with no filename, decoded by its charset.
    pub fn body_text(&self) -> String {
        if self.is_multipart() {
            for part in self.walk() {
                if part.content_type() == "text/plain" && !part.filename().is_some_and(|f| !f.is_empty()) {
                    if let Some(b) = part.payload() {
                        return decode_text(&b, &part.content_charset().unwrap_or_else(|| "utf-8".into()));
                    }
                }
            }
            return String::new();
        }
        let b = self.payload().unwrap_or_default();
        decode_text(&b, &self.content_charset().unwrap_or_else(|| "utf-8".into()))
    }

    /// `_extract_attachments`: none for a message that is not multipart.
    pub fn attachments(&self, uid: &str) -> Vec<Attachment> {
        if !self.is_multipart() {
            return vec![];
        }
        self.walk()
            .into_iter()
            .filter(|p| p.is_attachment())
            .enumerate()
            .map(|(ordinal, p)| Attachment {
                idx: ordinal as i64,
                filename: p.named(ordinal),
                mime: p.content_type(),
                size: p.payload().map_or(0, |b| b.len() as i64),
                url: String::new(),
                r#ref: json!({"uid": uid, "ord": ordinal}),
            })
            .collect()
    }

    /// `_attachment_bytes`: every file part, or only ordinal `idx`.
    pub fn attachment_bytes(&self, idx: Option<i64>) -> Vec<Downloaded> {
        self.walk()
            .into_iter()
            .filter(|p| p.is_attachment())
            .enumerate()
            .filter(|(ordinal, _)| idx.is_none_or(|i| i == *ordinal as i64))
            .map(|(ordinal, p)| Downloaded { filename: p.named(ordinal), mime: p.content_type(), data: p.payload().unwrap_or_default() })
            .collect()
    }
}

/// `_row_from_msg` for one raw message fetched at `uid`.
pub fn row(raw: &[u8], uid: &str) -> Row {
    let (part, msg) = parse(raw);
    let from_addr = msg.as_ref().and_then(|m| m.from()).map(single_address).unwrap_or_default();
    Row {
        from_addr,
        sender_name: part.get("from").unwrap_or("").to_string(),
        message_id: part.get("message-id").filter(|m| !m.is_empty()).unwrap_or(uid).to_string(),
        ts: part.get("date").unwrap_or("").to_string(),
        text: part.body_text(),
        attachments: part.attachments(uid),
    }
}

/// `parseaddr(...)[1]`: the address when the header holds exactly one, else "".
fn single_address(a: &Address<'_>) -> String {
    let addrs: Vec<_> = match a {
        Address::List(l) => l.iter().collect(),
        Address::Group(g) => g.iter().flat_map(|g| g.addresses.iter()).collect(),
    };
    match addrs.as_slice() {
        [one] => one.address.as_deref().unwrap_or("").to_string(),
        _ => String::new(),
    }
}

/// `email.message._parseparam`: split on `;` outside quotes, lowercasing each name.
fn parseparam(value: &str) -> Vec<String> {
    let mut s = format!(";{value}");
    let mut out = vec![];
    while s.starts_with(';') {
        s.remove(0);
        let mut end = s.find(';').map_or(-1, |e| e as isize);
        while end > 0 && {
            let head = &s[..end as usize];
            (head.matches('"').count() - head.matches("\\\"").count()) % 2 == 1
        } {
            end = s[end as usize + 1..].find(';').map_or(-1, |e| (e + end as usize + 1) as isize);
        }
        let end = if end < 0 { s.len() } else { end as usize };
        let mut f = s[..end].to_string();
        if let Some(i) = f.find('=') {
            f = format!("{}={}", f[..i].trim().to_lowercase(), f[i + 1..].trim());
        }
        out.push(f.trim().to_string());
        s = s[end..].to_string();
    }
    out
}

/// One RFC 2231 piece: its number, its text and whether it was `*`-encoded.
type Continuation = (i64, String, bool);

/// `email.utils.decode_params`: join RFC 2231 continuations, after the plain parameters.
fn decode_params(params: Vec<(String, String)>) -> Vec<(String, Param)> {
    let mut it = params.into_iter();
    let mut out = vec![];
    if let Some((n, v)) = it.next() {
        out.push((n, Param::Plain(v)));
    }
    let mut ext: Vec<(String, Vec<Continuation>)> = vec![];
    for (name, value) in it {
        let encoded = name.ends_with('*');
        let value = unquote(&value);
        match rfc2231_continuation(&name) {
            Some((base, num)) => {
                let entry = match ext.iter_mut().position(|(n, _)| *n == base) {
                    Some(i) => &mut ext[i].1,
                    None => {
                        ext.push((base, vec![]));
                        &mut ext.last_mut().unwrap().1
                    }
                };
                entry.push((num.unwrap_or(-1), value, encoded));
            }
            None => out.push((name, Param::Plain(format!("\"{}\"", quote(&value))))),
        }
    }
    for (name, mut parts) in ext {
        parts.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.cmp(&b.1)).then_with(|| a.2.cmp(&b.2)));
        let mut extended = false;
        let mut joined = String::new();
        for (_, s, encoded) in parts {
            if encoded {
                joined.push_str(&percent_decode_latin1(&s));
                extended = true;
            } else {
                joined.push_str(&s);
            }
        }
        let value = quote(&joined);
        if extended {
            let mut split = value.splitn(3, '\'');
            let (a, b, c) = (split.next(), split.next(), split.next());
            let p = match (a, b, c) {
                (Some(cs), Some(lang), Some(v)) => Param::Ext(Some(cs.into()), Some(lang.into()), format!("\"{v}\"")),
                _ => Param::Ext(None, None, format!("\"{value}\"")),
            };
            out.push((name, p));
        } else {
            out.push((name, Param::Plain(format!("\"{value}\""))));
        }
    }
    out
}

/// `^(\w+)\*(([0-9]+)\*?)?$`: the base name and the continuation number.
fn rfc2231_continuation(name: &str) -> Option<(String, Option<i64>)> {
    let (base, rest) = name.split_once('*')?;
    if base.is_empty() || !base.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_') {
        return None;
    }
    if rest.is_empty() {
        return Some((base.into(), None));
    }
    let digits = rest.strip_suffix('*').unwrap_or(rest);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    Some((base.into(), digits.parse().ok()))
}

/// `email.utils.collapse_rfc2231_value`.
fn collapse_rfc2231(p: Param) -> String {
    match p {
        Param::Plain(s) => unquote(&s),
        Param::Ext(cs, _, v) => {
            let raw = unquote(&v);
            let cs = cs.filter(|c| !c.is_empty()).unwrap_or_else(|| "us-ascii".into());
            decode_bytes(&latin1_bytes(&raw), &cs, false).unwrap_or_else(|| unquote(&raw))
        }
    }
}

/// `email.utils.unquote`.
fn unquote(s: &str) -> String {
    if s.len() > 1 {
        if s.starts_with('"') && s.ends_with('"') {
            return s[1..s.len() - 1].replace("\\\\", "\\").replace("\\\"", "\"");
        }
        if s.starts_with('<') && s.ends_with('>') {
            return s[1..s.len() - 1].to_string();
        }
    }
    s.to_string()
}

fn quote(s: &str) -> String {
    s.replace('\\', "\\\\").replace('"', "\\\"")
}

/// `urllib.parse.unquote(s, encoding="latin-1")`: each `%XX` becomes the char U+00XX.
fn percent_decode_latin1(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = String::new();
    let mut i = 0;
    while i < b.len() {
        if let (b'%', Some(Some(hi)), Some(Some(lo))) = (b[i], b.get(i + 1).map(|&c| hex(c)), b.get(i + 2).map(|&c| hex(c))) {
            out.push(char::from(hi << 4 | lo));
            i += 3;
        } else {
            let ch = s[i..].chars().next().unwrap();
            out.push(ch);
            i += ch.len_utf8();
        }
    }
    out
}

/// `str.encode('raw-unicode-escape')` for text that is latin-1 after percent-decoding.
fn latin1_bytes(s: &str) -> Vec<u8> {
    let mut out = vec![];
    for c in s.chars() {
        match u32::from(c) {
            n @ 0..=0xff => out.push(n as u8),
            n if n <= 0xffff => out.extend(format!("\\u{n:04x}").bytes()),
            n => out.extend(format!("\\U{n:08x}").bytes()),
        }
    }
    out
}

fn hex(c: u8) -> Option<u8> {
    (c as char).to_digit(16).map(|d| d as u8)
}

/// `binascii.a2b_qp(data, header=False)`.
fn qp_decode(b: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] != b'=' {
            out.push(b[i]);
            i += 1;
            continue;
        }
        i += 1;
        if i >= b.len() {
            break;
        }
        if b[i] == b'\n' || b[i] == b'\r' {
            if b[i] != b'\n' {
                while i < b.len() && b[i] != b'\n' {
                    i += 1;
                }
            }
            if i < b.len() {
                i += 1;
            }
        } else if b[i] == b'=' {
            out.push(b'=');
            i += 1;
        } else if i + 1 < b.len() && hex(b[i]).is_some() && hex(b[i + 1]).is_some() {
            out.push(hex(b[i]).unwrap() << 4 | hex(b[i + 1]).unwrap());
            i += 2;
        } else {
            out.push(b'=');
        }
    }
    out
}

/// `email._encoded_words.decode_b` on the joined lines: characters outside the alphabet are
/// skipped and missing padding is forgiven. Input that still cannot decode comes back as is.
fn b64_decode(b: &[u8]) -> Vec<u8> {
    use base64::engine::{general_purpose::GeneralPurpose, DecodePaddingMode, GeneralPurposeConfig};
    use base64::Engine;
    const LENIENT: GeneralPurpose = GeneralPurpose::new(
        &base64::alphabet::STANDARD,
        GeneralPurposeConfig::new().with_decode_padding_mode(DecodePaddingMode::Indifferent).with_decode_allow_trailing_bits(true),
    );
    let mut clean: Vec<u8> = b.iter().copied().take_while(|&c| c != b'=').filter(|c| c.is_ascii_alphanumeric() || *c == b'+' || *c == b'/').collect();
    if clean.len() % 4 == 1 {
        clean.pop();
    }
    let joined: Vec<u8> = b.iter().copied().filter(|c| *c != b'\r' && *c != b'\n').collect();
    LENIENT.decode(&clean).unwrap_or(joined)
}

/// `bytes.decode(charset, errors="replace")`, for a charset Python knows. An unknown charset
/// falls back to UTF-8, where the Python raised LookupError.
fn decode_text(b: &[u8], charset: &str) -> String {
    decode_bytes(b, charset, false).unwrap_or_else(|| String::from_utf8_lossy(b).into_owned())
}

/// `strict` makes an undecodable byte a failure, as `str(bytes, charset)` raises.
fn decode_bytes(b: &[u8], charset: &str, strict: bool) -> Option<String> {
    let norm: String = charset.trim().to_lowercase().chars().map(|c| if c.is_ascii_alphanumeric() || c == '.' { c } else { '_' }).collect();
    match norm.as_str() {
        "utf_8" | "utf8" | "u8" | "utf" | "cp65001" => {
            if strict {
                std::str::from_utf8(b).ok().map(str::to_string)
            } else {
                Some(String::from_utf8_lossy(b).into_owned())
            }
        }
        "us_ascii" | "ascii" | "646" | "ansi_x3.4_1968" | "ansi_x3_4_1968" | "ansi_x3.4_1986" | "cp367" | "csascii" | "ibm367" | "iso646_us" | "iso_646.irv_1991" | "iso_ir_6" | "us" => {
            if strict && !b.is_ascii() {
                return None;
            }
            Some(b.iter().map(|&c| if c.is_ascii() { c as char } else { '\u{fffd}' }).collect())
        }
        "latin_1" | "latin1" | "iso_8859_1" | "iso8859_1" | "8859" | "cp819" | "l1" | "iso_8859_1_1987" | "iso_ir_100" | "csisolatin1" | "ibm819" => {
            Some(b.iter().map(|&c| char::from(c)).collect())
        }
        _ => mail_parser::decoders::charsets::map::charset_decoder(charset.trim().as_bytes()).map(|f| f(b)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha256};

    fn fixture(name: &str) -> Vec<u8> {
        let lf: &[u8] = match name {
            "nested" => include_bytes!("fixtures/nested.eml"),
            "inline" => include_bytes!("fixtures/inline.eml"),
            "rfc2231" => include_bytes!("fixtures/rfc2231.eml"),
            "html_only" => include_bytes!("fixtures/html_only.eml"),
            "html_alt" => include_bytes!("fixtures/html_alt.eml"),
            "latin1" => include_bytes!("fixtures/latin1.eml"),
            "cp1252" => include_bytes!("fixtures/cp1252.eml"),
            "plain_noheaders" => include_bytes!("fixtures/plain_noheaders.eml"),
            _ => unreachable!(),
        };
        let mut out = Vec::with_capacity(lf.len() * 2);
        for &c in lf {
            if c == b'\n' {
                out.push(b'\r');
            }
            out.push(c);
        }
        out
    }

    fn atts(r: &Row) -> Vec<(i64, &str, &str, i64)> {
        r.attachments.iter().map(|a| (a.idx, a.filename.as_str(), a.mime.as_str(), a.size)).collect()
    }

    fn sha16(b: &[u8]) -> String {
        hex::encode(Sha256::digest(b))[..16].to_string()
    }

    fn downloads(raw: &[u8]) -> Vec<(String, String, usize, String)> {
        parse(raw).0.attachment_bytes(None).into_iter().map(|d| (d.filename, d.mime, d.data.len(), sha16(&d.data))).collect()
    }

    fn dl(f: &str, m: &str, n: usize, h: &str) -> (String, String, usize, String) {
        (f.into(), m.into(), n, h.into())
    }

    // Every expected value below is what awm.social.connectors.gmail_conn gives for the same
    // fixture with LF turned to CRLF, at release 49232bf9.

    #[test]
    fn nested_multipart_walks_into_a_forwarded_message() {
        let raw = fixture("nested");
        let r = row(&raw, "7");
        assert_eq!(r.from_addr, "tester@example.test");
        assert_eq!(r.sender_name, "Tester <tester@example.test>");
        assert_eq!(r.message_id, "<nested.1@example.test>");
        assert_eq!(r.ts, "Tue, 03 Nov 2026 10:00:00 +0000");
        assert_eq!(r.text, "Hello caf\u{e9},\r\na soft break and a trailing line.\r\n");
        assert_eq!(
            atts(&r),
            [(0, "report.pdf", "application/pdf", 285), (1, "attachment-1", "message/rfc822", 0), (2, "data.csv", "text/csv", 8), (3, "attachment-3", "application/octet-stream", 4)]
        );
        assert_eq!(r.attachments[2].r#ref, json!({"uid": "7", "ord": 2}));
        assert_eq!(
            downloads(&raw),
            [
                dl("report.pdf", "application/pdf", 285, "6f1649babd543d77"),
                dl("attachment-1", "message/rfc822", 0, "e3b0c44298fc1c14"),
                dl("data.csv", "text/csv", 8, "a64a34aacbdacd17"),
                dl("attachment-3", "application/octet-stream", 4, "63c1dd951ffedf6f"),
            ]
        );
        let one = parse(&raw).0.attachment_bytes(Some(2));
        assert_eq!(one.len(), 1);
        assert_eq!((one[0].filename.as_str(), one[0].data.as_slice()), ("data.csv", b"a,b\r\n1,2".as_slice()));
    }

    #[test]
    fn rfc2231_filenames_decode_as_python_does() {
        let raw = fixture("rfc2231");
        let r = row(&raw, "7");
        assert_eq!(r.text, "see the attached files");
        assert_eq!(
            atts(&r),
            [
                (0, "r\u{e9}sum\u{e9} final.txt", "text/plain", 12),
                (1, "caf\u{e9}_menu.bin", "application/octet-stream", 4),
                (2, "plain.txt", "text/plain", 10),
                (3, "spaced name.dat", "application/octet-stream", 6),
                (4, "say \"hi\".txt", "application/octet-stream", 6),
            ]
        );
        assert_eq!(
            downloads(&raw),
            [
                dl("r\u{e9}sum\u{e9} final.txt", "text/plain", 12, "95c30f789c33d459"),
                dl("caf\u{e9}_menu.bin", "application/octet-stream", 4, "054edec1d0211f62"),
                dl("plain.txt", "text/plain", 10, "a50d91e415bee52c"),
                dl("spaced name.dat", "application/octet-stream", 6, "4cf28831d09470e8"),
                dl("say \"hi\".txt", "application/octet-stream", 6, "b3a2bd470cb2c4f9"),
            ]
        );
    }

    #[test]
    fn non_utf8_charsets_decode_as_python_does() {
        let r = row(&fixture("latin1"), "7");
        assert_eq!(r.text, "Gr\u{fc}\u{df}e aus der K\u{fc}che\n");
        let raw = fixture("cp1252");
        let r = row(&raw, "7");
        assert_eq!(r.text, "Price: 5 \u{20ac}, \u{201c}quoted\u{201d} = done");
        assert_eq!(atts(&r), [(0, "note.txt", "text/plain", 6)]);
        assert_eq!(downloads(&raw), [dl("note.txt", "text/plain", 6, "d22e5e4f55058d2c")]);
    }

}
