"""
Config 注入验证 —— 不依赖真实凭证、不依赖网络、不依赖 NAS。
验证 Config 从 from_env 到各模块的注入链路是否完整。

pytest 版（2026-08-10）：原模块级 run_all + check() 计数 改为 10 个独立 test_ 函数，
每个函数自包含构造，语义与旧 50 项完全等价。
"""
import os
import sys
import tempfile
from queue import Queue

# Windows 控制台默认 GBK，打印 emoji 会崩；强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 注入假环境变量（在 import 任何项目模块之前）
os.environ.update({
    "local_root": "/tmp/test_root",
    "cloud_prefix": "test/cloud",
    "MONITOR_DIR": "/tmp/test_monitor",
    "P123_PASSPORT": "13800000000",
    "P123_PASSWORD": "test_pw",
    "P115_COOKIE": "test_cookie",
    "TELEGRAM_BOT_TOKEN": "123:abc",
    "TELEGRAM_CHAT_ID": "456",
    "QMEDIASYNC_BASE_URL": "http://localhost:9999",
    "QMEDIASYNC_API_KEY": "test_key",
    "QMEDIASYNC_PATH_IDS": "1,2,3",
    "QMEDIASYNC_DEBOUNCE_SECONDS": "60",
})

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402
from monitor import SimpleFileMonitor, UploadTask  # noqa: E402


# ── 1. Config.from_env() 解析 ──
def test_config_from_env_parse():
    cfg = Config.from_env()
    assert cfg.local_root == "/tmp/test_root", "local_root"
    assert cfg.cloud_prefix == "test/cloud", "cloud_prefix"
    assert cfg.monitor_dir == "/tmp/test_monitor", "monitor_dir"
    assert cfg.p123_passport == "13800000000", "p123_passport"
    assert cfg.p123_password == "test_pw", "p123_password"
    assert cfg.p115_cookie == "test_cookie", "p115_cookie"
    assert cfg.telegram_bot_token == "123:abc", "telegram_bot_token"
    assert cfg.telegram_chat_id == "456", "telegram_chat_id"
    assert cfg.qmediasync_base_url == "http://localhost:9999", "qmediasync_base_url"
    assert cfg.qmediasync_api_key == "test_key", "qmediasync_api_key"
    assert cfg.qmediasync_path_ids == (1, 2, 3), "qmediasync_path_ids"
    assert cfg.qmediasync_debounce_seconds == 60, "qmediasync_debounce_seconds"
    assert cfg.telegram_enabled is True, "telegram_enabled"
    assert cfg.qmediasync_enabled is True, "qmediasync_enabled"


# ── 2. Config 脱敏 repr ──
def test_config_repr_masks_secrets():
    cfg = Config.from_env()
    r = repr(cfg)
    assert "test_pw" not in r, "repr 不暴露密码原文"
    assert "test_cookie" not in r, "repr 不暴露 cookie 原文"
    assert "/tmp/test_root" in r, "repr 包含 local_root"
    assert "13800000000" in r, "repr 包含 phone"


# ── 3. Config 未配置时的回退 ──
def test_config_empty_fallback():
    # 直接构造空 Config（不依赖 from_env，因为 .env 文件有真实值会覆盖）
    cfg_empty = Config()
    assert cfg_empty.p123_passport == "", "空 Config p123_passport 为空串"
    assert cfg_empty.p115_cookie == "", "空 Config p115_cookie 为空串"
    assert cfg_empty.telegram_enabled is False, "空 Config telegram_enabled=False"
    assert cfg_empty.qmediasync_enabled is False, "空 Config qmediasync_enabled=False"


# ── 4. SimpleFileMonitor 接收 Config 注入 ──
def test_monitor_receives_config():
    cfg = Config.from_env()
    monitor = SimpleFileMonitor(
        path="/tmp/test_monitor",
        providers={},   # empty providers dict
        config=cfg,
        sync_trigger=None,
    )
    assert monitor.config is cfg, "monitor.config 正确存储"
    assert monitor.config.local_root == "/tmp/test_root", "monitor.config.local_root 可访问"
    assert monitor.config.cloud_prefix == "test/cloud", "monitor.config.cloud_prefix 可访问"
    assert monitor.sync_trigger is None, "monitor.sync_trigger 为 None"
    assert isinstance(monitor.upload_queue, Queue), "upload_queue 已创建"
    assert isinstance(monitor.pending_queue, Queue), "pending_queue 已创建"
    assert monitor.priority_queue is not None, "priority_queue 已创建"
    assert monitor.upload_queue.maxsize == 1000, "upload_queue 容量 1000"
    assert monitor.pending_queue.maxsize == 1000, "pending_queue 容量 1000"
    assert monitor.priority_queue.maxsize == 2000, "priority_queue 容量 2000"


# ── 5. convert_to_cloud_path 使用 Config 值 ──
def test_convert_to_cloud_path_uses_config():
    from pan123 import convert_to_cloud_path  # noqa: E402

    with tempfile.TemporaryDirectory() as tmpdir:
        # 创建测试目录结构，local_root 和文件在同一临时目录下
        test_file_dir = os.path.join(tmpdir, "movies", "action")
        os.makedirs(test_file_dir, exist_ok=True)
        test_file = os.path.join(test_file_dir, "test.mp4")
        with open(test_file, "w") as f:
            f.write("dummy")

        cloud_path = convert_to_cloud_path(
            local_root=tmpdir,
            cloud_prefix="test/cloud",
            local_full_path=test_file,
        )
        assert "test/cloud" in cloud_path, "convert_to_cloud_path 使用 config 值"


# ── 6. 模块导入链完整性 ──
def test_module_import_chain():
    from utils.telegram_notifier import TelegramNotifier  # noqa: F401
    from utils.onestrm_notifier import OneStrmNotifier  # noqa: F401
    from upload_worker import upload_task_worker  # noqa: F401
    from client import CloudClientManager  # noqa: F401


# ── 7. TelegramNotifier 从 Config 构造 ──
def test_telegram_notifier_from_config():
    from utils.telegram_notifier import TelegramNotifier

    cfg = Config.from_env()
    notifier = TelegramNotifier(cfg)
    assert notifier.bot_token == "123:abc", "notifier.bot_token 正确"
    assert notifier.chat_id == "456", "notifier.chat_id 正确"


# ── 8. OneStrmNotifier 从 Config 构造 ──
def test_onestrm_notifier_from_config():
    from utils.telegram_notifier import TelegramNotifier
    from utils.onestrm_notifier import OneStrmNotifier

    cfg = Config.from_env()
    notifier = TelegramNotifier(cfg)
    trigger = OneStrmNotifier(cfg, notifier)
    assert trigger._base_url == "http://localhost:9999", "trigger._base_url 正确"
    assert trigger._debounce_seconds == 60, "trigger._debounce_seconds 正确"
    assert trigger._path_ids == [1, 2, 3], "trigger._path_ids 正确"


# ── 9. OneStrmNotifier trigger_qmediasync 防抖不崩 ──
def test_onestrm_trigger_debounce():
    from utils.telegram_notifier import TelegramNotifier
    from utils.onestrm_notifier import OneStrmNotifier

    cfg = Config.from_env()
    notifier = TelegramNotifier(cfg)
    trigger = OneStrmNotifier(cfg, notifier)

    result = trigger.trigger_qmediasync()
    assert result is True, "trigger_qmediasync() 返回 True"
    assert trigger._debounce_timer is not None, "防抖计时器已创建"
    assert trigger._debounce_timer.daemon is True, "防抖计时器是 daemon"
    # 二次调用应重置计时器
    old_timer = trigger._debounce_timer
    result2 = trigger.trigger_qmediasync()
    assert trigger._debounce_timer is not old_timer, "二次调用重置计时器"
    # 清理
    trigger._debounce_timer.cancel()


# ── 10. upload_task_worker 签名兼容 ──
def test_upload_task_worker_signature():
    import inspect
    from upload_worker import upload_task_worker

    sig = inspect.signature(upload_task_worker)
    params = list(sig.parameters.keys())
    assert "notifier" in params, "upload_task_worker 有 notifier 参数"
    assert "on_final_failure" in params, "upload_task_worker 有 on_final_failure 参数"
