use foreverfish::{assets, VERSION};
use std::{fs, path::PathBuf};

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

#[test]
fn release_version_matches_lock_and_windows_manifest() {
    let root = root();
    let manifest: toml::Value =
        toml::from_str(&fs::read_to_string(root.join("Cargo.toml")).unwrap()).unwrap();
    assert_eq!(
        manifest["workspace"]["package"]["version"].as_str(),
        Some(VERSION)
    );
    let lock: toml::Value =
        toml::from_str(&fs::read_to_string(root.join("Cargo.lock")).unwrap()).unwrap();
    let packages: Vec<_> = lock["package"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|package| package["name"].as_str() == Some(env!("CARGO_PKG_NAME")))
        .collect();
    assert_eq!(packages.len(), 1);
    assert_eq!(packages[0]["version"].as_str(), Some(VERSION));
    let numeric = format!("{}.0", VERSION.split('-').next().unwrap());
    let windows = fs::read_to_string(root.join("crates/foreverfish/windows.manifest")).unwrap();
    assert!(windows.contains(&format!("assemblyIdentity version=\"{numeric}\"")));
}

#[test]
fn portable_package_inputs_and_matching_release_notes_exist() {
    for relative in [
        "LICENSE".to_string(),
        "THIRD_PARTY_NOTICES.md".to_string(),
        "docs/Rust使用说明.md".to_string(),
        "crates/foreverfish/assets/OFL-NotoSansSC.txt".to_string(),
        "crates/foreverfish/assets/FONT_SOURCE.md".to_string(),
        format!("docs/releases/rust-v{VERSION}.md"),
    ] {
        let path = root().join(&relative);
        assert!(path.is_file(), "Missing package input: {relative}");
        assert!(
            !fs::read_to_string(path).unwrap().trim().is_empty(),
            "{relative}"
        );
    }
}

#[test]
fn embedded_game_assets_match_the_maintained_python_assets() {
    let python = root().join("src/wowfish/assets");
    for &(size, bytes) in assets::ICONS {
        let path = python.join(format!("interact_icon/interact_gold_{size}.png"));
        assert!(bytes == fs::read(path).unwrap(), "Icon {size}");
    }
    for &(name, bytes) in assets::CLIPS {
        let path = python.join(format!("clips/{name}.wav"));
        assert!(bytes == fs::read(path).unwrap(), "Clip {name}");
    }
}
