//! What a harness plugin shares.
//!
//! [`contract`] is the platform-neutral half of harness@1 (`docs/redux/drafts/harness-contract.md`):
//! the target a verb names, the refusal shapes and the events a plugin reports to reflection.
//! [`claude`] (feature `claude`) is what awm knows about Claude Code's background sessions: the
//! records Claude writes, its daemon's PTY protocol, and launching, trusting, pinning and stopping
//! a job. It holds nothing a caller owns: no caller's state layout, policy or environment variable.

pub mod contract;

#[cfg(feature = "claude")]
pub mod claude;
