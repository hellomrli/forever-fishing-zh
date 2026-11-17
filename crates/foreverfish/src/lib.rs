pub mod assets;
pub mod audio;
pub mod cli;
pub mod config;
pub mod engine;
pub mod gui;
pub mod paths;
pub mod platform;
pub mod runner;
pub mod smoke;
pub mod vision;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
pub const TITLE: &str = "Forever 钓鱼助手 · Rust";
