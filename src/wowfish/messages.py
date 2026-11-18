"""Chinese product text and structured engine messages shared by GUI and CLI."""

STATE_NAMES = {
    "IDLE": "待机",
    "CASTING": "寻找浮漂",
    "WAIT_BITE": "等待咬钩",
    "INTERACT": "确认收杆",
    "PAUSED": "已暂停",
    "STOPPED": "已停止",
}
CLIP_LABELS = {
    "fishingbobber_1": "水花 1",
    "fishingbobber_2": "水花 2",
    "fishingbobber_3": "水花 3",
    "fishbite": "咬钩",
}
TEXT = {
    "unfocused": "游戏不在前台，请切回选中的客户端",
    "user_pause": "已手动暂停；按暂停快捷键或点击“继续”恢复",
    "cast": "已按 {key} 抛竿",
    "seek": "正在寻找浮漂上的金色齿轮，最多等待 {seconds:g} 秒",
    "found": "已找到浮漂，位置 ({x}, {y})",
    "wait_bite": "等待水花和浮漂变化，确认后按 {key} 收杆",
    "interact": "已确认水花（{score:.2f}），按 {key} 收杆",
    "loot": "等待图标连续消失，确认收杆结果",
    "caught": "图标已连续消失，收杆确认完成",
    "no_icon": "未找到交互图标；检查游戏设置和浮漂距离",
    "lost_icon": "浮漂图标持续丢失，准备重新抛竿",
    "no_bite": "本次抛竿未确认咬钩",
    "no_loot": "收杆后图标仍存在，准备重新抛竿",
    "skip": "已跳过本次咬钩",
    "visual_off": (
        "画面确认已连续 {casts} 竿拦下水花且没有收杆，本次运行改为听到水花就收杆；"
        "可在设置中调低画面变化阈值，或关闭画面确认"
    ),
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
    "resized": "游戏窗口尺寸或缩放发生变化，重新获取浮漂",
    "stats": (
        "抛竿 {casts} · 收杆确认（估计）{catches} · 超时/漏检 {misses} · "
        "水花识别 {bites} · {minutes:02d}:{seconds:02d}"
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
