"""
应用配置 —— 单一来源。
所有模块的配置从 Config 对象获取，不再直接调用 os.getenv。
Config.from_env() 是唯一读取环境变量和 .env 文件的地方。
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv


def _parse_path_ids(raw: str | None) -> tuple[int, ...]:
    """解析逗号分隔的路径 ID 列表，如 '1,2' -> (1, 2)。"""
    if not raw:
        return (1, 2)
    try:
        return tuple(int(x) for x in raw.split(",") if x.strip())
    except ValueError:
        return (1, 2)


@dataclass(frozen=True)
class Config:
    """应用运行时配置，由 Config.from_env() 从 .env 构建。"""

    # ── 路径映射 ──
    local_root: str = ""
    cloud_prefix: str = ""
    monitor_dir: str = ""

    # ── 云盘凭证 ──
    p123_passport: str = ""
    p123_password: str = ""
    p115_cookie: str = ""

    # ── Telegram 通知（可选） ──
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_proxy_url: str | None = None

    # ── QMediaSync 同步（可选） ──
    qmediasync_base_url: str = ""
    qmediasync_api_key: str = ""
    qmediasync_path_ids: tuple[int, ...] = (1, 2)
    qmediasync_debounce_seconds: int = 120

    # ── 便捷属性 ──
    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def qmediasync_enabled(self) -> bool:
        return bool(self.qmediasync_base_url and self.qmediasync_api_key)

    @classmethod
    def from_env(cls) -> "Config":
        """从 .env / 环境变量构建 Config（唯一读 env 的地方）。"""
        load_dotenv()

        return cls(
            # 路径
            local_root=os.getenv("local_root") or "",
            cloud_prefix=os.getenv("cloud_prefix") or "",
            monitor_dir=os.getenv("MONITOR_DIR") or "",
            # 凭证
            p123_passport=os.getenv("P123_PASSPORT") or "",
            p123_password=os.getenv("P123_PASSWORD") or "",
            p115_cookie=os.getenv("P115_COOKIE") or "",
            # Telegram
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
            telegram_proxy_url=os.getenv("TELEGRAM_PROXY_URL") or None,
            # QMediaSync
            qmediasync_base_url=os.getenv("QMEDIASYNC_BASE_URL") or "",
            qmediasync_api_key=os.getenv("QMEDIASYNC_API_KEY") or "",
            qmediasync_path_ids=_parse_path_ids(os.getenv("QMEDIASYNC_PATH_IDS")),
            qmediasync_debounce_seconds=int(os.getenv("QMEDIASYNC_DEBOUNCE_SECONDS", "120")),
        )

    def __repr__(self) -> str:
        # 脱敏：密码和 cookie 显示为 ***
        def _mask(val: str) -> str:
            if not val:
                return repr(val)
            if len(val) <= 8:
                return "***"
            return repr(val[:4] + "***" + val[-4:])

        return (
            f"Config("
            f"local_root={self.local_root!r}, "
            f"cloud_prefix={self.cloud_prefix!r}, "
            f"monitor_dir={self.monitor_dir!r}, "
            f"p123_passport={self.p123_passport!r}, "
            f"p123_password={_mask(self.p123_password)}, "
            f"p115_cookie={_mask(self.p115_cookie)}, "
            f"telegram_bot_token={_mask(self.telegram_bot_token or '')}, "
            f"telegram_chat_id={self.telegram_chat_id!r}, "
            f"telegram_enabled={self.telegram_enabled}, "
            f"qmediasync_base_url={self.qmediasync_base_url!r}, "
            f"qmediasync_enabled={self.qmediasync_enabled}, "
            f"qmediasync_debounce_seconds={self.qmediasync_debounce_seconds}"
            f")"
        )