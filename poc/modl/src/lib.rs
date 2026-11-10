//! Demo dex module. Features pick the build variant: v2 changes calc, bad_init panics at init.
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};

#[repr(C)]
pub struct Req { pub v: u32, pub selector: u32, pub id: u64, pub a: u64, pub b: u64 }
#[repr(C)]
pub struct Rep { pub v: u32, pub status: u32, pub id: u64, pub out: u64 }

static CALLS: AtomicU64 = AtomicU64::new(0);

#[no_mangle]
pub extern "C" fn dex_abi() -> u32 { 1 }
#[no_mangle]
pub extern "C" fn dex_version() -> u32 { if cfg!(feature = "v2") { 2 } else { 1 } }
#[no_mangle]
pub extern "C" fn dex_init() -> u32 {
    std::panic::catch_unwind(|| { if cfg!(feature = "bad_init") { panic!("bad init"); } }).map(|_| 0).unwrap_or(1)
}

#[no_mangle]
pub unsafe extern "C" fn dex_selector(p: *const u8, n: usize) -> u32 {
    match std::str::from_utf8(std::slice::from_raw_parts(p, n)).unwrap_or("") {
        "calc" => 1, "count" => 2, "sleep" => 3, "version" => 4, _ => 0,
    }
}

#[no_mangle]
pub unsafe extern "C" fn dex_call(q: *const Req, r: *mut Rep) {
    let q = &*q;
    let id = q.id;
    if std::panic::catch_unwind(|| call(q, r)).is_err() { *r = Rep { v: 1, status: 2, id, out: 0 }; }
}

unsafe fn call(q: &Req, r: *mut Rep) {
    let n = CALLS.fetch_add(1, Relaxed) + 1;
    let (status, out) = match q.selector {
        1 => (0, if cfg!(feature = "v2") { q.a.wrapping_mul(q.b).wrapping_add(1000) } else { q.a.wrapping_add(q.b) }),
        2 => (0, n),
        3 => { std::thread::sleep(std::time::Duration::from_millis(q.a)); (0, 0) }
        4 => (0, dex_version() as u64),
        _ => (1, 0),
    };
    *r = Rep { v: 1, status, id: q.id, out };
}
