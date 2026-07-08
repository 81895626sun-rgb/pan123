# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Watches a local/NAS directory and **mirrors every new file and folder to two cloud drives simultaneously** — 123云盘 (123pan) and 115网盘 (115pan). After uploads settle, it triggers a QMediaSync sync job. Designed to run as a long-lived daemon (bare-metal or Docker) against an SMB/CIFS-mounted NAS share.

## Run / build / test

```bash
# Local dev — loads config from ./.env (dotenv). Requires the monitored dir to exist.
python monitor.py

# Smoke-test cloud client login for both pans (no test framework; this is the only "test")
python test_client.py

# Docker (production form). Image is built by the GitHub Action — download the .tar artifact, then:
docker load -i pan123-monitor.tar
# .env, the SQLite DBs, logs, and MONITOR_DIR are bind-mounted (see docker-compose.yml).
docker compose up -d
```

There is **no lint config and no test suite** — `test_client.py` is a manual login smoke test. Python: readme says 3.13.2; Dockerfile pins 3.12-slim.

## Configuration

All runtime config lives in `.env` (see `.env.example`). Keys: `P123_PASSPORT`/`P123_PASSWORD`, `P115_COOKIE`, `local_root`, `cloud_prefix`, `MONITOR_DIR`, `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`/`TELEGRAM_PROXY_URL`, `QMEDIASYNC_BASE_URL`/`QMEDIASYNC_API_KEY`/`QMEDIASYNC_PATH_IDS`. The QMediaSync debounce window (`QMEDIASYNC_DEBOUNCE_SECONDS=120`) is a constant in `utils/onestrm_notifier.py`.

## Architecture: the 3-queue pipeline

The system is a threaded pipeline. `monitor.py` is the entry point and wires everything together; understanding it requires reading `monitor.py` + `pan123.py` + `upload_worker.py` + `file_checker.py` together.

```
watchdog events ──► pending_queue ──► [debounce_worker_thread] ──► priority_queue ──► [priority_queue_worker] ──► upload_queue ──► [upload_task_worker] ──► cloud APIs
                   (debounce, 1000)                        (dirs-before-files,        (per-file UploadTask)                        (smart_upload / upload_file)
                                                           depth-ordered, 2000)
```

- **`SimpleFileHandler`** (watchdog `FileSystemEventHandler`) pushes raw `on_created`/`on_moved` paths into `pending_queue`, deduped by `pending_tasks`. Temp files (dotfiles, `~`, `.tmp/.temp/.swp/.bak/.orig/.backup/-upload-tmp`) are filtered — the `-upload-tmp` suffix is what the NAS emits mid-copy.
- **`debounce_worker_thread`** waits for files to stop growing (dirs skip debounce), then enqueues into `priority_queue`. This is where `OneStrmNotifier.trigger_qmediasync()` fires.
- **`priority_queue_worker`** is single-threaded and enforces ordering: directories before files, shallow depth before deep, FIFO within equal priority. Tuples are `(is_file, depth, seq, filepath)` for new events or `(is_file, depth, seq, filepath, task)` for retries — the worker branches on `len(item)`.
- **`upload_task_worker`** (`upload_worker.py`) consumes `upload_queue` and performs the actual upload via `pan123.smart_upload` (123) or `pan123.upload_file` (115).

### File-readiness detection
`FileGrowthChecker.check_growth` (`file_checker.py`) samples file size twice with a 1s sleep; equal-and-nonzero → `READY`, else `GROWING`. Returns `FileState` enum. This is used in **both** the debounce stage and the upload stage — there is no OS-level "copy complete" signal from the NAS.

### Dual-cloud fan-out & path resolution
- `convert_to_cloud_path(local_root, cloud_prefix, local_full_path)` maps a local absolute path to a POSIX cloud path by swapping `local_root` → `cloud_prefix`. Shared by both pans.
- **123**: `find_directory_path` walks the cloud tree via `client.fs_list_new` (paginated, no local cache) to resolve a parent `FileId`.
- **115**: `find_cid_by_parts` checks the SQLite cache (`cid_mapping.db` via `cid_db_115.py`) first, then falls back to paginated `client.fs_files`, caching each resolved segment. Newly created 115 dirs are written to the cache immediately in `handle_dir_creation`.

### Upload strategy (`pan123.py`)
- **123 `smart_upload`**: instant-transfer (秒传) → direct upload (skipped for >50MB) → chunked `upload_large_video` (128MB slices, 3 workers, resume via `upload_list`, 3 retry rounds).
- **115 `upload_file`**: ≤128MiB uses `upload_file_sample`-style path; >128MiB uses 128MiB chunks. Auto-switches to 秒传 when the server reports `reuse`.

### Retry semantics — two independent limits
- `monitor.py` `MAX_RETRIES = 10`: parent directory not yet synced. The task is re-enqueued into `priority_queue` with `available_after = time.time() + retries*2` (linear backoff); the worker skips items whose `available_after` hasn't passed.
- `upload_worker.py` `MAX_RETRIES = 3`: upload failure. Re-enqueued into `upload_queue`; exhausted tasks are logged to `failed_tasks.txt` (JSON lines) and a Telegram alert fires.

### Notifications
- `utils/telegram_notifier.py` — success/failure messages. Loads `.env` itself (path resolved relative to the package). MarkdownV2 escaping is built in.
- `utils/onestrm_notifier.py` — QMediaSync trigger, debounced 120s via a resettable `threading.Timer` so a batch upload fires one sync, not N.

### Client lifecycle
`client.py` `CloudClientManager` is a singleton holding one `P123Client` and one `P115Client`. `get_client('123'|'115')` tests the connection (`user_info` / `login_status`) and re-creates on failure. 123 logs in with passport+password; 115 uses a cookie string (env var or `115_cookie.txt`). `atexit` cleanup drops client references **without logging out** — calling 115 `logout()` terminates the cookie's login device server-side and invalidates it for the next run.

## Critical invariants (do not break these)

- **Never route a `task` with `pan_name="123"` to the 115 handler, or vice versa.** This causes CID/FileId cross-contamination (CID 串台) and crashes the upload. `priority_queue_worker` enforces this — preserve the branch when editing.
- **`handle_dir_creation` must NOT recurse / `os.listdir`.** Under pure watchdog mode, each child fires its own `on_created` event. Walking the dir mid-copy scans empty dirs and drops files. Only create the single directory in the cloud and cache its ID.
- **Docker `local_root` must equal `MONITOR_DIR`.** `docker-compose.yml` maps `${MONITOR_DIR}:${MONITOR_DIR}:ro` 1:1, so the in-container path and the env var must match exactly. For SMB shares, mount on the host first.
- **State files must persist across restarts**: `cid_mapping.db` (115 CID cache), `upload_state.db`, `upload.log`, `error.log`, `failed_tasks.txt`. All are gitignored; Docker bind-mounts them from `./data/`. Losing `cid_mapping.db` forces a full re-walk of the 115 tree on every file.

## Logs

`upload.log` (INFO+) and `error.log` (ERROR+ only) are written to cwd via a root-logger `FileHandler` configured in `monitor.py`. Both are append-mode and grow unbounded — `upload.log` is already multi-MB in the repo's working tree.

## 排错参考

云盘 API / 上传 / 登录相关的历史错误与修复记录见 [`docs/troubleshooting.md`](docs/troubleshooting.md)。遇到 123 返回 404 或非 JSON、115 上传 405、cookie 退出后失效、或 p115client / p115oss 升级后参数报错时，先查该文档对应章节。
