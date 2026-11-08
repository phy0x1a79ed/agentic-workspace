//! The journal's guards: its wire, its stored state, its write order, the cutover and rollback,
//! tag sync, privacy, and one path each through precedents, briefs and the inbox.

mod common;
mod cutover;
mod privacy;
mod product;
mod stored;
mod tagsync;
mod wire;
mod write_order;
