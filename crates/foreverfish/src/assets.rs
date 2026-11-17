pub const DEFAULT_CONFIG: &str = include_str!("../assets/default_config.toml");
pub const FONT: &[u8] = include_bytes!("../assets/NotoSansSC-Regular.otf");
pub const ICONS: &[(u32, &[u8])] = &[
    (
        29,
        include_bytes!("../assets/interact_icon/interact_gold_29.png"),
    ),
    (
        42,
        include_bytes!("../assets/interact_icon/interact_gold_42.png"),
    ),
    (
        53,
        include_bytes!("../assets/interact_icon/interact_gold_53.png"),
    ),
    (
        79,
        include_bytes!("../assets/interact_icon/interact_gold_79.png"),
    ),
    (
        103,
        include_bytes!("../assets/interact_icon/interact_gold_103.png"),
    ),
];
pub const CLIPS: &[(&str, &[u8])] = &[
    (
        "fishingbobber_1",
        include_bytes!("../assets/clips/fishingbobber_1.wav"),
    ),
    (
        "fishingbobber_2",
        include_bytes!("../assets/clips/fishingbobber_2.wav"),
    ),
    (
        "fishingbobber_3",
        include_bytes!("../assets/clips/fishingbobber_3.wav"),
    ),
    ("fishbite", include_bytes!("../assets/clips/fishbite.wav")),
];
