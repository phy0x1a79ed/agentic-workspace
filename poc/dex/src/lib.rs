//! dex core: strict-JSON envelope, closed error set, uniform router, hot-swappable local modules,
//! and line-oriented connections that carry the same envelope across process and network seams.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicPtr, AtomicU64, Ordering};
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

pub fn hex(b: &[u8]) -> String { b.iter().map(|x| format!("{x:02x}")).collect() }
pub fn unhex(s: &str) -> Vec<u8> { (0..s.len() / 2).map(|i| u8::from_str_radix(&s[2 * i..2 * i + 2], 16).unwrap()).collect() }
pub fn sha(b: &[u8]) -> String { hex(&Sha256::digest(b)) }

// ---------- contract ----------
#[derive(Serialize, Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct Args { pub a: u64, pub b: u64 }

#[derive(Serialize, Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct Env { pub v: u32, pub id: u64, pub from: String, pub to: String, pub selector: String, pub args: Args }

#[derive(Serialize, Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct Rep { pub v: u32, pub id: u64, pub out: Option<u64>, pub err: Option<String> }

#[derive(Debug, Clone, PartialEq)]
pub enum CallErr { DoesNotUnderstand, Timeout, Unavailable, Moved(String) }
impl CallErr {
    pub fn code(&self) -> String {
        match self {
            CallErr::DoesNotUnderstand => "doesNotUnderstand".into(),
            CallErr::Timeout => "timeout".into(),
            CallErr::Unavailable => "unavailable".into(),
            CallErr::Moved(w) => format!("moved:{w}"),
        }
    }
    pub fn parse(s: &str) -> CallErr {
        match s {
            "doesNotUnderstand" => CallErr::DoesNotUnderstand,
            "timeout" => CallErr::Timeout,
            s if s.starts_with("moved:") => CallErr::Moved(s[6..].into()),
            _ => CallErr::Unavailable,
        }
    }
}

pub trait Callee: Send + Sync {
    fn call(&self, env: &Env, deadline: Instant) -> Result<u64, CallErr>;
}

/// The only surface a caller sees. Placement lives in the table.
pub struct Router {
    table: RwLock<HashMap<String, Arc<dyn Callee>>>,
    resolver: Option<Box<dyn Fn(&str) -> Option<Arc<dyn Callee>> + Send + Sync>>,
    next: AtomicU64,
}
impl Router {
    pub fn new() -> Self { Router { table: RwLock::new(HashMap::new()), resolver: None, next: AtomicU64::new(1) } }
    pub fn with_resolver(mut self, f: impl Fn(&str) -> Option<Arc<dyn Callee>> + Send + Sync + 'static) -> Self {
        self.resolver = Some(Box::new(f)); self
    }
    pub fn bind(&self, name: &str, c: Arc<dyn Callee>) { self.table.write().unwrap().insert(name.into(), c); }
    pub fn unbind(&self, name: &str) { self.table.write().unwrap().remove(name); }
    pub fn call(&self, from: &str, to: &str, selector: &str, a: u64, b: u64, deadline: Instant) -> Result<u64, CallErr> {
        let env = Env { v: 1, id: self.next.fetch_add(1, Ordering::Relaxed), from: from.into(), to: to.into(), selector: selector.into(), args: Args { a, b } };
        let mut moved = false;
        loop {
            let c = self.table.read().unwrap().get(to).cloned().ok_or(CallErr::DoesNotUnderstand)?;
            match c.call(&env, deadline) {
                Err(CallErr::Moved(_)) if !moved && self.resolver.is_some() => {
                    moved = true;
                    match (self.resolver.as_ref().unwrap())(to) {
                        Some(n) => { self.bind(to, n); continue; }
                        None => return Err(CallErr::Unavailable),
                    }
                }
                r => return r,
            }
        }
    }
}

// ---------- in-process module (struct path) ----------
#[repr(C)]
pub struct Req { pub v: u32, pub selector: u32, pub id: u64, pub a: u64, pub b: u64 }
#[repr(C)]
pub struct RepC { pub v: u32, pub status: u32, pub id: u64, pub out: u64 }
type CallFn = unsafe extern "C" fn(*const Req, *mut RepC);

pub struct Loaded {
    _lib: libloading::Library,
    call: CallFn,
    sel: HashMap<String, u32>,
    pub version: u32,
    pub digest: String,
}
impl Loaded {
    /// Verify digest, dlopen from a path named by the digest, check ABI, run init under catch_unwind.
    pub fn load(path: &std::path::Path, want: &str) -> Result<Loaded, String> {
        let bytes = std::fs::read(path).map_err(|e| e.to_string())?;
        let got = sha(&bytes);
        if got != want { return Err(format!("digest mismatch: want {want:.12} got {got:.12}")); }
        unsafe {
            let lib = libloading::Library::new(path).map_err(|e| e.to_string())?;
            let abi: libloading::Symbol<extern "C" fn() -> u32> = lib.get(b"dex_abi").map_err(|e| e.to_string())?;
            if abi() != 1 { return Err("abi mismatch".into()); }
            let init: libloading::Symbol<extern "C" fn() -> u32> = lib.get(b"dex_init").map_err(|e| e.to_string())?;
            if init() != 0 { return Err("init failed (module reported panic)".into()); }
            let ver: libloading::Symbol<extern "C" fn() -> u32> = lib.get(b"dex_version").map_err(|e| e.to_string())?;
            let version = ver();
            let selfn: libloading::Symbol<unsafe extern "C" fn(*const u8, usize) -> u32> = lib.get(b"dex_selector").map_err(|e| e.to_string())?;
            let mut sel = HashMap::new();
            for n in ["calc", "count", "sleep", "version"] { sel.insert(n.to_string(), selfn(n.as_ptr(), n.len())); }
            let call: libloading::Symbol<CallFn> = lib.get(b"dex_call").map_err(|e| e.to_string())?;
            let call = *call;
            Ok(Loaded { _lib: lib, call, sel, version, digest: got })
        }
    }
    #[inline]
    pub fn invoke(&self, selector: &str, id: u64, a: u64, b: u64) -> Result<u64, CallErr> {
        let s = *self.sel.get(selector).ok_or(CallErr::DoesNotUnderstand)?;
        let q = Req { v: 1, selector: s, id, a, b };
        let mut r = RepC { v: 0, status: 0, id: 0, out: 0 };
        unsafe { (self.call)(&q, &mut r) };
        match r.status { 0 => Ok(r.out), 1 => Err(CallErr::DoesNotUnderstand), _ => Err(CallErr::Unavailable) }
    }
}

/// Swappable function table: an atomic pointer to the current module. Old modules stay mapped (leaked on purpose).
pub struct Host { cur: AtomicPtr<Loaded> }
impl Host {
    pub fn new(first: Loaded) -> Host { Host { cur: AtomicPtr::new(Box::into_raw(Box::new(first))) } }
    pub fn get(&self) -> &Loaded { unsafe { &*self.cur.load(Ordering::Acquire) } }
    pub fn swap(&self, next: Loaded) { self.cur.store(Box::into_raw(Box::new(next)), Ordering::Release); }
}
impl Callee for Host {
    fn call(&self, env: &Env, _d: Instant) -> Result<u64, CallErr> { self.get().invoke(&env.selector, env.id, env.args.a, env.args.b) }
}

// ---------- connections ----------
pub struct Conn {
    tx: Mutex<Box<dyn Write + Send>>,
    rx: Mutex<Receiver<String>>,
    child: Mutex<Option<Child>>,
}
impl Conn {
    pub fn from_parts(r: impl Read + Send + 'static, w: impl Write + Send + 'static, child: Option<Child>) -> Conn {
        let (s, rx) = channel();
        std::thread::spawn(move || {
            for l in BufReader::new(r).lines() { match l { Ok(l) => { if s.send(l).is_err() { break; } } Err(_) => break } }
        });
        Conn { tx: Mutex::new(Box::new(w)), rx: Mutex::new(rx), child: Mutex::new(child) }
    }
    pub fn unix(p: &str) -> std::io::Result<Conn> {
        let s = std::os::unix::net::UnixStream::connect(p)?;
        Ok(Conn::from_parts(s.try_clone()?, s, None))
    }
    pub fn spawn(cmd: &mut Command) -> std::io::Result<Conn> {
        let mut ch = cmd.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn()?;
        let (i, o) = (ch.stdin.take().unwrap(), ch.stdout.take().unwrap());
        Ok(Conn::from_parts(o, i, Some(ch)))
    }
    pub fn send(&self, line: &str) -> bool {
        let mut w = self.tx.lock().unwrap();
        w.write_all(line.as_bytes()).and_then(|_| w.write_all(b"\n")).and_then(|_| w.flush()).is_ok()
    }
    pub fn recv(&self, dl: Instant) -> Result<String, CallErr> {
        let left = dl.saturating_duration_since(Instant::now());
        match self.rx.lock().unwrap().recv_timeout(left) {
            Ok(l) => Ok(l),
            Err(RecvTimeoutError::Timeout) => Err(CallErr::Timeout),
            Err(RecvTimeoutError::Disconnected) => Err(CallErr::Unavailable),
        }
    }
    pub fn kill(&self) { if let Some(c) = self.child.lock().unwrap().as_mut() { let _ = c.kill(); let _ = c.wait(); } }
}
impl Drop for Conn { fn drop(&mut self) { self.kill(); } }

/// A callee reached over a Conn (unix socket or ssh stdio). Same envelope, JSON on the wire.
pub struct Remote {
    make: Box<dyn Fn() -> std::io::Result<Conn> + Send + Sync>,
    conn: Mutex<Option<Arc<Conn>>>,
}
impl Remote {
    pub fn new(make: impl Fn() -> std::io::Result<Conn> + Send + Sync + 'static) -> Remote { Remote { make: Box::new(make), conn: Mutex::new(None) } }
    pub fn kill(&self) { if let Some(c) = self.conn.lock().unwrap().as_ref() { c.kill(); } }
}
impl Callee for Remote {
    fn call(&self, env: &Env, deadline: Instant) -> Result<u64, CallErr> {
        let c = {
            let mut g = self.conn.lock().unwrap();
            if g.is_none() { *g = Some(Arc::new((self.make)().map_err(|_| CallErr::Unavailable)?)); }
            g.clone().unwrap()
        };
        let line = serde_json::to_string(env).unwrap();
        if !c.send(&line) { *self.conn.lock().unwrap() = None; return Err(CallErr::Unavailable); }
        loop {
            let l = match c.recv(deadline) {
                Ok(l) => l,
                Err(CallErr::Unavailable) => { *self.conn.lock().unwrap() = None; return Err(CallErr::Unavailable); }
                Err(e) => return Err(e),
            };
            let r: Rep = serde_json::from_str(&l).map_err(|_| CallErr::Unavailable)?;
            if r.id != env.id { continue; }
            return match (r.out, r.err) { (Some(o), _) => Ok(o), (_, Some(e)) => Err(CallErr::parse(&e)), _ => Err(CallErr::Unavailable) };
        }
    }
}

/// Serve envelopes from a stream against a callee until EOF.
pub fn serve(r: impl Read, mut w: impl Write, c: &dyn Callee) {
    for l in BufReader::new(r).lines() {
        let Ok(l) = l else { break };
        let rep = match serde_json::from_str::<Env>(&l) {
            Ok(e) => match c.call(&e, Instant::now() + Duration::from_secs(60)) {
                Ok(o) => Rep { v: 1, id: e.id, out: Some(o), err: None },
                Err(x) => Rep { v: 1, id: e.id, out: None, err: Some(x.code()) },
            },
            Err(_) => Rep { v: 1, id: 0, out: None, err: Some("doesNotUnderstand".into()) },
        };
        if writeln!(w, "{}", serde_json::to_string(&rep).unwrap()).and_then(|_| w.flush()).is_err() { break; }
    }
}

// ---------- store ----------
pub struct Store { pub dir: std::path::PathBuf }
impl Store {
    pub fn new(dir: &str) -> Store { std::fs::create_dir_all(dir).unwrap(); Store { dir: dir.into() } }
    pub fn path(&self, d: &str) -> std::path::PathBuf { self.dir.join(format!("{d}.so")) }
    pub fn has(&self, d: &str) -> bool { self.path(d).exists() }
    pub fn put(&self, bytes: &[u8]) -> String { let d = sha(bytes); std::fs::write(self.path(&d), bytes).unwrap(); d }
    pub fn list(&self) -> Vec<String> {
        let mut v: Vec<(std::time::SystemTime, String)> = std::fs::read_dir(&self.dir).unwrap().filter_map(|e| {
            let e = e.ok()?; let n = e.file_name().to_string_lossy().to_string();
            let d = n.strip_suffix(".so")?.to_string();
            Some((e.metadata().ok()?.modified().ok()?, d))
        }).collect();
        v.sort(); v.into_iter().map(|x| x.1).collect()
    }
    pub fn touch(&self, d: &str) { let _ = std::fs::File::options().write(true).open(self.path(d)).and_then(|f| f.set_modified(std::time::SystemTime::now())); }
    pub fn remove(&self, d: &str) { let _ = std::fs::remove_file(self.path(d)); }
}
