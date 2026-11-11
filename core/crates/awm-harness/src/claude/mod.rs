//! What awm knows about Claude Code's background sessions: the files Claude writes and reads,
//! its daemon's PTY protocol, and launching, trusting, pinning and stopping a job.

pub mod cli;
pub mod daemonlog;
pub mod detect;
pub mod home;
pub mod job;
pub mod kept;
pub mod launch;
pub mod live;
pub mod pins;
pub mod pty;
pub mod records;
pub mod roster;
pub mod trust;

pub use home::ClaudeHome;
