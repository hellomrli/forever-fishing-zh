"""Chinese product text and structured engine messages shared by GUI and CLI."""

STATE_NAMES = {
    "IDLE": "检查金色齿轮",
    "CASTING": "寻找金色齿轮",
    "WAIT_BITE": "等待咬钩声音",
    "INTERACT": "完成拾取后继续抛竿",
    "PAUSED": "已暂停",
    "STOPPED": "已停止",
}
CLIP_LABELS = {
    "fishingbobber_1": "咬钩音 1",
    "fishingbobber_2": "咬钩音 2",
    "fishingbobber_3": "咬钩音 3",
    "fishbite": "咬钩",
}
TEXT = {
    "unfocused": "游戏不在前台，请切回选中的客户端",
    "user_pause": "已手动暂停；按暂停快捷键或点击“继续”恢复",
    "cast": "已按 {key} 抛竿",
    "seek": "等待浮漂落水后寻找金色齿轮，最多等待 {seconds:g} 秒",
    "found": "已找到浮漂，位置 ({x}, {y})",
    "wait_bite": "金色齿轮存在，等待咬钩声音；声音命中立即按 {key} 起杆",
    "interact": "听到咬钩声音（{score:.2f}），已立即按 {key} 起杆",
    "loot": "等待 {seconds:g} 秒完成拾取，然后继续抛竿",
    "caught": "已观察到齿轮消失，估计收杆完成",
    "pickup_done": "起杆后的拾取等待结束，准备下一竿",
    "no_icon": "未找到交互图标；检查游戏设置和浮漂距离",
    "lost_icon": "浮漂图标持续丢失，准备重新抛竿",
    "no_bite": "{seconds:g} 秒内没有识别到咬钩声音，重新抛竿",
    "lure": "已按 {key} 使用鱼饵",
    "lure_wait": "等待 {seconds:g} 秒后抛竿，每 {minutes:g} 分钟补饵",
    "recast": "将在 {seconds:.1f} 秒后重新抛竿",
    "rest": "休息 {minutes:.1f} 分钟后继续",
    "time_up": "会话时间已到",
    "time_quit": "会话时间已到，准备退出游戏",
    "quit": "已按 {key} 执行退出宏",
    "quit_wait": "等待游戏窗口关闭，最多 {seconds:g} 秒",
    "closed": "游戏窗口已关闭",
    "stopping": "正在停止",
    "stats": (
        "抛竿 {casts} · 收杆确认（估计）{catches} · 超时/漏检 {misses} · "
        "声音识别 {bites} · {minutes:02d}:{seconds:02d}"
    ),
}


def msg(code: str, **params: object) -> str:
    return TEXT[code].format(**params)


def clip_label(name: str) -> str:
    return CLIP_LABELS.get(name, name)


def error_text(text: str) -> str:
    """Translate common backend/config failures while retaining useful diagnostic details."""
    replacements = {
        "World of Warcraft window not found; is the game running?": (
            "未找到游戏窗口，请启动客户端或重新选择窗口"
        ),
        "no loopback audio device found": "未找到系统回环设备，请检查声音输出设备",
        "no loopback devices found": "未找到系统回环设备",
        "audio capture stopped": "音频采集已中断",
        "must be 0 (forever) or positive": "必须为 0（不限时）或正数",
        "must be positive": "必须大于 0",
        "must be 0 or positive": "必须大于或等于 0",
        "must be in (0, 1]": "必须大于 0 且不超过 1",
        "must be between 10 and 500": "必须在 10–500 之间",
        "must be empty or a supported key name": "必须为支持的键位或留空",
        "must be a supported key name": "必须为支持的键位",
        "Config error in": "配置错误，位置",
        "unknown key": "未知配置字段",
        "unknown section": "未知配置分组",
        "ignored": "已忽略",
        "not supported": "不支持",
        "DISPLAY is not set": "未连接图形桌面（DISPLAY 未设置）",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text
