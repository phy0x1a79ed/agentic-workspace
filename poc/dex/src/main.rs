//! node: callee / origin / attach / publish daemons plus the experiment drivers e1..e4.
use dex::*;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

fn arg(i: usize) -> String { std::env::args().nth(i).unwrap_or_default() }
fn ms(d: Duration) -> f64 { d.as_secs_f64() * 1000.0 }
fn us(d: Duration) -> f64 { d.as_secs_f64() * 1e6 }
fn dl(ms: u64) -> Instant { Instant::now() + Duration::from_millis(ms) }

fn load_host(store: &Store, so: &str) -> Host {
    let bytes = std::fs::read(so).unwrap();
    let d = store.put(&bytes);
    Host::new(Loaded::load(&store.path(&d), &d).unwrap())
}

fn pct(v: &mut Vec<Duration>, p: f64) -> Duration { v.sort(); v[((v.len() as f64 - 1.0) * p) as usize] }

fn main() {
    match arg(1).as_str() {
        "callee-unix" => {
            let st = Store::new(&format!("{}.store", arg(2)));
            let host = Arc::new(load_host(&st, &arg(3)));
            let _ = std::fs::remove_file(arg(2));
            let l = UnixListener::bind(arg(2)).unwrap();
            for s in l.incoming().flatten() {
                let h = host.clone();
                std::thread::spawn(move || serve(s.try_clone().unwrap(), s, &*h));
            }
        }
        "callee-stdio" => {
            let st = Store::new(&format!("{}.store", arg(3)));
            let host = load_host(&st, &arg(2));
            serve(std::io::stdin(), std::io::stdout(), &host);
        }
        "origin" => origin(&arg(2), &arg(3)),
        "attach" => {
            let s = UnixStream::connect(arg(2)).unwrap();
            let mut s2 = s.try_clone().unwrap();
            std::thread::spawn(move || { let _ = std::io::copy(&mut std::io::stdin(), &mut s2); });
            let _ = std::io::copy(&mut &s, &mut std::io::stdout());
        }
        "publish" => {
            // publish <sock> <name> <file> [tamper]
            let c = Conn::unix(&arg(2)).unwrap();
            let bytes = std::fs::read(arg(4)).unwrap();
            c.send(&json!({"op":"publish","name":arg(3),"hex":hex(&bytes),"tamper":arg(5)=="tamper"}).to_string());
            println!("{}", c.recv(dl(10000)).unwrap());
        }
        "gw" => {
            let (port, body, n) = (arg(2), arg(3), arg(4).parse::<usize>().unwrap());
            let mut v = vec![];
            let mut sz = 0;
            for _ in 0..n {
                let t = Instant::now();
                let mut s = std::net::TcpStream::connect(format!("127.0.0.1:{port}")).unwrap();
                write!(s, "POST /invoke HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}", body.len(), body).unwrap();
                let mut out = String::new();
                std::io::Read::read_to_string(&mut s, &mut out).unwrap();
                sz = out.len();
                v.push(t.elapsed());
            }
            println!("gateway n={n} resp={sz}B p50={:.2}ms p99={:.2}ms", ms(pct(&mut v.clone(), 0.5)), ms(pct(&mut v, 0.99)));
        }
        "e1" => e1(),
        "e2" => e2(),
        "e3" => e3(),
        "e4" => e4(),
        _ => eprintln!("unknown"),
    }
}

// ---------------------------------------------------------------- origin / worker
type W = Arc<Mutex<UnixStream>>;
struct Origin {
    st: Store,
    dir: String,
    tags: Mutex<HashMap<String, (String, u64)>>,
    subs: Mutex<Vec<(W, Vec<String>)>>,
}
fn wline(w: &W, v: &Value) -> bool { let mut g = w.lock().unwrap(); writeln!(g, "{v}").and_then(|_| g.flush()).is_ok() }

fn find_cargo() -> Option<String> {
    let mut dirs: Vec<String> = std::env::var("PATH").unwrap_or_default().split(':').map(String::from).collect();
    dirs.push(format!("{}/.cargo/bin", std::env::var("HOME").unwrap_or_default()));
    dirs.into_iter().map(|d| format!("{d}/cargo")).find(|p| std::path::Path::new(p).exists())
}
fn glibc() -> f64 {
    let o = Command::new("ldd").arg("--version").output().ok().map(|o| String::from_utf8_lossy(&o.stdout).lines().next().unwrap_or("").to_string()).unwrap_or_default();
    o.split_whitespace().last().and_then(|x| x.parse().ok()).unwrap_or(0.0)
}

fn origin(dir: &str, sock: &str) {
    let o = Arc::new(Origin { st: Store::new(&format!("{dir}/blobs")), dir: dir.into(), tags: Mutex::new(HashMap::new()), subs: Mutex::new(vec![]) });
    let _ = std::fs::remove_file(sock);
    let l = UnixListener::bind(sock).unwrap();
    for s in l.incoming().flatten() {
        let o = o.clone();
        std::thread::spawn(move || {
            let w: W = Arc::new(Mutex::new(s.try_clone().unwrap()));
            for line in BufReader::new(s).lines() {
                let Ok(line) = line else { break };
                let Ok(m) = serde_json::from_str::<Value>(&line) else { continue };
                o.op(&w, &m);
            }
        });
    }
}

impl Origin {
    fn op(&self, w: &W, m: &Value) {
        let name = m["name"].as_str().unwrap_or("").to_string();
        match m["op"].as_str().unwrap_or("") {
            "sub" => {
                let held = m["held"].as_str().unwrap_or("");
                let cur = self.tags.lock().unwrap().get(&name).cloned();
                match cur {
                    None => { wline(w, &json!({"ev":"unknown","name":name})); }
                    Some((d, seq)) if d == held => { wline(w, &json!({"ev":"notmodified","name":name,"seq":seq})); }
                    Some((d, seq)) => { wline(w, &json!({"ev":"current","name":name,"digest":d,"seq":seq})); }
                }
                self.subs.lock().unwrap().push((w.clone(), vec![name]));
            }
            "fetch" => {
                let d = m["digest"].as_str().unwrap_or("");
                match std::fs::read(self.st.path(d)) {
                    Ok(b) => wline(w, &json!({"ev":"blob","digest":d,"hex":hex(&b)})),
                    Err(_) => wline(w, &json!({"ev":"noblob","digest":d})),
                };
            }
            "publish" => {
                let mut b = unhex(m["hex"].as_str().unwrap());
                let d = sha(&b);
                if m["tamper"].as_bool() == Some(true) { let n = b.len() / 2; b[n] ^= 0xff; }
                std::fs::write(self.st.path(&d), &b).unwrap();
                let seq = { let mut t = self.tags.lock().unwrap(); let s = t.get(&name).map(|x| x.1).unwrap_or(0) + 1; t.insert(name.clone(), (d.clone(), s)); s };
                let ev = json!({"ev":"publish","name":name,"digest":d,"seq":seq});
                self.subs.lock().unwrap().retain(|(sw, names)| !names.contains(&name) || wline(sw, &ev));
                wline(w, &json!({"ev":"published","digest":d,"seq":seq}));
            }
            "bid" => {
                let it = &m["item"];
                let (cargo, g) = (find_cargo(), glibc());
                let max: f64 = it["glibc_max"].as_str().and_then(|x| x.parse().ok()).unwrap_or(99.0);
                let node = std::fs::read_to_string("/etc/hostname").unwrap_or_default().trim().to_string();
                if cargo.is_none() { wline(w, &json!({"ev":"decline","node":node,"reason":"no cargo"})); }
                else if g > max { wline(w, &json!({"ev":"decline","node":node,"reason":format!("glibc {g} > {max}")})); }
                else {
                    let est = std::fs::read_to_string(format!("{}/last_build_ms", self.dir)).ok().and_then(|x| x.trim().parse::<u64>().ok()).unwrap_or(60000);
                    wline(w, &json!({"ev":"bid","node":node,"est_ms":est,"glibc":g}));
                }
            }
            "award" => {
                let it = &m["item"];
                let src = unhex(m["src_hex"].as_str().unwrap());
                if sha(&src) != it["src_digest"].as_str().unwrap_or("") { wline(w, &json!({"ev":"failed","reason":"src digest mismatch"})); return; }
                let t0 = Instant::now();
                let bd = format!("{}/build-{}", self.dir, it["dirname"].as_str().unwrap_or("x"));
                let _ = std::fs::remove_dir_all(&bd);
                std::fs::create_dir_all(&bd).unwrap();
                std::fs::write(format!("{bd}.tar"), &src).unwrap();
                Command::new("tar").args(["xf", &format!("{bd}.tar"), "-C", &bd]).status().unwrap();
                let cargo = find_cargo().unwrap();
                let mut c = Command::new(cargo);
                c.current_dir(&bd).env("CARGO_TARGET_DIR", format!("{bd}/tgt")).args(["build", "--release", "-p", "modl"]);
                if let Some(f) = it["features"].as_str() { if !f.is_empty() { c.args(["--features", f]); } }
                if it["remap"].as_bool() == Some(true) { c.env("RUSTFLAGS", format!("--remap-path-prefix={bd}=/src")); }
                let ok = c.output().map(|o| o.status.success()).unwrap_or(false);
                if !ok { wline(w, &json!({"ev":"failed","reason":"build failed"})); return; }
                let so = std::fs::read(format!("{bd}/tgt/release/libmodl.so")).unwrap();
                let d = self.st.put(&so);
                let dur = t0.elapsed().as_millis();
                let _ = std::fs::write(format!("{}/last_build_ms", self.dir), dur.to_string());
                wline(w, &json!({"ev":"built","digest":d,"build_ms":dur as u64}));
            }
            _ => { wline(w, &json!({"ev":"error","reason":"unknown op"})); }
        }
    }
}

// ---------------------------------------------------------------- E1
fn stats(label: &str, r: &Router, to: &str, n: usize) {
    let mut v = Vec::with_capacity(n);
    let mut errs = 0;
    for _ in 0..n {
        let t = Instant::now();
        if r.call("e1", to, "calc", 1, 2, dl(5000)).is_err() { errs += 1; }
        v.push(t.elapsed());
    }
    println!("E1 {label:<14} n={n} errs={errs} p50={:.1}us p99={:.1}us", us(pct(&mut v.clone(), 0.5)), us(pct(&mut v, 0.99)));
}

struct MovedOnce(Arc<Host>, Mutex<bool>);
impl Callee for MovedOnce {
    fn call(&self, e: &Env, d: Instant) -> Result<u64, CallErr> {
        let mut m = self.1.lock().unwrap();
        if !*m { *m = true; return Err(CallErr::Moved("new".into())); }
        self.0.call(e, d)
    }
}

fn e1() {
    // e1 <so> <capella node> <capella so>
    let (so, cnode, cso) = (arg(2), arg(3), arg(4));
    let tmp = std::env::var("E1_TMP").unwrap_or("/tmp".into());
    let st = Store::new(&format!("{tmp}/e1store"));
    let host = Arc::new(load_host(&st, &so));
    let sock = format!("{tmp}/e1.sock");
    let mut child = Command::new(std::env::current_exe().unwrap()).args(["callee-unix", &sock, &so]).spawn().unwrap();
    while !std::path::Path::new(&sock).exists() { std::thread::sleep(Duration::from_millis(20)); }
    let r = Router::new().with_resolver({ let h = host.clone(); move |_| Some(h.clone() as Arc<dyn Callee>) });
    r.bind("local", host.clone());
    let sk = sock.clone();
    let proc_remote = Arc::new(Remote::new(move || Conn::unix(&sk)));
    r.bind("proc", proc_remote.clone());
    let (cn, cs) = (cnode.clone(), cso.clone());
    let net = Arc::new(Remote::new(move || Conn::spawn(Command::new("ssh").args(["capella", &cn, "callee-stdio", &cs, &format!("{cn}-e1")]))));
    r.bind("net", net.clone());
    // warm
    for to in ["local", "proc", "net"] { for _ in 0..50 { let _ = r.call("e1", to, "calc", 1, 2, dl(10000)); } }
    stats("local(struct)", &r, "local", 200000);
    stats("proc(unix+json)", &r, "proc", 20000);
    stats("net(ssh+json)", &r, "net", 2000);
    // one call site, same answers
    for to in ["local", "proc", "net"] { println!("E1 value {to} calc(1,2)={:?} version={:?}", r.call("e1", to, "calc", 1, 2, dl(5000)), r.call("e1", to, "version", 0, 0, dl(5000))); }
    // error set through the same path
    for to in ["local", "proc", "net"] {
        println!("E1 {to}: bad selector={:?} timeout={:?}", r.call("e1", to, "nope", 0, 0, dl(1000)), r.call("e1", to, "sleep", 300, 0, dl(50)));
    }
    println!("E1 unknown name={:?}", r.call("e1", "ghost", "calc", 1, 2, dl(100)));
    // moved -> re-resolve once
    let r2 = Router::new().with_resolver({ let h = host.clone(); move |_| Some(h.clone() as Arc<dyn Callee>) });
    r2.bind("svc", Arc::new(MovedOnce(host.clone(), Mutex::new(false))));
    println!("E1 moved then retry={:?}", r2.call("e1", "svc", "calc", 1, 2, dl(100)));
    // kill callees, same call path
    child.kill().unwrap(); let _ = child.wait();
    let t = Instant::now();
    println!("E1 killed proc: {:?} after {:.1}ms", r.call("e1", "proc", "calc", 1, 2, dl(2000)), ms(t.elapsed()));
    let t = Instant::now();
    println!("E1 killed proc (2nd call, reconnect refused): {:?} after {:.2}ms", r.call("e1", "proc", "calc", 1, 2, dl(2000)), ms(t.elapsed()));
    net.kill();
    let t = Instant::now();
    println!("E1 killed net: {:?} after {:.1}ms", r.call("e1", "net", "calc", 1, 2, dl(5000)), ms(t.elapsed()));
    // strict contract: unknown key is refused
    let bad = r#"{"v":1,"id":1,"from":"x","to":"y","selector":"calc","args":{"a":1,"b":2},"extra":1}"#;
    println!("E1 unknown key refused={}", serde_json::from_str::<Env>(bad).is_err());
    let _ = proc_remote;
}

// ---------------------------------------------------------------- E2
struct SyncSt { held: Option<String>, host: Option<Arc<Host>>, rejects: Vec<String>, swaps: Vec<(u32, f64)> }

/// Handle one origin event on `conn`: fetch by digest, verify, load, swap. Returns a description.
fn on_event(conn: &Conn, st: &Store, s: &mut SyncSt, line: &str, router: &Router) -> String {
    let v: Value = serde_json::from_str(line).unwrap();
    let ev = v["ev"].as_str().unwrap_or("");
    if ev == "notmodified" { return "304 notmodified".into(); }
    if ev != "publish" && ev != "current" { return format!("ignored {ev}"); }
    let d = v["digest"].as_str().unwrap().to_string();
    if s.held.as_deref() == Some(&d) { return "already held".into(); }
    let t0 = Instant::now();
    let mut blob = None;
    if !st.has(&d) {
        conn.send(&json!({"op":"fetch","digest":d}).to_string());
        loop {
            let l = conn.recv(dl(20000)).unwrap();
            let b: Value = serde_json::from_str(&l).unwrap();
            if b["ev"] == "blob" { blob = Some(unhex(b["hex"].as_str().unwrap())); break; }
            if b["ev"] == "noblob" { break; }
        }
        let Some(bytes) = blob else { return "fetch failed".into() };
        if sha(&bytes) != d { s.rejects.push(format!("{:.8}: digest mismatch", d)); return format!("REJECT {:.8} digest mismatch (v kept: {})", d, s.host.as_ref().map(|h| h.get().version).unwrap_or(0)); }
        std::fs::write(st.path(&d), &bytes).unwrap();
    }
    match Loaded::load(&st.path(&d), &d) {
        Err(e) => { s.rejects.push(format!("{:.8}: {e}", d)); format!("REJECT {:.8} {e} (v kept: {})", d, s.host.as_ref().map(|h| h.get().version).unwrap_or(0)) }
        Ok(l) => {
            let ver = l.version;
            match &s.host { None => { let h = Arc::new(Host::new(l)); router.bind("L", h.clone()); s.host = Some(h); } Some(h) => h.swap(l) }
            s.held = Some(d.clone());
            let el = ms(t0.elapsed());
            s.swaps.push((ver, el));
            format!("SWAP -> v{ver} {:.8} fetch+verify+load+swap {:.2}ms", d, el)
        }
    }
}

fn ssh_conn(args: Vec<String>) -> Conn { Conn::spawn(Command::new("ssh").args(args)).unwrap() }

fn e2() {
    // e2 <tmp> <remote dir>
    let (tmp, rd) = (arg(2), arg(3));
    let st = Store::new(&format!("{tmp}/e2store"));
    let router = Arc::new(Router::new());
    let attach = |rd: &str| ssh_conn(vec!["capella".into(), format!("{rd}/node"), "attach".into(), format!("{rd}/o.sock")]);
    let mut s = SyncSt { held: None, host: None, rejects: vec![], swaps: vec![] };
    let mut conn = attach(&rd);
    conn.send(&json!({"op":"sub","name":"L","held":Value::Null}).to_string());
    let l = conn.recv(dl(20000)).unwrap();
    println!("E2 initial: {}", on_event(&conn, &st, &mut s, &l, &router));

    // constant load
    let stop = Arc::new(AtomicBool::new(false));
    let mut ths = vec![];
    for _ in 0..4 {
        let (r, stop) = (router.clone(), stop.clone());
        ths.push(std::thread::spawn(move || {
            let (mut errs, mut n, mut trans) = (0u64, 0u64, vec![]);
            let mut last = u64::MAX;
            while !stop.load(Ordering::Relaxed) {
                match r.call("load", "L", "calc", 1, 2, dl(1000)) {
                    Ok(o) => { if o != last { trans.push(o); last = o; } }
                    Err(_) => errs += 1,
                }
                n += 1;
            }
            (n, errs, trans)
        }));
    }
    let publish = |name: &str, file: &str, tamper: bool| {
        let (rd, name, file) = (rd.clone(), name.to_string(), file.to_string());
        std::thread::spawn(move || {
            let mut c = Command::new("ssh");
            c.args(["capella", &format!("{rd}/node"), "publish", &format!("{rd}/o.sock"), &name, &format!("{rd}/{file}")]);
            if tamper { c.arg("tamper"); }
            c.output().unwrap();
        });
    };
    let wait_ev = |conn: &Conn| -> String { loop { let l = conn.recv(dl(20000)).unwrap(); if l.contains("\"publish\"") || l.contains("\"current\"") { return l; } } };
    let cnt = |r: &Router| r.call("m", "L", "count", 0, 0, dl(1000)).unwrap();

    std::thread::sleep(Duration::from_millis(300));
    println!("E2 count before swap = {}", cnt(&router));
    publish("L", "v2.so", false);
    let l = wait_ev(&conn);
    println!("E2 event v2: {}", on_event(&conn, &st, &mut s, &l, &router));
    println!("E2 count after swap  = {} (state lost)", cnt(&router));
    std::thread::sleep(Duration::from_millis(300));
    publish("L", "v4.so", true);
    let l = wait_ev(&conn);
    println!("E2 event tampered: {}", on_event(&conn, &st, &mut s, &l, &router));
    publish("L", "bad.so", false);
    let l = wait_ev(&conn);
    println!("E2 event panic-on-init: {}", on_event(&conn, &st, &mut s, &l, &router));
    println!("E2 still serving: calc={:?}", router.call("m", "L", "calc", 1, 2, dl(1000)));
    // offline catch-up
    conn.kill();
    drop(conn);
    let p = std::thread::spawn({ let (rd,) = (rd.clone(),); move || { Command::new("ssh").args(["capella", &format!("{rd}/node"), "publish", &format!("{rd}/o.sock"), "L", &format!("{rd}/v3.so")]).output().unwrap(); } });
    p.join().unwrap();
    println!("E2 published v3 while mizar link was down");
    conn = attach(&rd);
    conn.send(&json!({"op":"sub","name":"L","held":s.held}).to_string());
    let l = conn.recv(dl(20000)).unwrap();
    println!("E2 reconnect with held digest: {}", on_event(&conn, &st, &mut s, &l, &router));
    conn.kill(); drop(conn);
    conn = attach(&rd);
    conn.send(&json!({"op":"sub","name":"L","held":s.held}).to_string());
    let l = conn.recv(dl(20000)).unwrap();
    println!("E2 reconnect, nothing new: {}", on_event(&conn, &st, &mut s, &l, &router));
    std::thread::sleep(Duration::from_millis(300));
    stop.store(true, Ordering::Relaxed);
    for (i, t) in ths.into_iter().enumerate() {
        let (n, e, tr) = t.join().unwrap();
        println!("E2 load thread {i}: calls={n} errors={e} outputs-in-order={tr:?}");
    }
    println!("E2 swaps={:?} rejects={:?}", s.swaps, s.rejects);
}

// ---------------------------------------------------------------- E3
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize, PartialEq)]
enum Entry { Copy { digest: String }, Service { route: String } }

struct Node {
    st: Store,
    table: HashMap<String, Entry>,
    pinned: HashMap<String, Arc<Host>>,
    conn: Conn,
    cap: usize,
    tpath: String,
    stats: HashMap<&'static str, u32>,
}
impl Node {
    fn new(dir: &str, cap: usize, link: Vec<String>) -> Node {
        let tpath = format!("{dir}/table.json");
        let table = std::fs::read_to_string(&tpath).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default();
        Node { st: Store::new(&format!("{dir}/store")), table, pinned: HashMap::new(), conn: ssh_conn(link), cap, tpath, stats: HashMap::new() }
    }
    fn persist(&self) { std::fs::write(&self.tpath, serde_json::to_string(&self.table).unwrap()).unwrap(); }
    fn sub(&mut self, name: &str) -> String {
        let held = match self.table.get(name) { Some(Entry::Copy { digest }) => json!(digest), _ => Value::Null };
        self.conn.send(&json!({"op":"sub","name":name,"held":held}).to_string());
        let l = self.conn.recv(dl(20000)).unwrap();
        self.apply(&l)
    }
    /// An origin event updates the page table; nothing else moves until the name is used.
    fn apply(&mut self, line: &str) -> String {
        let v: Value = serde_json::from_str(line).unwrap();
        let (ev, name) = (v["ev"].as_str().unwrap_or(""), v["name"].as_str().unwrap_or("").to_string());
        match ev {
            "current" | "publish" => {
                self.table.insert(name.clone(), Entry::Copy { digest: v["digest"].as_str().unwrap().into() });
                self.pinned.remove(&name);
                self.persist();
                format!("{name}: table -> {:.8}", v["digest"].as_str().unwrap())
            }
            "notmodified" => format!("{name}: 304 (table kept)"),
            _ => format!("{name}: {ev}"),
        }
    }
    fn drain_events(&mut self) -> Vec<String> {
        let mut out = vec![];
        while let Ok(l) = self.conn.recv(Instant::now() + Duration::from_millis(300)) { out.push(self.apply(&l)); }
        out
    }
    fn evict(&mut self, keep: &str) {
        let hot: Vec<String> = self.pinned.values().map(|h| h.get().digest.clone()).collect();
        let referenced: Vec<String> = self.table.values().filter_map(|e| if let Entry::Copy { digest } = e { Some(digest.clone()) } else { None }).collect();
        let mut list = self.st.list();
        while list.len() > self.cap {
            // oldest-touched first; never a loaded module, never the blob being served; orphans (unreferenced) go first
            let pos = list.iter().position(|d| !referenced.contains(d) && !hot.contains(d) && d != keep).or_else(|| list.iter().position(|d| !hot.contains(d) && d != keep));
            let Some(p) = pos else { break };
            let d = list.remove(p);
            self.st.remove(&d);
            *self.stats.entry("evicted").or_default() += 1;
        }
    }
    fn call(&mut self, name: &str, pin: bool) -> (String, Result<u64, String>, Duration) {
        let t = Instant::now();
        let Some(Entry::Copy { digest }) = self.table.get(name).cloned() else { return ("none".into(), Err("doesNotUnderstand".into()), t.elapsed()) };
        if let Some(h) = self.pinned.get(name) { let r = h.get().invoke("calc", 1, 1, 2).map_err(|e| e.code()); return ("L0".into(), r, t.elapsed()); }
        let mut level = "L1";
        if !self.st.has(&digest) {
            level = "L3";
            self.conn.send(&json!({"op":"fetch","digest":digest}).to_string());
            let bytes = loop {
                let l = self.conn.recv(dl(20000)).unwrap();
                let v: Value = serde_json::from_str(&l).unwrap();
                if v["ev"] == "blob" { break unhex(v["hex"].as_str().unwrap()); }
                self.apply(&l);
            };
            if sha(&bytes) != digest { return ("L3".into(), Err("digest mismatch".into()), t.elapsed()); }
            std::fs::write(self.st.path(&digest), bytes).unwrap();
        }
        self.st.touch(&digest);
        let l = Loaded::load(&self.st.path(&digest), &digest).unwrap();
        let r = l.invoke("calc", 1, 1, 2).map_err(|e| e.code());
        if pin { self.pinned.insert(name.into(), Arc::new(Host::new(l))); } else { std::mem::forget(l); }
        self.evict(&digest);
        (level.into(), r, t.elapsed())
    }
}

fn e3() {
    // e3 <dir> <remote dir>
    let (dir, rd) = (arg(2), arg(3));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let link = || vec!["capella".to_string(), format!("{rd}/node"), "attach".into(), format!("{rd}/o.sock")];
    let mut n = Node::new(&dir, 3, link());
    for i in 1..=5 { println!("E3 sub {}", n.sub(&format!("n{i}"))); }
    // a service entry: route only, never copied
    n.table.insert("svc".into(), Entry::Service { route: "capella".into() });
    n.persist();
    println!("E3 store cap=3; sequence of calls (level, result, time):");
    for (name, pin) in [("n1", true), ("n1", true), ("n2", false), ("n3", false), ("n4", false), ("n5", false), ("n2", false), ("n1", true)] {
        let (lv, r, d) = n.call(name, pin);
        println!("E3   call {name}{} -> {lv} {r:?} {:.2}ms  store={} blobs", if pin { "(pinned)" } else { "" }, ms(d), n.st.list().len());
    }
    println!("E3 evicted={:?} svc entry still {:?}", n.stats.get("evicted"), n.table.get("svc"));
    // an event arrives while connected: table invalidated, next use refills
    let rdc = rd.clone();
    std::thread::spawn(move || { std::thread::sleep(Duration::from_millis(200)); Command::new("ssh").args(["capella", &format!("{rdc}/node"), "publish", &format!("{rdc}/o.sock"), "n3", &format!("{rdc}/n3b.so")]).output().unwrap(); });
    let l = n.conn.recv(dl(20000)).unwrap();
    println!("E3 event: {}", n.apply(&l));
    let (lv, r, d) = n.call("n3", false);
    println!("E3 n3 after event -> {lv} {r:?} {:.2}ms; store={} blobs (old n3 blob orphan evicted first)", ms(d), n.st.list().len());
    // restart: process state lost, disk state survives
    let before = n.table.clone();
    drop(n);
    let mut n2 = Node::new(&dir, 3, link());
    println!("E3 restart: table.json restored entries={} equal={} store blobs={} pinned={}", n2.table.len(), n2.table == before, n2.st.list().len(), n2.pinned.len());
    for i in 1..=5 { println!("E3 resub {}", n2.sub(&format!("n{i}"))); }
    let (lv, r, d) = n2.call("n3", false);
    println!("E3 n3 after restart -> {lv} {r:?} {:.2}ms", ms(d));
}

// ---------------------------------------------------------------- E4
fn e4() {
    // e4 <src tar> <local sock> <remote dir>
    let (tar, lsock, rd) = (arg(2), arg(3), arg(4));
    let src = std::fs::read(&tar).unwrap();
    let sd = sha(&src);
    let local = Conn::unix(&lsock).unwrap();
    let remote = ssh_conn(vec!["capella".into(), format!("{rd}/node"), "attach".into(), format!("{rd}/o.sock")]);
    let item = |dirname: &str, remap: bool| json!({"id":"job1","src_digest":sd,"features":"v2","remap":remap,"dirname":dirname,"triple":"x86_64-unknown-linux-gnu","glibc_max":"2.35"});
    let t0 = Instant::now();
    let it = item("a", false);
    let peers = [("mizar", &local), ("capella", &remote)];
    let mut winner: Option<(&str, &Conn, u64)> = None;
    for (n, c) in peers.iter() {
        c.send(&json!({"op":"bid","item":it}).to_string());
        let r: Value = serde_json::from_str(&c.recv(dl(30000)).unwrap()).unwrap();
        println!("E4 {n} replies: {r}");
        if r["ev"] == "bid" { let e = r["est_ms"].as_u64().unwrap(); if winner.map(|w| e < w.2).unwrap_or(true) { winner = Some((n, c, e)); } }
    }
    let (wn, wc, _) = winner.expect("no bidder");
    println!("E4 award -> {wn} (bid round {:.0}ms)", ms(t0.elapsed()));
    let build = |dirname: &str, remap: bool| -> (String, u64) {
        wc.send(&json!({"op":"award","item":item(dirname, remap),"src_hex":hex(&src)}).to_string());
        let r: Value = serde_json::from_str(&wc.recv(dl(600000)).unwrap()).unwrap();
        assert_eq!(r["ev"], "built", "{r}");
        (r["digest"].as_str().unwrap().to_string(), r["build_ms"].as_u64().unwrap())
    };
    let (d1, t1) = build("a", false);
    println!("E4 built {:.12} in {t1}ms", d1);
    wc.send(&json!({"op":"fetch","digest":d1}).to_string());
    let b: Value = serde_json::from_str(&wc.recv(dl(60000)).unwrap()).unwrap();
    let bytes = unhex(b["hex"].as_str().unwrap());
    let st = Store::new(&format!("{tar}.store"));
    println!("E4 fetched by digest: verify={}", sha(&bytes) == d1);
    st.put(&bytes);
    let l = Loaded::load(&st.path(&d1), &d1).unwrap();
    println!("E4 loaded on mizar: version={} calc(3,4)={:?}", l.version, l.invoke("calc", 1, 3, 4));
    let (d2, _) = build("a", false);
    let (d3, _) = build("b", false);
    let (d4, _) = build("cc", true);
    let (d5, _) = build("dd", true);
    println!("E4 repro: same dir rebuild equal={}", d1 == d2);
    println!("E4 repro: different build dir, no remap equal={}", d1 == d3);
    println!("E4 repro: different dirs, both --remap-path-prefix equal={}", d4 == d5);
    println!("E4 digests {:.10} {:.10} {:.10} {:.10} {:.10}", d1, d2, d3, d4, d5);
}
