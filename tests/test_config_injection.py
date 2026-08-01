"""
Config 注入验证 —— 不依赖真实凭证、不依赖网络、不依赖 NAS。
验证 Config 从 from_env 到各模块的注入链路是否完整。
"""

import os
import sys
import threading
import tempfile
import time
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

_passed = 0
_failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}  — {detail}")


# ── 1. Config.from_env() 解析 ──
print("\n── Config.from_env() 解析 ──")
cfg = Config.from_env()

check("local_root", cfg.local_root == "/tmp/test_root")
check("cloud_prefix", cfg.cloud_prefix == "test/cloud")
check("monitor_dir", cfg.monitor_dir == "/tmp/test_monitor")
check("p123_passport", cfg.p123_passport == "13800000000")
check("p123_password", cfg.p123_password == "test_pw")
check("p115_cookie", cfg.p115_cookie == "test_cookie")
check("telegram_bot_token", cfg.telegram_bot_token == "123:abc")
check("telegram_chat_id", cfg.telegram_chat_id == "456")
check("qmediasync_base_url", cfg.qmediasync_base_url == "http://localhost:9999")
check("qmediasync_api_key", cfg.qmediasync_api_key == "test_key")
check("qmediasync_path_ids", cfg.qmediasync_path_ids == (1, 2, 3))
check("qmediasync_debounce_seconds", cfg.qmediasync_debounce_seconds == 60)
check("telegram_enabled", cfg.telegram_enabled is True)
check("qmediasync_enabled", cfg.qmediasync_enabled is True)

# ── 2. Config 脱敏 repr ──
print("\n── Config repr 脱敏 ──")
r = repr(cfg)
check("repr 不暴露密码原文", "test_pw" not in r)
check("repr 不暴露 cookie 原文", "test_cookie" not in r)
check("repr 包含 local_root", "/tmp/test_root" in r)
check("repr 包含 phone", "13800000000" in r)

# ── 3. Config 未配置时的回退 ──
print("\n── Config 未配置时回退 ──")
# 直接构造空 Config（不依赖 from_env，因为 .env 文件有真实值会覆盖）
cfg_empty = Config()
check("空 Config p123_passport 为空串", cfg_empty.p123_passport == "")
check("空 Config p115_cookie 为空串", cfg_empty.p115_cookie == "")
check("空 Config telegram_enabled=False", cfg_empty.telegram_enabled is False)
check("空 Config qmediasync_enabled=False", cfg_empty.qmediasync_enabled is False)

# ── 4. SimpleFileMonitor 接收 Config 注入 ──
print("\n── SimpleFileMonitor 接收 Config ──")
monitor = SimpleFileMonitor(
    path="/tmp/test_monitor",
    client_115=None,   # 不传真实 client
    client_123=None,
    config=cfg,
    sync_trigger=None,
)

check("monitor.config 正确存储", monitor.config is cfg)
check("monitor.config.local_root 可访问", monitor.config.local_root == "/tmp/test_root")
check("monitor.config.cloud_prefix 可访问", monitor.config.cloud_prefix == "test/cloud")
check("monitor.sync_trigger 为 None", monitor.sync_trigger is None)
check("upload_queue 已创建", isinstance(monitor.upload_queue, Queue))
check("pending_queue 已创建", isinstance(monitor.pending_queue, Queue))
check("priority_queue 已创建", monitor.priority_queue is not None)
check("upload_queue 容量 1000", monitor.upload_queue.maxsize == 1000)
check("pending_queue 容量 1000", monitor.pending_queue.maxsize == 1000)
check("priority_queue 容量 2000", monitor.priority_queue.maxsize == 2000)

# ── 5. convert_to_cloud_path 使用 Config 值 ──
print("\n── convert_to_cloud_path 使用 Config 值 ──")
from pan123 import convert_to_cloud_path  # noqa: E402

# 模拟：monitor 调用时传入 monitor.config.*
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
    check("convert_to_cloud_path 使用 config 值", "test/cloud" in cloud_path)

# ── 6. 模块导入链完整性 ──
print("\n── 模块导入链完整性 ──")
try:
    from utils.telegram_notifier import TelegramNotifier  # noqa: F401, E402
    check("TelegramNotifier 可导入", True)
except Exception as e:
    check("TelegramNotifier 可导入", False, str(e))

try:
    from utils.onestrm_notifier import OneStrmNotifier  # noqa: F401, E402
    check("OneStrmNotifier 可导入", True)
except Exception as e:
    check("OneStrmNotifier 可导入", False, str(e))

try:
    from upload_worker import upload_task_worker  # noqa: F401, E402
    check("upload_task_worker 可导入", True)
except Exception as e:
    check("upload_task_worker 可导入", False, str(e))

try:
    from client import CloudClientManager  # noqa: F401, E402
    check("CloudClientManager 可导入", True)
except Exception as e:
    check("CloudClientManager 可导入", False, str(e))

# ── 7. TelegramNotifier 从 Config 构造 ──
print("\n── TelegramNotifier 从 Config 构造 ──")
try:
    notifier = TelegramNotifier(cfg)
    check("TelegramNotifier(cfg) 构造成功", True)
    check("notifier.bot_token 正确", notifier.bot_token == "123:abc")
    check("notifier.chat_id 正确", notifier.chat_id == "456")
except Exception as e:
    check("TelegramNotifier(cfg) 构造成功", False, str(e))

# ── 8. OneStrmNotifier 从 Config 构造 ──
print("\n── OneStrmNotifier 从 Config 构造 ──")
try:
    trigger = OneStrmNotifier(cfg, notifier)
    check("OneStrmNotifier(cfg, notifier) 构造成功", True)
    check("trigger._base_url 正确", trigger._base_url == "http://localhost:9999")
    check("trigger._debounce_seconds 正确", trigger._debounce_seconds == 60)
    check("trigger._path_ids 正确", trigger._path_ids == [1, 2, 3])
except Exception as e:
    check("OneStrmNotifier(cfg, notifier) 构造成功", False, str(e))

# ── 9. OneStrmNotifier trigger_qmediasync 防抖不崩 ──
print("\n── OneStrmNotifier.trigger_qmediasync 防抖 ──")
try:
    result = trigger.trigger_qmediasync()
    check("trigger_qmediasync() 返回 True", result is True)
    check("防抖计时器已创建", trigger._debounce_timer is not None)
    check("防抖计时器是 daemon", trigger._debounce_timer.daemon is True)
    # 二次调用应重置计时器
    old_timer = trigger._debounce_timer
    result2 = trigger.trigger_qmediasync()
    check("二次调用重置计时器", trigger._debounce_timer is not old_timer)
    # 清理
    trigger._debounce_timer.cancel()
except Exception as e:
    check("trigger_qmediasync 不崩", False, str(e))

# ── 10. upload_task_worker 签名兼容 ──
print("\n── upload_task_worker 签名兼容 ──")
try:
    import inspect
    sig = inspect.signature(upload_task_worker)
    params = list(sig.parameters.keys())
    check("upload_task_worker 有 notifier 参数", "notifier" in params)
    check("upload_task_worker 有 on_final_failure 参数", "on_final_failure" in params)
except Exception as e:
    check("upload_task_worker 签名检查", False, str(e))

# ── 结果 ──
print(f"\n{'='*40}")
print(f"通过: {_passed}  失败: {_failed}")
if _failed:
    print("❌ 有失败项，Config 注入链路可能有问题")
    sys.exit(1)
else:
    print("✅ 全部通过，Config 注入链路完整")