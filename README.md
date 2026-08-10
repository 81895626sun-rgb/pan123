# pan123-monitor

监控本地/NAS 目录，将每个新文件/新目录**同时镜像到两个云盘**——123云盘（123pan）和 115网盘（115pan）。上传稳定后自动触发 QMediaSync 同步任务。设计为常驻守护进程（裸机或 Docker）运行于 SMB/CIFS 挂载的 NAS 共享目录之上。

## 功能

- **双云盘镜像**：一个文件自动上传到 123 + 115 两个网盘，路径按 `local_root → cloud_prefix` 映射
- **文件就绪检测**：NAS 无"复制完成"信号，通过两次采样文件大小（1s 间隔）判断文件是否停止增长
- **多级重试**：父目录未同步（10 次退避重试）与上传失败（3 次重试，写 `failed_tasks.txt` + Telegram 告警）两套独立语义
- **上传策略**：123 支持秒传/直传/128MB 分片并发；115 按 128MiB 分块，服务端报 `reuse` 自动切秒传
- **QMediaSync 联动**：批量上传结束后触发一次同步（120s 防抖，重置式定时器）
- **Telegram 通知**：成功/失败消息，MarkdownV2 转义内置
- **单线程上传**：上传严格单线程（云合规 + 本地带宽约束），目录/文件按优先级队列排序处理

## 架构：3-队列管道

```
watchdog 事件 ──► pending_queue ──► [debounce_worker_thread] ──► priority_queue ──► [priority_queue_worker] ──► upload_queue ──► [upload_task_worker] ──► 云盘 API
                 (防抖, 1000)                               (目录优先、浅层优先, 2000)          (每文件 UploadTask)                             (smart_upload / upload_file)
```

- `SimpleFileHandler`（watchdog）将 `on_created`/`on_moved` 路径推入 `pending_queue`（按 `pending_tasks` 去重），过滤临时文件（dotfile、`~`、`.tmp/.temp/.swp/.bak`、`-upload-tmp` 后缀）
- `debounce_worker_thread` 等文件停止增长后入 `priority_queue`；QMediaSync 触发也在这里
- `priority_queue_worker` 单线程，目录先于文件、浅层先于深层、同级 FIFO
- `upload_task_worker` 消费 `upload_queue` 执行实际上传
- 配置一律经 `config.py` 的 `Config.from_env()`（唯一 `os.getenv`/dotenv 来源），其余模块依赖注入

## 快速开始

### 本地开发

```bash
# 1. 复制配置
cp .env.example .env
# 编辑 .env 填入账号/路径（见下节）

# 2. 启动（需监控目录已存在）
python monitor.py
```

### Docker（生产形态）

镜像由 GitHub Action 构建，下载 `pan123-monitor.tar` 后：

```bash
docker load -i pan123-monitor.tar
# .env、SQLite 库、日志、MONITOR_DIR 均 bind-mount（见 docker-compose.yml）
docker compose up -d
```

> 部署注意：bind-mount 单文件要求宿主先存在该文件——首次部署前执行 `touch ./data/failed_tasks.txt`。

## 配置

所有运行时配置在 `.env`（见 `.env.example`）：

| 分组 | 键 | 说明 |
|---|---|---|
| 123 云盘 | `P123_PASSPORT` / `P123_PASSWORD` | 123 账号（手机号）与密码 |
| 115 云盘 | `P115_COOKIE` | 115 Cookie（浏览器 F12 → Cookies 获取，含 CID/KID/SEID/UID） |
| 本地目录 | `local_root` / `cloud_prefix` / `MONITOR_DIR` | 本地根目录、云端前缀、监控目录 |
| Telegram | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` / `TELEGRAM_PROXY_URL` | 通知（可选） |
| QMediaSync | `QMEDIASYNC_BASE_URL` / `QMEDIASYNC_API_KEY` / `QMEDIASYNC_PATH_IDS` / `QMEDIASYNC_DEBOUNCE_SECONDS` | 同步联动（可选），防抖默认 120s |

## 测试

纯单元测试，零网络、零真实凭证（假 env 注入 + mock）：

```bash
pip install -r requirements.txt pytest
pytest tests/
```

- CI（`.github/workflows/test.yml`）在每次 push / PR 自动运行以上测试
- 冒烟测试（登录校验、云盘 API 连通）是 `tests/test_115_api.py` / `tests/test_123_api.py`，**需要真实凭证**，需手动运行且 pytest 不会收集：
  ```bash
  python tests/test_115_api.py   # 校验 115 登录 + fs_files
  python tests/test_123_api.py   # 校验 123 登录 + 目录列表
  ```

## 目录结构

```
monitor.py            入口，管道编排
config.py             配置单一来源（Config.from_env）
pan123.py             上传策略（秒传/直传/分片）+ 路径转换
client.py             CloudClientManager 单例（双网盘客户端）
upload_worker.py      上传队列消费端（重试、失败记录、通知）
file_checker.py       文件就绪检测（两次采样）
cid_db_115.py         115 目录 ID 缓存（SQLite）
providers/            CloudProvider 协议（新增网盘只加一个类）
utils/                telegram_notifier / onestrm_notifier
```

## 排错

云盘 API / 上传 / 登录相关的历史错误与修复见 [`docs/troubleshooting.md`](docs/troubleshooting.md)。

## 技术栈

Python 3.12+（Dockerfile 固定 3.12-slim）、watchdog、p123client、p115client、p115oss、flask（115 内部使用）、SQLite 状态持久化。
