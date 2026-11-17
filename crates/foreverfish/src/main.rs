#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

fn main() {
    if let Err(error) = foreverfish::cli::main() {
        eprintln!("{error:#}");
        if !std::env::args().any(|arg| arg == "--smoke-test") {
            foreverfish::platform::show_error(&format!("{error:#}"));
        }
        std::process::exit(1);
    }
}
