//! cch: serves harness@1 over Claude Code sessions as tool `harness-cc`.

pub mod paths;
pub mod lineage;
pub mod sessions;

pub mod transcript;
pub mod watcher;

pub mod termscreen;
pub mod guards;
pub mod settings_guard;

pub mod lane;
pub mod tmux;
pub mod target;
pub mod permission_mode;

pub mod sender;
pub mod promises;
pub mod resume;
pub mod control;

pub mod verbs;
pub mod app;
