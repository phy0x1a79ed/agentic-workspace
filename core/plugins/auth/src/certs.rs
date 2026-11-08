//! The edge's TLS material: a private root CA and a leaf signed by it, plus the SAN set
//! the leaf must cover (ports/auth.md C1, State; the port of `awm.tlsfront.certs`).
//!
//! The root lives at `$REMOTE_AUDIO_CA_DIR`, else `$XDG_CONFIG_HOME/remote-audio/ca`, the
//! same root remote-audio uses, so a device that trusts it needs no new setup. It is read
//! in place and never re-minted once either half exists.
//!
//! A node holding `ca.pem` without `ca-key.pem` is a *trust consumer*: it can verify its
//! peers but cannot mint for the fleet. There nothing is provisioned. A missing leaf
//! refuses to start, because minting a fresh root would replace the fleet's trust root and
//! surface as a certificate error on every peer instead of the configuration mistake it is.

use std::collections::BTreeSet;
use std::ffi::OsString;
use std::io::Write;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use rcgen::{
    BasicConstraints, CertificateParams, DistinguishedName, DnType, ExtendedKeyUsagePurpose,
    Ia5String, IsCa, KeyPair, KeyUsagePurpose, SanType, SerialNumber, PKCS_ECDSA_P256_SHA256,
};
use rustls::pki_types::{PrivateKeyDer, PrivatePkcs8KeyDer};

pub const CA_DIR_ENV: &str = "REMOTE_AUDIO_CA_DIR";
pub const EXTRA_SANS_ENV: &str = "AWM_TLS_EXTRA_SANS";
pub const EDGE_CERT_DIR_ENV: &str = "AWM_EDGE_CERT_DIR";
pub const MESH_SUBNET_ENV: &str = "AWM_MESH_SUBNET";
/// The fleet's ZeroTier network.
pub const DEFAULT_MESH_SUBNET: &str = "10.74.81.0/24";

/// A leaf's lifetime: the mobile cap.
pub const LEAF_DAYS: i64 = 397;
pub const CA_DAYS: i64 = 3650;
/// Below this many days left, the CA holder re-cuts its leaf and a trust consumer warns.
pub const RENEW_DAYS: i64 = 30;

pub const CA_CN: &str = "remote-audio local CA";
pub const LEAF_CN: &str = "awm-https-front";

#[derive(Debug, thiserror::Error)]
pub enum CertError {
    /// The CA halves are in a state this node must not resolve by minting.
    #[error("{0}")]
    TrustConsumer(String),
    #[error("{0}: {1}")]
    Io(PathBuf, #[source] std::io::Error),
    #[error("{0}")]
    Mint(String),
}

fn io_err(path: &Path) -> impl FnOnce(std::io::Error) -> CertError + '_ {
    move |e| CertError::Io(path.to_path_buf(), e)
}

/// What [`ensure_certs`] provisioned. `san` is the comma-joined SAN set the leaf carries.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct CertInfo {
    pub cert: PathBuf,
    pub key: PathBuf,
    /// The root mirrored into the cert dir, which `/ca.crt` serves.
    pub ca: PathBuf,
    pub san: String,
}

impl CertInfo {
    /// The root's PEM bytes for `/ca.crt`, read on each call as the Python front did.
    pub fn ca_pem(&self) -> std::io::Result<Vec<u8>> {
        std::fs::read(&self.ca)
    }
}

// ---------------------------------------------------------------------------
// SANs
// ---------------------------------------------------------------------------

/// Python's `str.isspace`: Rust's White_Space plus the four ASCII separators U+001C..U+001F.
fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Python's `str.splitlines` boundaries.
fn py_line_break(c: char) -> bool {
    matches!(
        c,
        '\n' | '\r' | '\u{0b}' | '\u{0c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}'
    )
}

/// `re.split(r"[,\s]+", s)` without the empty edge tokens, which the normaliser drops anyway.
fn split_tokens(s: &str) -> impl Iterator<Item = &str> {
    s.split(|c: char| c == ',' || py_space(c)).filter(|t| !t.is_empty())
}

/// Python's `ipaddress.ip_address` acceptance: a scoped IPv6 (`fe80::1%eth0`) is an address.
fn py_parses_as_ip(tok: &str) -> bool {
    if tok.parse::<Ipv4Addr>().is_ok() {
        return true;
    }
    match tok.split_once('%') {
        Some((_, scope)) if scope.is_empty() || scope.contains('%') => false,
        Some((addr, _)) => addr.parse::<Ipv6Addr>().is_ok(),
        None => tok.parse::<Ipv6Addr>().is_ok(),
    }
}

/// Coerce an operator-supplied SAN token into `IP:x` / `DNS:x` form; `None` for an empty one.
///
/// A prefixed token keeps its value as given. A bare token is `IP:` when it parses as an
/// address and `DNS:` otherwise.
pub fn normalize_san(tok: &str) -> Option<String> {
    let tok = tok.trim_matches(py_space);
    if tok.is_empty() {
        return None;
    }
    if let Some((pre, val)) = tok.split_once(':') {
        let pre = pre.to_uppercase();
        if pre == "IP" || pre == "DNS" {
            let val = val.trim_matches(py_space);
            return (!val.is_empty()).then(|| format!("{pre}:{val}"));
        }
    }
    if py_parses_as_ip(tok) {
        Some(format!("IP:{tok}"))
    } else {
        Some(format!("DNS:{tok}"))
    }
}

/// The raw tokens of a `.sans` file: one or more per line, comma or space separated, `#`
/// starting a comment that runs to the end of the line.
pub fn sans_file_tokens(text: &str) -> Vec<&str> {
    text.split(py_line_break).flat_map(|line| split_tokens(line.split('#').next().unwrap_or(""))).collect()
}

fn push_unique(out: &mut Vec<String>, s: String) {
    if !out.contains(&s) {
        out.push(s);
    }
}

/// The SANs a host cannot enumerate for itself, such as the Windows-side mesh address of a
/// WSL node: from `env` (else `$AWM_TLS_EXTRA_SANS`) and the `san_file`, deduplicated in order.
pub fn extra_sans(env: Option<&str>, san_file: Option<&Path>) -> Result<Vec<String>, CertError> {
    let from_env = match env {
        Some(v) => v.to_owned(),
        None => std::env::var(EXTRA_SANS_ENV).unwrap_or_default(),
    };
    let file_text = match san_file {
        Some(p) if p.exists() => {
            let bytes = std::fs::read(p).map_err(io_err(p))?;
            String::from_utf8(bytes)
                .map_err(|e| CertError::Io(p.to_path_buf(), std::io::Error::new(std::io::ErrorKind::InvalidData, e)))?
        }
        _ => String::new(),
    };
    let mut out = Vec::new();
    for tok in split_tokens(&from_env).chain(sans_file_tokens(&file_text)) {
        if let Some(n) = normalize_san(tok) {
            push_unique(&mut out, n);
        }
    }
    Ok(out)
}

/// [`default_sans`] plus [`extra_sans`], defaults first, deduplicated.
pub fn resolve_sans(env: Option<&str>, san_file: Option<&Path>) -> Result<Vec<String>, CertError> {
    resolve_sans_with(default_sans(), env, san_file)
}

/// [`resolve_sans`] over a given default list.
pub fn resolve_sans_with(
    defaults: Vec<String>,
    env: Option<&str>,
    san_file: Option<&Path>,
) -> Result<Vec<String>, CertError> {
    let mut sans = defaults;
    for s in extra_sans(env, san_file)? {
        push_unique(&mut sans, s);
    }
    Ok(sans)
}

/// Loopback plus every non-loopback IPv4 address of this host.
pub fn default_sans() -> Vec<String> {
    let mut sans = vec!["IP:127.0.0.1".to_owned(), "DNS:localhost".to_owned()];
    sans.extend(host_ipv4().into_iter().map(|ip| format!("IP:{ip}")));
    sans
}

/// One SAN as compared: an address by value, a DNS name case-folded.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub enum SanKey {
    Ip(IpAddr),
    Dns(String),
    Other(String),
}

pub fn san_key(tok: &str) -> SanKey {
    if let Some(v) = tok.strip_prefix("IP:") {
        if let Ok(ip) = v.parse::<IpAddr>() {
            return SanKey::Ip(ip);
        }
    } else if let Some(v) = tok.strip_prefix("DNS:") {
        return SanKey::Dns(v.to_ascii_lowercase());
    }
    SanKey::Other(tok.to_owned())
}

/// A SAN list as a set, so that two lists in a different order compare equal.
pub fn san_set<S: AsRef<str>>(sans: &[S]) -> BTreeSet<SanKey> {
    sans.iter().map(|s| san_key(s.as_ref())).collect()
}

// ---------------------------------------------------------------------------
// Host addresses and the WSL fallback
// ---------------------------------------------------------------------------

/// The addresses `hostname -I` prints: every up, non-loopback interface, IPv6 link-local skipped.
pub fn host_addresses() -> Vec<IpAddr> {
    let mut out = Vec::new();
    // SAFETY: getifaddrs hands back a list we only read and then free once.
    unsafe {
        let mut head: *mut libc::ifaddrs = std::ptr::null_mut();
        if libc::getifaddrs(&mut head) != 0 {
            return out;
        }
        let mut p = head;
        while !p.is_null() {
            let ifa = &*p;
            p = ifa.ifa_next;
            if ifa.ifa_addr.is_null() {
                continue;
            }
            let flags = ifa.ifa_flags as libc::c_int;
            if flags & libc::IFF_UP == 0 || flags & libc::IFF_LOOPBACK != 0 {
                continue;
            }
            let ip = match (*ifa.ifa_addr).sa_family as libc::c_int {
                libc::AF_INET => {
                    let sin = &*(ifa.ifa_addr as *const libc::sockaddr_in);
                    IpAddr::V4(Ipv4Addr::from(u32::from_be(sin.sin_addr.s_addr)))
                }
                libc::AF_INET6 => {
                    let sin6 = &*(ifa.ifa_addr as *const libc::sockaddr_in6);
                    let v6 = Ipv6Addr::from(sin6.sin6_addr.s6_addr);
                    if v6.segments()[0] & 0xffc0 == 0xfe80 {
                        continue;
                    }
                    IpAddr::V6(v6)
                }
                _ => continue,
            };
            if !out.contains(&ip) {
                out.push(ip);
            }
        }
        libc::freeifaddrs(head);
    }
    out
}

/// This host's non-loopback IPv4 addresses, in interface order.
pub fn host_ipv4() -> Vec<Ipv4Addr> {
    host_addresses()
        .into_iter()
        .filter_map(|ip| match ip {
            IpAddr::V4(v4) if !v4.is_loopback() => Some(v4),
            _ => None,
        })
        .collect()
}

/// An address block, `addr/prefix`, parsed leniently as Python's `ip_network(strict=False)`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IpNet {
    pub addr: IpAddr,
    pub prefix: u8,
}

impl IpNet {
    pub fn parse(s: &str) -> Option<IpNet> {
        let (a, p) = match s.trim().split_once('/') {
            Some((a, p)) => (a, Some(p)),
            None => (s.trim(), None),
        };
        let addr: IpAddr = a.parse().ok()?;
        let max = if addr.is_ipv4() { 32 } else { 128 };
        let prefix = match p {
            Some(p) => p.parse::<u8>().ok().filter(|n| *n <= max)?,
            None => max,
        };
        Some(IpNet { addr, prefix })
    }

    pub fn contains(&self, ip: IpAddr) -> bool {
        match (self.addr, ip) {
            (IpAddr::V4(n), IpAddr::V4(a)) => {
                let mask = if self.prefix == 0 { 0 } else { u32::MAX << (32 - self.prefix as u32) };
                u32::from(n) & mask == u32::from(a) & mask
            }
            (IpAddr::V6(n), IpAddr::V6(a)) => {
                let mask = if self.prefix == 0 { 0 } else { u128::MAX << (128 - self.prefix as u32) };
                u128::from(n) & mask == u128::from(a) & mask
            }
            _ => false,
        }
    }
}

/// `$AWM_MESH_SUBNET`, else the fleet's; `None` when the variable does not parse.
pub fn mesh_subnet() -> Option<IpNet> {
    let raw = std::env::var(MESH_SUBNET_ENV).ok().filter(|s| !s.is_empty());
    IpNet::parse(raw.as_deref().unwrap_or(DEFAULT_MESH_SUBNET))
}

/// This host's own address on the mesh, from the interfaces it can see.
pub fn mesh_address(net: &IpNet) -> Option<IpAddr> {
    host_addresses().into_iter().find(|ip| net.contains(*ip))
}

/// The first mesh address declared in a `.sans` file, for a WSL node that cannot see its own:
/// there the mesh client runs on the Windows host and traffic arrives through a port forward.
pub fn declared_mesh_address(net: &IpNet, sans_files: &[&Path]) -> Option<IpAddr> {
    for path in sans_files {
        let Ok(text) = std::fs::read_to_string(path) else { continue };
        for tok in sans_file_tokens(&text) {
            let Some(norm) = normalize_san(tok) else { continue };
            if let SanKey::Ip(ip) = san_key(&norm) {
                if net.contains(ip) {
                    return Some(ip);
                }
            }
        }
    }
    None
}

/// The host a browser on the mesh dials: the enumerated mesh address, else one declared in
/// `sans_files`, else loopback, so a page shows a URL that at least works from here.
pub fn front_host(sans_files: &[&Path]) -> String {
    let ip = mesh_subnet()
        .and_then(|net| mesh_address(&net).or_else(|| declared_mesh_address(&net, sans_files)))
        .unwrap_or(IpAddr::V4(Ipv4Addr::LOCALHOST));
    match ip {
        IpAddr::V6(v6) => format!("[{v6}]"),
        IpAddr::V4(v4) => v4.to_string(),
    }
}

/// `https://{front_host}:{port}`: a front's `url`.
pub fn front_url(port: u16, sans_files: &[&Path]) -> String {
    format!("https://{}:{port}", front_host(sans_files))
}

// ---------------------------------------------------------------------------
// Where the material lives
// ---------------------------------------------------------------------------

/// The CA directory: `$REMOTE_AUDIO_CA_DIR`, else `$XDG_CONFIG_HOME/remote-audio/ca`, else
/// `~/.config/remote-audio/ca`.
pub fn ca_dir() -> PathBuf {
    if let Some(d) = std::env::var_os(CA_DIR_ENV).filter(|v| !v.is_empty()) {
        return PathBuf::from(d);
    }
    let base = std::env::var_os("XDG_CONFIG_HOME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".config"));
    base.join("remote-audio").join("ca")
}

/// The former httpsfront service folder, `<workspace>/awm/services/httpsfront`.
pub fn legacy_service_dir(workspace: &Path) -> PathBuf {
    workspace.join("awm").join("services").join("httpsfront")
}

/// The edge's live `.sans` file: `<state>/sans`.
pub fn sans_file(state_dir: &Path) -> PathBuf {
    state_dir.join("sans")
}

/// The edge's cert dir, migrating the legacy material first.
///
/// `$AWM_EDGE_CERT_DIR`, when set, is the cert dir, read and minted into in place. Else the
/// dir is `<state>/certs`, seeded once from `<legacy_service_dir>/.certs`. In both cases
/// `<legacy_service_dir>/.sans` is copied once to `<state>/sans`. A failed copy is logged and
/// left to [`ensure_certs`], which mints on the CA holder and refuses on a trust consumer.
pub fn cert_dir(state_dir: &Path, legacy_service_dir: &Path) -> PathBuf {
    cert_dir_for(std::env::var_os(EDGE_CERT_DIR_ENV), state_dir, legacy_service_dir)
}

/// [`cert_dir`] with the value of `$AWM_EDGE_CERT_DIR` given.
pub fn cert_dir_for(edge_cert_dir: Option<OsString>, state_dir: &Path, legacy_service_dir: &Path) -> PathBuf {
    let legacy_sans = legacy_service_dir.join(".sans");
    if let Some(dir) = edge_cert_dir.filter(|v| !v.is_empty()) {
        if let Err(e) = copy_file_once(&legacy_sans, &sans_file(state_dir)) {
            tracing::warn!("could not copy {} to the state dir: {e}", legacy_sans.display());
        }
        return PathBuf::from(dir);
    }
    match copy_legacy_once(&legacy_service_dir.join(".certs"), &legacy_sans, state_dir) {
        Ok(copied) => {
            for p in copied {
                tracing::info!("copied the legacy {} into the state dir", p.display());
            }
        }
        Err(e) => tracing::warn!("could not copy the legacy certs into {}: {e}", state_dir.display()),
    }
    state_dir.join("certs")
}

/// Copy the legacy leaf dir to `<state>/certs` and the legacy `.sans` to `<state>/sans`.
///
/// Never overwrites: a file already in state stays, and the leaf pair is copied only when
/// state holds neither half, so a cert and key from different mints never meet. File modes
/// are kept. Returns the destination paths written.
pub fn copy_legacy_once(legacy_cert_dir: &Path, legacy_sans: &Path, state_dir: &Path) -> std::io::Result<Vec<PathBuf>> {
    let mut copied = Vec::new();
    let dst_dir = state_dir.join("certs");
    if let Some(meta) = std::fs::metadata(legacy_cert_dir).ok().filter(|m| m.is_dir()) {
        if !dst_dir.exists() {
            std::fs::create_dir_all(&dst_dir)?;
            std::fs::set_permissions(&dst_dir, std::fs::Permissions::from_mode(meta.permissions().mode() & 0o7777))?;
        }
        let pair_in_state = dst_dir.join("cert.pem").exists() || dst_dir.join("key.pem").exists();
        let mut entries: Vec<_> = std::fs::read_dir(legacy_cert_dir)?.collect::<Result<_, _>>()?;
        entries.sort_by_key(|e| e.file_name());
        for entry in entries {
            let name = entry.file_name();
            if pair_in_state && (name == "cert.pem" || name == "key.pem") {
                continue;
            }
            let src = entry.path();
            if !std::fs::metadata(&src).map(|m| m.is_file()).unwrap_or(false) {
                continue;
            }
            let dst = dst_dir.join(&name);
            if copy_file_once(&src, &dst)? {
                copied.push(dst);
            }
        }
    }
    let dst_sans = sans_file(state_dir);
    if copy_file_once(legacy_sans, &dst_sans)? {
        copied.push(dst_sans);
    }
    Ok(copied)
}

/// Copy `src` to `dst` with `src`'s mode unless `dst` exists. `Ok(false)` when there was
/// nothing to do. The link step fails rather than replace a file that appeared meanwhile.
fn copy_file_once(src: &Path, dst: &Path) -> std::io::Result<bool> {
    let Ok(meta) = std::fs::metadata(src) else { return Ok(false) };
    if !meta.is_file() || dst.symlink_metadata().is_ok() {
        return Ok(false);
    }
    if let Some(parent) = dst.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let bytes = std::fs::read(src)?;
    let tmp = tmp_path(dst);
    let mode = meta.permissions().mode() & 0o7777;
    write_new(&tmp, &bytes, mode)?;
    std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(mode))?;
    let linked = std::fs::hard_link(&tmp, dst);
    let _ = std::fs::remove_file(&tmp);
    match linked {
        Ok(()) => Ok(true),
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => Ok(false),
        Err(e) => Err(e),
    }
}

fn tmp_path(dst: &Path) -> PathBuf {
    let name = dst.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    dst.with_file_name(format!(".{name}.tmp.{}.{nanos}", std::process::id()))
}

fn write_new(path: &Path, bytes: &[u8], mode: u32) -> std::io::Result<()> {
    let mut f = std::fs::OpenOptions::new().write(true).create_new(true).mode(mode).open(path)?;
    f.write_all(bytes)?;
    f.sync_all()
}

/// Write `bytes` to `path` through a temp file and a rename, so a reader never sees half.
fn write_atomic(path: &Path, bytes: &[u8], mode: u32) -> Result<(), CertError> {
    let tmp = tmp_path(path);
    write_new(&tmp, bytes, mode).map_err(io_err(&tmp))?;
    std::fs::rename(&tmp, path).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        CertError::Io(path.to_path_buf(), e)
    })
}

// ---------------------------------------------------------------------------
// Reading certificates and keys
// ---------------------------------------------------------------------------

fn read_cert_der(path: &Path) -> Result<Vec<u8>, String> {
    let bytes = std::fs::read(path).map_err(|e| e.to_string())?;
    let first = rustls_pemfile::certs(&mut bytes.as_slice()).next();
    match first {
        Some(Ok(der)) => Ok(der.to_vec()),
        Some(Err(e)) => Err(e.to_string()),
        None => Err("no certificate in the file".to_owned()),
    }
}

fn with_cert<T>(path: &Path, f: impl FnOnce(&x509_parser::certificate::X509Certificate<'_>) -> T) -> Result<T, String> {
    let der = read_cert_der(path)?;
    let (_, cert) = x509_parser::parse_x509_certificate(&der).map_err(|e| e.to_string())?;
    Ok(f(&cert))
}

fn sans_of(cert: &x509_parser::certificate::X509Certificate<'_>) -> BTreeSet<String> {
    use x509_parser::extensions::GeneralName;
    let mut found = BTreeSet::new();
    if let Ok(Some(ext)) = cert.subject_alternative_name() {
        for name in &ext.value.general_names {
            match name {
                GeneralName::DNSName(d) => {
                    found.insert(format!("DNS:{d}"));
                }
                GeneralName::IPAddress(b) => {
                    let ip = match b.len() {
                        4 => IpAddr::V4(Ipv4Addr::new(b[0], b[1], b[2], b[3])),
                        16 => IpAddr::V6(Ipv6Addr::from(<[u8; 16]>::try_from(*b).unwrap_or_default())),
                        _ => continue,
                    };
                    found.insert(format!("IP:{ip}"));
                }
                _ => {}
            }
        }
    }
    found
}

/// The SAN tokens a leaf carries, read from the certificate itself; empty when unreadable.
pub fn cert_sans(cert_path: &Path) -> BTreeSet<String> {
    with_cert(cert_path, sans_of).unwrap_or_default()
}

/// Days until the certificate expires, floored; negative once expired, `None` when unreadable.
pub fn cert_days_left(cert_path: &Path) -> Option<i64> {
    let not_after = with_cert(cert_path, |c| c.validity().not_after.timestamp()).ok()?;
    Some(((not_after as f64 - crate::now()) / 86_400.0).floor() as i64)
}

fn der_len(len: usize) -> Vec<u8> {
    if len < 0x80 {
        return vec![len as u8];
    }
    let bytes: Vec<u8> = len.to_be_bytes().into_iter().skip_while(|b| *b == 0).collect();
    let mut out = vec![0x80 | bytes.len() as u8];
    out.extend(bytes);
    out
}

fn der_tlv(tag: u8, body: &[u8]) -> Vec<u8> {
    let mut out = vec![tag];
    out.extend(der_len(body.len()));
    out.extend_from_slice(body);
    out
}

/// Wrap a PKCS#1 or SEC1 key in PKCS#8, which ring signs with.
fn wrap_pkcs8(alg_id: &[u8], inner: &[u8]) -> Vec<u8> {
    let mut body = vec![0x02, 0x01, 0x00];
    body.extend(der_tlv(0x30, alg_id));
    body.extend(der_tlv(0x04, inner));
    der_tlv(0x30, &body)
}

const ALG_RSA: &[u8] = &[0x06, 0x09, 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x01, 0x05, 0x00];
const ALG_EC_P256: &[u8] =
    &[0x06, 0x07, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x02, 0x01, 0x06, 0x08, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x03, 0x01, 0x07];
const ALG_EC_P384: &[u8] = &[0x06, 0x07, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x02, 0x01, 0x06, 0x05, 0x2b, 0x81, 0x04, 0x00, 0x22];

/// Load a private key PEM (PKCS#8, PKCS#1 RSA or SEC1 EC) as an rcgen key pair.
pub fn load_key_pair(path: &Path) -> Result<KeyPair, String> {
    let bytes = std::fs::read(path).map_err(|e| e.to_string())?;
    let key = rustls_pemfile::private_key(&mut bytes.as_slice())
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "no private key in the file".to_owned())?;
    let candidates: Vec<Vec<u8>> = match &key {
        PrivateKeyDer::Pkcs8(k) => vec![k.secret_pkcs8_der().to_vec()],
        PrivateKeyDer::Pkcs1(k) => vec![wrap_pkcs8(ALG_RSA, k.secret_pkcs1_der())],
        PrivateKeyDer::Sec1(k) => {
            vec![wrap_pkcs8(ALG_EC_P256, k.secret_sec1_der()), wrap_pkcs8(ALG_EC_P384, k.secret_sec1_der())]
        }
        _ => return Err("unsupported private key format".to_owned()),
    };
    let mut last = String::new();
    for der in candidates {
        match KeyPair::try_from(&PrivatePkcs8KeyDer::from(der)) {
            Ok(kp) => return Ok(kp),
            Err(e) => last = e.to_string(),
        }
    }
    Err(last)
}

fn verify_signed_by(
    leaf: &x509_parser::certificate::X509Certificate<'_>,
    ca: &x509_parser::certificate::X509Certificate<'_>,
) -> Result<(), String> {
    use ring::signature as sig;
    let spki = ca.public_key();
    let curve = spki.algorithm.parameters.as_ref().and_then(|p| p.as_oid().ok()).map(|o| o.to_id_string());
    let alg: &dyn sig::VerificationAlgorithm = match (leaf.signature_algorithm.algorithm.to_id_string().as_str(), curve.as_deref()) {
        ("1.2.840.113549.1.1.11", _) => &sig::RSA_PKCS1_2048_8192_SHA256,
        ("1.2.840.113549.1.1.12", _) => &sig::RSA_PKCS1_2048_8192_SHA384,
        ("1.2.840.113549.1.1.13", _) => &sig::RSA_PKCS1_2048_8192_SHA512,
        ("1.2.840.10045.4.3.2", Some("1.2.840.10045.3.1.7")) => &sig::ECDSA_P256_SHA256_ASN1,
        ("1.2.840.10045.4.3.3", Some("1.2.840.10045.3.1.7")) => &sig::ECDSA_P256_SHA384_ASN1,
        ("1.2.840.10045.4.3.2", Some("1.3.132.0.34")) => &sig::ECDSA_P384_SHA256_ASN1,
        ("1.2.840.10045.4.3.3", Some("1.3.132.0.34")) => &sig::ECDSA_P384_SHA384_ASN1,
        ("1.3.101.112", _) => &sig::ED25519,
        (other, _) => return Err(format!("unsupported signature algorithm {other}")),
    };
    sig::UnparsedPublicKey::new(alg, spki.subject_public_key.data.as_ref())
        .verify(leaf.tbs_certificate.as_ref(), leaf.signature_value.data.as_ref())
        .map_err(|_| "the signature does not verify".to_owned())
}

/// `None` when the leaf chains to `ca_path` and `key_path` is its key, else a short reason.
///
/// Copying the wrong pair onto a trust consumer is the easy provisioning mistake, and it
/// shows only as a client-side certificate error with nothing in the logs.
pub fn leaf_matches_ca(cert_path: &Path, key_path: &Path, ca_path: &Path) -> Option<String> {
    let chain = (|| -> Result<Vec<u8>, String> {
        let leaf_der = read_cert_der(cert_path)?;
        let ca_der = read_cert_der(ca_path)?;
        let (_, leaf) = x509_parser::parse_x509_certificate(&leaf_der).map_err(|e| e.to_string())?;
        let (_, ca) = x509_parser::parse_x509_certificate(&ca_der).map_err(|e| e.to_string())?;
        if leaf.issuer().as_raw() != ca.subject().as_raw() {
            return Err(format!("its issuer {} is not {}", leaf.issuer(), ca.subject()));
        }
        verify_signed_by(&leaf, &ca)?;
        Ok(leaf.public_key().subject_public_key.data.to_vec())
    })();
    let leaf_pub = match chain {
        Ok(p) => p,
        Err(why) => return Some(format!("does not chain to {}: {why}", ca_path.display())),
    };
    match load_key_pair(key_path) {
        Err(_) => Some(format!("{} is not readable as the private key for {}", key_path.display(), cert_path.display())),
        Ok(kp) if kp.public_key_raw() != leaf_pub.as_slice() => {
            Some(format!("{} is not the private key for {}", key_path.display(), cert_path.display()))
        }
        Ok(_) => None,
    }
}

// ---------------------------------------------------------------------------
// Minting
// ---------------------------------------------------------------------------

fn mint_err(what: &str) -> impl FnOnce(rcgen::Error) -> CertError + '_ {
    move |e| CertError::Mint(format!("{what}: {e}"))
}

fn random_serial() -> SerialNumber {
    let mut b: [u8; 16] = rand::random();
    b[0] = (b[0] & 0x7f) | 0x01;
    SerialNumber::from(b.to_vec())
}

fn dn(cn: &str) -> DistinguishedName {
    let mut dn = DistinguishedName::new();
    dn.push(DnType::CommonName, cn);
    dn
}

fn san_type(tok: &str) -> Result<SanType, CertError> {
    if let Some(v) = tok.strip_prefix("IP:") {
        let ip = v.parse::<IpAddr>().map_err(|_| CertError::Mint(format!("SAN {tok}: not an IP address")))?;
        return Ok(SanType::IpAddress(ip));
    }
    if let Some(v) = tok.strip_prefix("DNS:") {
        let name = Ia5String::try_from(v).map_err(|e| CertError::Mint(format!("SAN {tok}: {e}")))?;
        return Ok(SanType::DnsName(name));
    }
    Err(CertError::Mint(format!("SAN {tok}: neither IP: nor DNS:")))
}

fn mint_root(ca_cert: &Path, ca_key: &Path) -> Result<(), CertError> {
    let kp = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).map_err(mint_err("root key"))?;
    let mut p = CertificateParams::default();
    p.distinguished_name = dn(CA_CN);
    p.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    p.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
    let now = time::OffsetDateTime::now_utc();
    p.not_before = now;
    p.not_after = now + time::Duration::days(CA_DAYS);
    p.serial_number = Some(random_serial());
    let cert = p.self_signed(&kp).map_err(mint_err("root"))?;
    write_atomic(ca_key, kp.serialize_pem().as_bytes(), 0o600)?;
    write_atomic(ca_cert, cert.pem().as_bytes(), 0o644)
}

fn mint_leaf(cert_dir: &Path, ca_cert: &Path, ca_key: &Path, sans: &[String]) -> Result<(), CertError> {
    let ca_pem = std::fs::read_to_string(ca_cert).map_err(io_err(ca_cert))?;
    let ca_params = CertificateParams::from_ca_cert_pem(&ca_pem).map_err(mint_err("reading the root"))?;
    let ca_kp = load_key_pair(ca_key).map_err(|e| CertError::Mint(format!("reading {}: {e}", ca_key.display())))?;
    let issuer = ca_params.self_signed(&ca_kp).map_err(mint_err("loading the root"))?;

    let kp = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).map_err(mint_err("leaf key"))?;
    let mut p = CertificateParams::default();
    p.distinguished_name = dn(LEAF_CN);
    p.subject_alt_names = sans.iter().map(|s| san_type(s)).collect::<Result<_, _>>()?;
    p.is_ca = IsCa::ExplicitNoCa;
    p.key_usages = vec![KeyUsagePurpose::DigitalSignature];
    p.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
    p.use_authority_key_identifier_extension = true;
    let now = time::OffsetDateTime::now_utc();
    p.not_before = now;
    p.not_after = now + time::Duration::days(LEAF_DAYS);
    p.serial_number = Some(random_serial());
    let cert = p.signed_by(&kp, &issuer, &ca_kp).map_err(mint_err("leaf"))?;

    let key = cert_dir.join("key.pem");
    let crt = cert_dir.join("cert.pem");
    write_atomic(&key, kp.serialize_pem().as_bytes(), 0o600)?;
    write_atomic(&crt, cert.pem().as_bytes(), 0o644)?;
    // The sidecar the Python front compared, kept so a rollback reading this dir does not re-cut.
    write_atomic(&cert_dir.join(".san"), sans.join(",").as_bytes(), 0o644)
}

/// Why the CA holder must re-cut its leaf, or `None` when the one on disk serves.
fn remint_reason(cert: &Path, key: &Path, ca_cert: &Path, sans: &[String]) -> Option<String> {
    if !cert.exists() || !key.exists() {
        return Some("no leaf".to_owned());
    }
    let actual = cert_sans(cert);
    if san_set(&actual.iter().collect::<Vec<_>>()) != san_set(sans) {
        return Some(format!("the leaf carries {}", actual.into_iter().collect::<Vec<_>>().join(",")));
    }
    match cert_days_left(cert) {
        None => return Some("the leaf is unreadable".to_owned()),
        Some(d) if d < RENEW_DAYS => return Some(format!("the leaf has {d} day(s) left")),
        Some(_) => {}
    }
    leaf_matches_ca(cert, key, ca_cert).map(|why| format!("the leaf {why}"))
}

/// Ensure a CA-signed leaf exists under `cert_dir`, minting only what is missing.
///
/// `ca_dir` defaults to [`ca_dir()`] and `sans` to [`default_sans`]. On the CA holder the leaf
/// is re-cut when its SAN set (compared as a set) or its validity requires. A node with
/// `ca.pem` but no `ca-key.pem` mints nothing: it validates the provisioned leaf and fails
/// closed without one.
pub fn ensure_certs(cert_dir: &Path, ca_dir_override: Option<&Path>, sans: Option<&[String]>) -> Result<CertInfo, CertError> {
    let ca_dir = ca_dir_override.map(Path::to_path_buf).unwrap_or_else(ca_dir);
    std::fs::create_dir_all(cert_dir).map_err(io_err(cert_dir))?;
    std::fs::create_dir_all(&ca_dir).map_err(io_err(&ca_dir))?;

    let ca_key = ca_dir.join("ca-key.pem");
    let ca_cert = ca_dir.join("ca.pem");
    let key = cert_dir.join("key.pem");
    let cert = cert_dir.join("cert.pem");
    let ca_pub = cert_dir.join("ca.pem");

    let sans: Vec<String> = match sans {
        Some(s) => s.to_vec(),
        None => default_sans(),
    };
    let san = sans.join(",");

    // The root is minted only when neither half is present.
    if !ca_cert.exists() && !ca_key.exists() {
        tracing::info!("minting local root CA at {} (install ca.pem on devices once)", ca_dir.display());
        mint_root(&ca_cert, &ca_key)?;
    } else if !ca_cert.exists() {
        return Err(CertError::TrustConsumer(format!(
            "{k} exists but {c} does not — the root's public half is missing and cannot be \
             regenerated from the key alone. Restore {c} from the node that holds the CA, or \
             remove {k} to mint a new (fleet-incompatible) root deliberately.",
            k = ca_key.display(),
            c = ca_cert.display()
        )));
    }
    // A rewrite on every start would move the plugin's stamp and restart it: a reload loop.
    let root = std::fs::read(&ca_cert).map_err(io_err(&ca_cert))?;
    if std::fs::read(&ca_pub).ok().as_deref() != Some(root.as_slice()) {
        write_atomic(&ca_pub, &root, 0o644)?;
    }

    let info = |san: String| CertInfo { cert: cert.clone(), key: key.clone(), ca: ca_pub.clone(), san };

    if !ca_key.exists() {
        return consumer_leaf(&cert, &key, &ca_cert, &ca_key, &sans, &san).map(info);
    }

    if let Some(why) = remint_reason(&cert, &key, &ca_cert, &sans) {
        tracing::info!("minting leaf cert signed by local CA (SAN={san}): {why}");
        mint_leaf(cert_dir, &ca_cert, &ca_key, &sans)?;
    }
    Ok(info(san))
}

/// The trust consumer: validate the provisioned leaf and report the SANs it actually carries.
fn consumer_leaf(cert: &Path, key: &Path, ca_cert: &Path, ca_key: &Path, sans: &[String], san: &str) -> Result<String, CertError> {
    if !cert.exists() || !key.exists() {
        return Err(CertError::TrustConsumer(format!(
            "{} is present without {}, so this node cannot sign — but no leaf was provisioned \
             at {} / {}. Mint one on the node holding the CA key with SAN={san} and copy both \
             files here.",
            ca_cert.display(),
            ca_key.display(),
            cert.display(),
            key.display()
        )));
    }
    if let Some(mismatch) = leaf_matches_ca(cert, key, ca_cert) {
        return Err(CertError::TrustConsumer(format!(
            "the provisioned leaf at {} {mismatch}. Copy the matching cert/key pair from the node holding {}.",
            cert.display(),
            ca_key.display()
        )));
    }
    let days = cert_days_left(cert);
    if let Some(d) = days.filter(|d| *d < 0) {
        return Err(CertError::TrustConsumer(format!(
            "the provisioned leaf at {} expired {} day(s) ago and cannot be re-minted here. Cut a \
             fresh one on the node holding {} with SAN={san} and copy it over.",
            cert.display(),
            -d,
            ca_key.display()
        )));
    }
    if let Some(d) = days.filter(|d| *d < RENEW_DAYS) {
        tracing::warn!(
            "the leaf at {} expires in {d} day(s) and can only be renewed on the node holding {}",
            cert.display(),
            ca_key.display()
        );
    }
    // Drift warns where absence refuses: an uncovered address is usually a new docker bridge
    // no client dials, and refusing would take every page down for it.
    let actual = cert_sans(cert);
    let have = san_set(&actual.iter().collect::<Vec<_>>());
    let missing: BTreeSet<&str> = sans.iter().filter(|s| !have.contains(&san_key(s))).map(String::as_str).collect();
    if !missing.is_empty() {
        tracing::warn!(
            "the provisioned leaf at {} does not cover {}; clients dialing those addresses will \
             see a certificate error. Re-mint on the node holding {} with SAN={san} and copy it here.",
            cert.display(),
            missing.into_iter().collect::<Vec<_>>().join(","),
            ca_key.display()
        );
    }
    tracing::info!("using pre-provisioned leaf at {} (no CA key on this node)", cert.display());
    // Report what the leaf carries, not what was asked: an operator reads this after a
    // client-side certificate error.
    let carried = actual.into_iter().collect::<Vec<_>>().join(",");
    Ok(if carried.is_empty() { san.to_owned() } else { carried })
}
