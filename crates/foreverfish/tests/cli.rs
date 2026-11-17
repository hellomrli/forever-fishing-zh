use std::{fs, process::Command};

#[test]
fn relative_unicode_config_paths_can_be_created_read_and_recovered() {
    let directory = tempfile::tempdir().unwrap();
    let execute = |arguments: &[&str]| {
        let output = Command::new(env!("CARGO_BIN_EXE_ForeverFishing-Rust-CLI"))
            .current_dir(directory.path())
            .env("FOREVERFISH_HOME", directory.path().join("home"))
            .args(["--config", "中文配置.toml"])
            .args(arguments)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8(output.stdout).unwrap()
    };
    assert_eq!(execute(&["config", "--path"]).trim(), "中文配置.toml");
    let path = directory.path().join("中文配置.toml");
    assert!(path.is_file());
    let text = "# 用户配置\n[audio]\ndevice='中文扬声器'\n";
    fs::write(&path, text).unwrap();
    assert_eq!(execute(&["config"]), format!("{text}\n"));
    execute(&["config", "--restore-defaults"]);
    assert_eq!(
        fs::read_to_string(path.with_extension("toml.bak")).unwrap(),
        text
    );
    assert_eq!(
        fs::read_to_string(path).unwrap(),
        foreverfish::assets::DEFAULT_CONFIG
    );
}
