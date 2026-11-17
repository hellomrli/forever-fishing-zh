fn main() {
    if let Err(error) = foreverfish::cli::main() {
        eprintln!("{error:#}");
        std::process::exit(1);
    }
}
