//! The entries of a manifest's `needs.network` (install.md R116 item 1).
//!
//! An entry is `host`, `*.host` (any subdomain) or either with `:port`. A host entry with no
//! port allows 80 and 443. `*` alone allows every host and port, for a plugin that browses
//! wherever it is sent; the install review sees it. An entry that starts `tcp:` or `udp:` names
//! traffic the egress proxy cannot see, such as ssh or a wake-on-LAN broadcast: the gateway
//! records it and checks nothing.

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NetworkEntry {
    Any,
    Host { pattern: String, port: Option<u16> },
    Recorded(String),
}

impl NetworkEntry {
    pub fn parse(raw: &str) -> Result<Self, String> {
        let entry = raw.trim();
        if entry == "*" {
            return Ok(Self::Any);
        }
        if let Some((proto, rest)) = entry.split_once(':').filter(|(p, _)| *p == "tcp" || *p == "udp") {
            let port = rest.split_whitespace().next().unwrap_or("");
            return match port.parse::<u16>() {
                Ok(_) => Ok(Self::Recorded(entry.to_string())),
                Err(_) => Err(format!("network entry {raw:?}: {proto}: takes a port, as {proto}:22")),
            };
        }
        let (host, port) = match entry.rsplit_once(':') {
            Some((h, p)) => (h, Some(p.parse::<u16>().map_err(|_| format!("network entry {raw:?}: {p:?} is not a port"))?)),
            None => (entry, None),
        };
        let bare = host.strip_prefix("*.").unwrap_or(host);
        let label_ok = |l: &str| {
            !l.is_empty() && !l.starts_with('-') && l.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')
        };
        if bare.is_empty() || !bare.split('.').all(label_ok) {
            return Err(format!("network entry {raw:?} is not host, *.host, host:port, tcp:<port> or udp:<port>"));
        }
        Ok(Self::Host { pattern: host.to_ascii_lowercase(), port })
    }

    /// Whether a connection to `host:port` matches this entry.
    pub fn allows(&self, host: &str, port: u16) -> bool {
        let (pattern, want) = match self {
            Self::Any => return true,
            Self::Host { pattern, port } => (pattern, port),
            Self::Recorded(_) => return false,
        };
        let host = host.trim_end_matches('.').to_ascii_lowercase();
        let host_ok = match pattern.strip_prefix("*.") {
            Some(suffix) => host.strip_suffix(suffix).is_some_and(|sub| sub.len() > 1 && sub.ends_with('.')),
            None => host == *pattern,
        };
        host_ok && want.map_or(port == 80 || port == 443, |w| w == port)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn entries_parse_and_match() {
        let e = NetworkEntry::parse("api.zotero.org").unwrap();
        assert!(e.allows("api.zotero.org", 443) && e.allows("API.zotero.org.", 80));
        assert!(!e.allows("api.zotero.org", 8443) && !e.allows("zotero.org", 443));
        let w = NetworkEntry::parse("*.slack.com").unwrap();
        assert!(w.allows("wss-primary.slack.com", 443) && w.allows("a.b.slack.com", 443));
        assert!(!w.allows("slack.com", 443) && !w.allows("evilslack.com", 443));
        let p = NetworkEntry::parse("nexus.example.org:8443").unwrap();
        assert!(p.allows("nexus.example.org", 8443) && !p.allows("nexus.example.org", 443));
        assert!(NetworkEntry::parse("10.0.0.2:22").unwrap().allows("10.0.0.2", 22));
        assert!(NetworkEntry::parse("*").unwrap().allows("anything.example", 8080));
        assert_eq!(NetworkEntry::parse("tcp:22 lan").unwrap(), NetworkEntry::Recorded("tcp:22 lan".into()));
        assert!(!NetworkEntry::parse("udp:9 broadcast").unwrap().allows("x", 9));
        for bad in ["", "https://x.org", "x.org/path", "x..org", "tcp:ssh", "x.org:http", "*."] {
            assert!(NetworkEntry::parse(bad).is_err(), "{bad:?} must not parse");
        }
    }
}
