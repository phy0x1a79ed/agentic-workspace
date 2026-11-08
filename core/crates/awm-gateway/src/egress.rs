//! The egress proxy: a plugin's services reach only the hosts its `needs.network` lists
//! (install.md R116 item 1).
//!
//! Each plugin gets its own loopback port, so the proxy knows the plugin by the port a
//! connection arrives on. A service finds the port in `HTTP_PROXY` and `HTTPS_PROXY`. The
//! proxy reads the plugin's list at each connection, so a reload takes effect at once. Like
//! the tool guard, it catches a plugin's mistakes: a client that ignores the proxy variables
//! is not held.

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Mutex, Weak};

use awm_proto::NetworkEntry;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

use crate::gateway::Gateway;

const HEAD_MAX: usize = 16 * 1024;
/// Hosts every service reaches with no proxy: this machine.
const LOOPBACK: [&str; 3] = ["localhost", "127.0.0.1", "::1"];

#[derive(Default)]
pub struct Egress {
    ports: Mutex<HashMap<String, u16>>,
}

impl Egress {
    /// The proxy port of `plugin`, bound on first use and kept for the gateway's life.
    pub fn port_for(&self, gw: Weak<Gateway>, plugin: &str) -> std::io::Result<u16> {
        let mut ports = self.ports.lock().unwrap_or_else(|p| p.into_inner());
        if let Some(port) = ports.get(plugin) {
            return Ok(*port);
        }
        let std_listener = std::net::TcpListener::bind("127.0.0.1:0")?;
        std_listener.set_nonblocking(true)?;
        let port = std_listener.local_addr()?.port();
        let listener = TcpListener::from_std(std_listener)?;
        let name = plugin.to_string();
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                let (gw, name) = (gw.clone(), name.clone());
                tokio::spawn(async move {
                    if let Err(e) = serve(stream, &gw, &name).await {
                        tracing::debug!("egress proxy for {name}: {e}");
                    }
                });
            }
        });
        ports.insert(plugin.to_string(), port);
        Ok(port)
    }
}

/// The hosts a service reaches with no proxy: loopback, the gateway's own host, and the
/// fleet's nodes and their addresses from the edge list, since a peer call is awm's own traffic.
pub fn exempt_hosts(edges_file: &Path, hub_url: &str) -> Vec<String> {
    let mut hosts: Vec<String> = LOOPBACK.iter().map(|h| h.to_string()).collect();
    if let Some(host) = hub_url.split("://").nth(1).and_then(|r| r.split(['/', ':']).next()).filter(|h| !h.is_empty()) {
        hosts.push(host.to_string());
    }
    if let Some(nodes) = std::fs::read_to_string(edges_file)
        .ok()
        .and_then(|t| serde_yaml::from_str::<serde_yaml::Value>(&t).ok())
        .and_then(|v| v.get("nodes").and_then(serde_yaml::Value::as_mapping).cloned())
    {
        for (name, node) in nodes {
            hosts.extend(name.as_str().map(str::to_string));
            if let Some(addrs) = node.get("addresses").and_then(serde_yaml::Value::as_mapping) {
                hosts.extend(addrs.values().filter_map(serde_yaml::Value::as_str).map(|a| a.split(':').next().unwrap_or(a).to_string()));
            }
        }
    }
    hosts.sort();
    hosts.dedup();
    hosts
}

/// The variables that point a service at its plugin's proxy.
pub fn proxy_env(port: u16, exempt: &[String]) -> Vec<(&'static str, String)> {
    let url = format!("http://127.0.0.1:{port}");
    let none = exempt.join(",");
    vec![
        ("HTTP_PROXY", url.clone()),
        ("HTTPS_PROXY", url.clone()),
        ("http_proxy", url.clone()),
        ("https_proxy", url),
        ("NO_PROXY", none.clone()),
        ("no_proxy", none),
    ]
}

/// Why `plugin` may not reach `host:port`, or None when its manifest lists it.
pub fn refusal(gw: &Gateway, plugin: &str, host: &str, port: u16) -> Option<String> {
    let exempt = exempt_hosts(&gw.cfg().edges_path(&gw.state), &gw.hub_url());
    if exempt.iter().any(|h| h.eq_ignore_ascii_case(host)) {
        return None;
    }
    let load = gw.read_load();
    let Some(p) = load.plugins.iter().find(|p| p.name == plugin) else {
        return Some(format!("plugin {plugin:?} is not loaded"));
    };
    if p.needs.network.iter().filter_map(|e| NetworkEntry::parse(e).ok()).any(|e| e.allows(host, port)) {
        return None;
    }
    let entry = if port == 80 || port == 443 { host.to_string() } else { format!("{host}:{port}") };
    Some(format!(
        "plugin {plugin:?} does not list {host}:{port} in its awm-plugin.toml; add {entry:?} to network in its [needs]"
    ))
}

async fn serve(mut client: TcpStream, gw: &Weak<Gateway>, plugin: &str) -> std::io::Result<()> {
    let mut buf = Vec::with_capacity(1024);
    let head_end = loop {
        let mut chunk = [0u8; 2048];
        let n = client.read(&mut chunk).await?;
        if n == 0 {
            return Ok(());
        }
        buf.extend_from_slice(&chunk[..n]);
        if let Some(i) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
            break i + 4;
        }
        if buf.len() > HEAD_MAX {
            return answer(&mut client, 431, "request head too large").await;
        }
    };
    let head = String::from_utf8_lossy(&buf[..head_end]).to_string();
    let mut lines = head.split("\r\n");
    let mut first = lines.next().unwrap_or("").split_whitespace();
    let (method, target, version) = (first.next().unwrap_or(""), first.next().unwrap_or(""), first.next().unwrap_or("HTTP/1.1"));
    let Some((host, port, path)) = target_of(method, target) else {
        return answer(&mut client, 400, "the awm egress proxy takes CONNECT host:port or an absolute http:// URL").await;
    };
    let Some(gw) = gw.upgrade() else { return answer(&mut client, 503, "the gateway is stopping").await };
    if let Some(why) = refusal(&gw, plugin, &host, port) {
        tracing::warn!("egress guard: refused plugin {plugin} {host}:{port}: {why}");
        return answer(&mut client, 403, &format!("awm egress guard: {why}")).await;
    }
    drop(gw);
    let mut upstream = match TcpStream::connect((host.as_str(), port)).await {
        Ok(s) => s,
        Err(e) => return answer(&mut client, 502, &format!("{host}:{port}: {e}")).await,
    };
    if method.eq_ignore_ascii_case("CONNECT") {
        client.write_all(b"HTTP/1.1 200 Connection Established\r\n\r\n").await?;
        upstream.write_all(&buf[head_end..]).await?;
    } else {
        // One request per connection, so a reused connection cannot reach a second host.
        let mut out = format!("{method} {path} {version}\r\n");
        for line in lines.filter(|l| !l.is_empty()) {
            let name = line.split(':').next().unwrap_or("").trim().to_ascii_lowercase();
            if !name.starts_with("proxy-") && name != "connection" {
                out.push_str(line);
                out.push_str("\r\n");
            }
        }
        out.push_str("Connection: close\r\n\r\n");
        upstream.write_all(out.as_bytes()).await?;
        upstream.write_all(&buf[head_end..]).await?;
    }
    tokio::io::copy_bidirectional(&mut client, &mut upstream).await?;
    Ok(())
}

/// The host, port and origin-form path a proxy request names.
fn target_of(method: &str, target: &str) -> Option<(String, u16, String)> {
    let split = |authority: &str, default: u16| -> Option<(String, u16)> {
        if let Some(rest) = authority.strip_prefix('[') {
            let (h, after) = rest.split_once(']')?;
            let port = after.strip_prefix(':').map_or(Some(default), |p| p.parse().ok())?;
            return Some((h.to_string(), port));
        }
        match authority.rsplit_once(':') {
            Some((h, p)) => Some((h.to_string(), p.parse().ok()?)),
            None => Some((authority.to_string(), default)),
        }
    };
    if method.eq_ignore_ascii_case("CONNECT") {
        let (h, p) = split(target, 443)?;
        return (!h.is_empty()).then(|| (h, p, String::new()));
    }
    let rest = target.strip_prefix("http://")?;
    let (authority, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    let (h, p) = split(authority, 80)?;
    (!h.is_empty()).then(|| (h, p, path.to_string()))
}

async fn answer(client: &mut TcpStream, status: u16, text: &str) -> std::io::Result<()> {
    let reason = match status {
        400 => "Bad Request",
        403 => "Forbidden",
        431 => "Request Header Fields Too Large",
        502 => "Bad Gateway",
        _ => "Service Unavailable",
    };
    let body = format!("{text}\n");
    let reply = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    client.write_all(reply.as_bytes()).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_manifest_network_entry_parses() {
        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
        let mut seen = 0;
        for root in ["core/plugins", "awm/services", "awm/plugins"] {
            for dir in std::fs::read_dir(repo.join(root)).unwrap().flatten() {
                let Ok(text) = std::fs::read_to_string(dir.path().join(awm_proto::MANIFEST_FILE)) else { continue };
                let m = awm_proto::Manifest::parse(&text).unwrap_or_else(|e| panic!("{}: {e}", dir.path().display()));
                for entry in &m.needs.network {
                    NetworkEntry::parse(entry).unwrap_or_else(|e| panic!("{}: {e}", dir.path().display()));
                }
                seen += 1;
            }
        }
        assert!(seen > 20, "found only {seen} manifests");
    }

    #[test]
    fn a_request_names_its_target_and_the_fleet_and_loopback_go_direct() {
        assert_eq!(target_of("CONNECT", "api.zotero.org:443"), Some(("api.zotero.org".into(), 443, String::new())));
        assert_eq!(target_of("GET", "http://x.org/a?b"), Some(("x.org".into(), 80, "/a?b".into())));
        assert_eq!(target_of("GET", "http://x.org:8080"), Some(("x.org".into(), 8080, "/".into())));
        assert_eq!(target_of("CONNECT", "[::1]:22"), Some(("::1".into(), 22, String::new())));
        assert_eq!(target_of("GET", "/relative"), None);
        assert_eq!(target_of("CONNECT", "x.org:http"), None);

        let t = tempfile::tempdir().unwrap();
        let edges = t.path().join("edges.yml");
        std::fs::write(&edges, "nodes:\n  home: {role: homebase, addresses: {lan: 10.0.0.2, ts: null}}\n  work: {role: work}\n").unwrap();
        let hosts = exempt_hosts(&edges, "http://192.168.1.5:7011/");
        for h in ["localhost", "127.0.0.1", "::1", "192.168.1.5", "home", "10.0.0.2", "work"] {
            assert!(hosts.iter().any(|x| x == h), "{h} missing from {hosts:?}");
        }
        let env = proxy_env(4000, &hosts);
        assert!(env.contains(&("HTTPS_PROXY", "http://127.0.0.1:4000".into())));
        assert!(env.iter().any(|(k, v)| *k == "no_proxy" && v.contains("10.0.0.2")));
    }
}
