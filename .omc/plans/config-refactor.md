# 配置收拢重构：os.getenv 散落 -> Config 单一来源

**分支**: `config-refactor`（已从 `qmediasync` 切出）
**目标**: 实现"这个值应该在一个地方定义，所有地方引用"。所有应用配置的 `os.getenv` 收敛到 `Config.from_env()`，其余模块全部依赖注入。
**约束**: 上传单线程不变；不碰业务逻辑（队列、重试、上传策略）；CI 只看 main，本分支不自动构建镜像。

---

## 设计要点

### `Config.from_env()` 是唯一读 env 的地方
- 读所有配置项，**不做必填校验**（校验留在使用点，保持现有行为：`_new_123_client` 校验凭证、`__main__` 校验 MONITOR_DIR）
- 这样冒烟测试只配部分凭证也能跑（`test_123_api.py` 只需 P123 凭证）
- `frozen=True`，敏感字段（password/cookie）在 `__repr__` 脱敏
- 新增 `telegram_enabled` / `qmediasync_enabled` 两个 property
- `QMEDIASYNC_DEBOUNCE_SECONDS` 从硬编码 120 提升为 env 可配（目前硬编码在 `onestrm_notifier.py:19`）

### 依赖注入链
```
__main__ -> Config.from_env()
        -> CloudClientManager(config)      # 凭证
        -> TelegramNotifier(config)        # 仅 telegram_enabled 时构造，否则 None
        -> OneStrmNotifier(config, notifier) # 仅 qmediasync_enabled 时构造，否则 None
        -> SimpleFileMonitor(..., config, sync_trigger)
        -> upload_task_worker(..., notifier)
```

### 可选通知器的处理（行为改进，非回归）
- **现状**: Telegram 未配置时 `TelegramNotifier()` 构造抛 ValueError，导致 `log_failed_task` 崩进 `upload_task_worker` 的全局 except；QMediaSync 未配置时 POST 到空 URL -> requests 异常 -> 发"失败"Telegram
- **改后**: 未配置则传 `None`，调用方 guard `if notifier is not None` / `if sync_trigger is not None`，静默跳过

### CloudClientManager 单例 + config 兼容
- `__new__(cls, config: Config | None = None)`：None 时回退 `Config.from_env()`
- 这样 `test_client.py` / `tests/test_*` 的 `CloudClientManager().get_client(...)` 不用改
- **footgun（记入 CLAUDE.md）**: 单例首次创建时的 config 生效，后续调用传 config 被忽略。各进程独立运行无影响

---

## 文件改动清单

### 1. `config.py`（新建）
- `@dataclass(frozen=True) class Config`：local_root, cloud_prefix, monitor_dir, p123_passport, p123_password, p115_cookie, telegram_bot_token/chat_id/proxy_url, qmediasync_base_url/api_key/path_ids/debounce_seconds
- `@classmethod from_env() -> Config`：唯一调用 `load_dotenv()` + `os.getenv` 的地方
- `telegram_enabled` / `qmediasync_enabled` property
- `__repr__` 脱敏（password/cookie 显示 `***`，passport 保留用于排查登录）

### 2. `client.py`
- import Config
- `CloudClientManager.__new__(cls, config=None)`：None 回退 `Config.from_env()`，存 `self._config`
- `_new_123_client`：`self._config.p123_passport` / `.p123_password` 替代 `os.getenv`（保留 `if not passport: raise ValueError`）
- `_new_115_client`：`self._config.p115_cookie` 替代 `os.getenv` + 文件读取逻辑保留（cookie 文件回退仍需要？——保留 env 优先、文件回退，但从 config 取 env 值）

### 3. `utils/telegram_notifier.py`
- 删除 import 时 `load_dotenv`（第 6/11 行）
- 构造器改为 `__init__(self, config: Config)`，从 config 取 token/chat_id/proxy（保留缺凭证 raise ValueError）
- 删除模块级便捷函数 `send_telegram_message`（无外部调用方了）
- `send_message` / `send_file` 不变

### 4. `utils/onestrm_notifier.py`
- 删除模块级 `QMEDIASYNC_*` env 读取（第 11-19 行）
- 改为实例化：`__init__(self, config: Config, notifier: TelegramNotifier | None = None)`，存 base_url/api_key/path_ids/debounce_seconds/notifier
- `_debounce_timer` / `_debounce_lock` 从类级移到实例
- `trigger_qmediasync` 改为实例方法（`cls` -> `self`），用 `self._debounce_seconds`
- `_trigger_qmediasync_sync` 用 `self._base_url`/`self._api_key`，Telegram 报告改用 `self._notifier.send_message(...)`（guard None）

### 5. `upload_worker.py`
- 删除模块级 `_telegram_notifier` / `_get_telegram_notifier`
- `upload_task_worker(..., on_final_failure=None, notifier=None)`：加 notifier 参数
- `log_failed_task(task, error_msg, notifier)` / `log_successful_task(task, file_id, notifier)`：notifier 为 None 时只写 `failed_tasks.txt`/日志，不发 Telegram
- `handle_upload_failure` 透传 notifier

### 6. `monitor.py`
- import Config, TelegramNotifier
- `SimpleFileMonitor.__init__(self, path, client_115, client_123, config, sync_trigger=None)`：存 `self.config` / `self.sync_trigger`
- `handle_file_creation_115` / `handle_file_creation_123` / `handle_dir_creation`：`os.getenv('local_root')` -> `monitor.config.local_root`，`cloud_prefix` 同理（共 6 处）
- `debounce_worker_thread`：`OneStrmNotifier.trigger_qmediasync()` -> `if monitor.sync_trigger is not None: monitor.sync_trigger.trigger_qmediasync()`
- `start_monitoring(path, client_115, client_123, config, notifier, sync_trigger)`：传 config 给 monitor，传 notifier 给 upload 线程
- `__main__`：`config = Config.from_env()`；校验 monitor_dir；构造 notifier（`if config.telegram_enabled`）/ sync_trigger（`if config.qmediasync_enabled`）；`CloudClientManager(config)`

### 7. `utils/__init__.py`
- 导出改为 `TelegramNotifier`, `OneStrmNotifier`（移除已删的 `send_telegram_message`）

### 8. `.env.example`
- QMediaSync 段新增 `QMEDIASYNC_DEBOUNCE_SECONDS=120` + 注释

### 9. `CLAUDE.md`
- Configuration 段：说明 `config.py` 是单一配置来源，`Config.from_env()` 唯一读 env
- 删除"QMediaSync debounce 是 onestrm_notifier.py 的常量"那句，改为 env 可配
- Critical invariants 段：加一条"所有配置走 Config，模块不再直接 os.getenv"

### 10. 冒烟测试（`test_client.py` / `tests/test_123_api.py` / `tests/test_115_api.py`）
- **不改**：`CloudClientManager()` 的 `config=None` 回退让它们继续工作
- `123test.py`（throwaway，.dockerignore）：不动

---

## 验证

无测试套件，验证手段：
1. `python tests/test_123_api.py` —— 123 连通性，验证 Config + client 链路
2. `python tests/test_115_api.py` —— 115 连通性
3. `python test_client.py` —— 双盘登录冒烟
4. `python monitor.py` 启动到客户端登录步（无真实凭证会失败在登录，但应越过 config 加载阶段，不报 import/env 错误）
5. 人工 review：`grep -rn "os.getenv" *.py utils/` 应只剩 `config.py` 一处（冒烟测试自己的 `os.getenv` 用于友好报错，可保留或一并清理）

## 行为变更总结
- Telegram 未配置：不再崩，静默跳过（改进）
- QMediaSync 未配置：不再发错误 Telegram，静默跳过（改进）
- `QMEDIASYNC_DEBOUNCE_SECONDS` 现可 env 配（原硬编码 120）
- 其余行为不变

## 不做
- 不动队列元组协议、重试逻辑、上传策略、线程模型（留给后续分支）
- 不改 `handle_file_creation_115/123` 合并（P0 另一项，独立分支）
- 不加测试框架
