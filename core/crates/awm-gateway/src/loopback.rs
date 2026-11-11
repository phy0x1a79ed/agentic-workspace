//! Which Unix account opened a TCP connection to the gateway, read from the kernel's socket tables.

use std::net::{IpAddr, SocketAddr};

use axum::extract::connect_info::Connected;
use axum::serve::IncomingStream;
use tokio::net::TcpListener;

/// The account behind one accepted connection. `uid` is None when no socket on this host matches:
/// a client off loopback, or one already gone.
#[derive(Clone, Copy, Debug)]
pub struct Peer {
    pub uid: Option<u32>,
}

impl Connected<IncomingStream<'_, TcpListener>> for Peer {
    fn connect_info(stream: IncomingStream<'_, TcpListener>) -> Self {
        let uid = stream.io().local_addr().ok().and_then(|server| uid_of(*stream.remote_addr(), server));
        Peer { uid }
    }
}

pub fn own_uid() -> u32 {
    // SAFETY: geteuid takes no arguments and cannot fail.
    unsafe { libc::geteuid() }
}

/// The owner of the client end of a connection: the socket whose local address is `client` and
/// whose remote address is `server`.
pub fn uid_of(client: SocketAddr, server: SocketAddr) -> Option<u32> {
    let (client, server) = (canonical(client), canonical(server));
    ["/proc/net/tcp", "/proc/net/tcp6"].iter().filter_map(|f| std::fs::read_to_string(f).ok()).find_map(|table| {
        table.lines().skip(1).filter_map(parse_row).find(|(local, remote, _)| *local == client && *remote == server).map(|row| row.2)
    })
}

fn canonical(a: SocketAddr) -> SocketAddr {
    SocketAddr::new(a.ip().to_canonical(), a.port())
}

/// One row of `/proc/net/tcp` or `tcp6`: local address, remote address, uid.
fn parse_row(line: &str) -> Option<(SocketAddr, SocketAddr, u32)> {
    let f: Vec<&str> = line.split_whitespace().collect();
    Some((addr(f.get(1)?)?, addr(f.get(2)?)?, f.get(7)?.parse().ok()?))
}

/// `0100007F:1E8B`: the address as 32-bit words printed in host byte order, then the port.
fn addr(field: &str) -> Option<SocketAddr> {
    let (hex, port) = field.split_once(':')?;
    let word = |i: usize| u32::from_str_radix(hex.get(i * 8..i * 8 + 8)?, 16).ok().map(u32::to_ne_bytes);
    let ip = match hex.len() {
        8 => IpAddr::from(word(0)?),
        32 => {
            let mut b = [0u8; 16];
            for i in 0..4 {
                b[i * 4..i * 4 + 4].copy_from_slice(&word(i)?);
            }
            IpAddr::from(b)
        }
        _ => return None,
    };
    Some(SocketAddr::new(ip.to_canonical(), u16::from_str_radix(port, 16).ok()?))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(target_endian = "little")]
    #[test]
    fn rows_parse_for_both_families() {
        let cases = [
            ("   0: 0100007F:1E8B 0100007F:D431 01 00000000:00000000 00:00000000 00000000  1003        0 1 1", Some(("127.0.0.1:7819", "127.0.0.1:54321", 1003))),
            ("   1: 00000000000000000000000001000000:1E8B 00000000000000000000000001000000:D431 01 0:0 0:0 0 997 0 2", Some(("[::1]:7819", "[::1]:54321", 997))),
            ("   2: 0000000000000000FFFF00000100007F:1E8B 0000000000000000FFFF00000100007F:D431 01 0:0 0:0 0 0 0 3", Some(("127.0.0.1:7819", "127.0.0.1:54321", 0))),
            ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid", None),
            ("   3: 0100007F 0100007F:D431 01 0:0 0:0 0 1 0 4", None),
        ];
        for (line, want) in cases {
            let want = want.map(|(l, r, u)| (l.parse().unwrap(), r.parse().unwrap(), u));
            assert_eq!(parse_row(line), want, "{line}");
        }
    }

    #[tokio::test]
    async fn a_loopback_client_is_this_process_account() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let server = listener.local_addr().unwrap();
        let _client = tokio::net::TcpStream::connect(server).await.unwrap();
        let (_conn, client) = listener.accept().await.unwrap();
        assert_eq!(uid_of(client, server), Some(own_uid()));
        assert_eq!(uid_of(server, client), Some(own_uid()), "the server's own end is found too");
        assert_eq!(uid_of("10.9.9.9:1".parse().unwrap(), server), None, "no row, no account");
    }
}
