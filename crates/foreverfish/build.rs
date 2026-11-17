fn main() {
    println!("cargo:rerun-if-changed=windows.manifest");
    // Linux can type-check the Windows backend without an SDK/resource compiler.
    // Shipping executables are built natively by the Windows CI job.
    if cfg!(windows) && std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        let mut resource = winresource::WindowsResource::new();
        resource
            .set("ProductName", "Forever 钓鱼助手 Rust")
            .set("FileDescription", "Forever 钓鱼助手（Rust 中文界面）")
            .set("LegalCopyright", "MIT · Richard Franklin / hellomrli")
            .set("FileVersion", env!("CARGO_PKG_VERSION"))
            .set("ProductVersion", env!("CARGO_PKG_VERSION"))
            .set_manifest(include_str!("windows.manifest"));
        resource.compile().expect("无法编译 Windows 版本资源");
    }
}
