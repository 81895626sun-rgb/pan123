# 排错记录 (Troubleshooting Log)

本文件记录 pan123/115 双云盘同步服务运行与升级中遇到的关键错误、根因和修复，供后续排查参考。
入口：`CLAUDE.md` →「排错参考」指向本文件。

> 实证：`uploadbak1.log`（2026-03 ~ 2026-07）里有 44 个文件因 #1（123 404）与 #2（115 405）同时触发，在两个云盘上都最终失败。

---

## 1. 123 云盘 API 返回 404 / 非 JSON（orjson 解析失败）

**症状**
- `123云盘连接测试失败: unexpected character: line 1 column 1 (char 0)`（orjson 把 HTML 当 JSON 解析）
- `fs_list_new` / `user_info` 请求 `https://www.123pan.com/b/api/...` 返回 `404 Not Found`
- 上游表现：`123网盘父目录未就绪` 反复重试 10 次后 `[任务放弃]`

**根因**
123pan 把 API 从 `www.123pan.com/b` 迁移到裸域名 `123pan.com`。p123client 写死的 `DEFAULT_BASE_URL = "https://www.123pan.com/b"` 失效：
- `www.123pan.com` 现在是纯前端站点，`/api/*` 返回 SPA HTML 壳（HTTP 200），orjson 解析直接炸。
- `/b/api/*` 部分接口返回 404。

**修复**（`client.py`）
`P123ClientFixed(P123Client)` 覆盖 `request()`：当 `base_url == DEFAULT_BASE_URL`（库默认值）时重定向到 `https://123pan.com`。登录走 `login.123pan.com`（`DEFAULT_LOGIN_BASE_URL`）不受影响。
```python
_WORKING_123_BASE_URL = "https://123pan.com"
class P123ClientFixed(P123Client):
    def request(self, url, method="GET", request=None, base_url=DEFAULT_BASE_URL, ...):
        if isinstance(base_url, str) and base_url == DEFAULT_BASE_URL:
            base_url = _WORKING_123_BASE_URL
        return super().request(url, method=method, ..., base_url=base_url, ...)
```
`_new_123_client` 用 `P123ClientFixed(passport, password)` 而非 `P123Client`。

**再犯排查**：抓 `user_info` 的实际响应体，是 HTML 就是 base_url 失效；检查 p123client 的 `DEFAULT_BASE_URL` 是否又被官方改回。

---

## 2. 115 上传 405（WAF 拦截）

**症状**
- `【上传遇阻】网盘:[115] ... code=405 method='POST' url='https://uplb.115.com/4.0/initupload.php?...'`
- 重试 3 次后 `❌ 【彻底放弃上传】`

**根因**
115 的 initupload 走阿里云 CDN WAF（Tengine），WAF 校验 `appversion`：
- p115oss **0.1.0.2** 发假 `appversion="99.99.99.99"` → WAF 直接 405。
- p115oss **0.1.0.3** 改用真实 `appversion="36.2.28"`，但其 `upload_init` 有 bug：`appversion = payload["appversion"]` 抛 KeyError（`upload_file` 构造的 payload 里没这字段），上传必崩。

**修复**（`requirements.txt` + `client.py`）
1. 锁 `p115oss==0.1.0.3`（不要退回 0.1.0.2 的假 appversion）。
2. `client.py` monkeypatch `p115oss.upload.upload_init`，调用前注入真实 `appversion="36.2.28"`：
```python
_orig_p115oss_upload_init = p115oss.upload.upload_init
def _p115oss_upload_init_fixed(payload, *, async_=False, **request_kwargs):
    if "appversion" not in payload:
        payload = {**payload, "appversion": "36.2.28"}
    return _orig_p115oss_upload_init(payload, async_=async_, **request_kwargs)
p115oss.upload.upload_init = _p115oss_upload_init_fixed
```

**再犯排查**：405 几乎都是 WAF 拦 appversion / UA / 签名。看 initupload 请求 payload 里 appversion 是不是真实的；p115oss 升级后重新确认 `upload_init` 签名。

---

## 3. 115 cookie 在脚本退出 / 测试后失效

**症状**
115 cookie 跑的时候能用，但脚本一退出（或跑完 `test_client.py`）cookie 就失效，下次启动登录不上。看着像「被服务端踢」，实际是代码自己调了 logout 把会话终结了。

**根因**
`CloudClientManager` 原有两处调 `client.logout()`：
- `_cleanup`（atexit，程序退出时）→ 已修。
- `reset_client`（强制重置，`test_client.py` 会调）→ 当时漏修，后补。

115 的 `logout()` 会 POST 到 115 注销接口，**服务端终止该 cookie 对应的登录设备**，cookie 当场失效。123 的 logout 不杀会话，不受影响。

**修复**（`client.py`）
`_cleanup` 与 `reset_client` 都不再调 `logout()`，只把本地引用置 None：
```python
@classmethod
def _cleanup(cls):
    for service, client in cls._clients.items():
        if client:
            cls._clients[service] = None
            logging.info(f"{service}云盘客户端已清理(保留会话,不logout)")

def reset_client(self, service):
    ...
    if self._clients[service]:
        self._clients[service] = None
        logging.info(f"{service}云盘客户端已重置(保留会话,不logout)")
```

**教训**：登录 115/123 要谨慎，**尽量复用已登录成功的 client 对象**，不要反复 login/logout。任何「退出时清理」「重置」的代码都别调 115 的 logout。

**再犯排查**：cookie 失效先 `grep -n "logout(" *.py` 排查代码自身，再怀疑服务端踢；`test_client.py` 的 `reset_client` 是高危点。

---

## 4. p115client `ensure_cookies` TypeError

**症状**：`P115Client(cookies=cookie, ensure_cookies=True)` 报 TypeError。
**根因**：p115client 0.0.9.3.x 移除了 `ensure_cookies` 参数。
**修复**（`client.py` `_new_115_client`）：`P115Client(cookies=cookie)`，去掉该参数。

## 5. p115client `upload_file` 的 `progress` 参数被移除

**症状**：`client.upload_file(..., progress=...)` 报 unexpected kwarg。
**根因**：p115client 0.0.9.3.x 移除了 `progress`，进度回调改用 `reporthook`。
**修复**（`pan123.py` `_upload_large_file`）：调用 `upload_file` 时去掉 `progress=`。

---

## 6. 其他整理

- **`upload_worker.py` 模块级 `get_client`**：原先模块顶层就创建 client（import 时登录），导致测试 / 退出行为混乱。改为 `upload_task_worker(client_123, client_115, upload_queue)`，由 `monitor.py` 传参。
- **Telegram 懒加载**：`utils/telegram_notifier` 改为首次用到时经 `_get_telegram_notifier()` 创建，避免 import 时连 Telegram。
- **凭证外移**：`123test.py` 的硬编码 passport/password、`utils/onestrm_notifier.py` 的 QMediaSync 配置，全部改读 `.env`。

---

## 7. 失败文件重拷后不再重传（dispatched_tasks 永不清空）

> **状态：已实施（A 回调版 + C）。** 2026-07-08 排查并落地。

**症状**
某文件上传最终失败（`❌ 【彻底放弃上传】`）后，手动重拷/重存该文件触发 watchdog，日志里反复出现 `系统事件捕获 [加入防抖队列]`，但**没有任何 `路径就绪` / `入优先级队列` / `开始处理任务` 后续**，文件永远不会再上传，直到重启守护进程。看着像"卡在防抖队列"，实际是防抖线程**静默丢弃**了重复事件。

**根因**
`monitor.py` 的 `dispatched_tasks`（line 68）是一个 `set`，记录所有已派发入优先级队列的路径，**只有 `add`（line 159），没有 `discard`/`remove`**——上传最终失败时 `upload_worker.handle_upload_failure` 也没有任何反馈回 monitor 去清它。

防抖线程 `debounce_worker_thread` 从 `pending_queue` 取出路径后，**第一件事**（line 128-131）就是查 `dispatched_tasks`，命中即静默 `discard` + `task_done` + `continue`，**不打日志**：
```python
if filepath in monitor.dispatched_tasks:   # 派发过就在里面，失败后也没清
    monitor.pending_tasks.discard(filepath)
    monitor.pending_queue.task_done()
    continue                               # 静默丢弃，日志无痕
```
`加入防抖队列` 是 watchdog 线程**入队前**打的（`monitor.py:95`），不代表入队后会被处理；丢弃发生在入队之后且无日志，所以症状有欺骗性。

事件链：首次派发（`add`）-> 上传失败（`dispatched_tasks` 未清）-> 重拷触发 `on_created` -> 入队（打"加入防抖队列"）-> 防抖线程取出命中 `dispatched_tasks` -> 静默丢弃 -> 永远到不了上传队列。重启进程是临时解（清空 `dispatched_tasks`），但根因不除则每个失败文件都会复发。

**修复方案（A 回调版 + C，已落地）**

**C：消除静默**——丢弃时打日志，使症状可观测。`monitor.py:128-131`：
```python
# before
if filepath in monitor.dispatched_tasks:
    monitor.pending_tasks.discard(filepath)
    monitor.pending_queue.task_done()
    continue
# after
if filepath in monitor.dispatched_tasks:
    logging.info(f"已派发过,忽略重复事件: {filepath}")
    monitor.pending_tasks.discard(filepath)
    monitor.pending_queue.task_done()
    continue
```

思路：让 `dispatched_tasks` **"失败感知"**--成功文件继续永久去重（符合本意），失败文件清掉放行。失败信号通过**回调**从 `upload_worker` 传回 monitor，不引入循环依赖。

**A：彻底放弃时回调清 `dispatched_tasks`**。只在 `handle_upload_failure` 的 `else`（彻底放弃）分支触发，重试中（`if` 分支）不清，不会误删。

`monitor.py` -- `SimpleFileMonitor.__init__` 加锁 + 新增方法，`start_monitoring` 启动上传线程时把方法作为回调传入：
```python
# __init__ 里
self.dispatched_lock = threading.Lock()

def mark_upload_failed(self, path):
    """上传最终失败后移除已派发标记，使重拷可重新检测重传。成功文件不受影响。"""
    with self.dispatched_lock:
        self.dispatched_tasks.discard(path)

# start_monitoring 里启动上传线程处（原 line 417），多传一个回调
from upload_worker import upload_task_worker
upload_thread = threading.Thread(
    target=upload_task_worker,
    args=(client_123, client_115, monitor.upload_queue, monitor.mark_upload_failed))
```

`upload_worker.py` -- 两个函数各加一个**可选**参数 `on_final_failure=None`，向后兼容：
```python
def upload_task_worker(client_123, client_115, upload_queue, on_final_failure=None):
    ...  # 调 handle_upload_failure 时把 on_final_failure 透传下去

def handle_upload_failure(upload_queue, task, error_msg=None, on_final_failure=None):
    if task.retries < MAX_RETRIES:
        task.retries += 1
        upload_queue.put(task)             # 重试：不清 dispatched_tasks
        logging.info(f"♻️ 【任务排队重试】({task.retries}/{MAX_RETRIES}) ...")
    else:                                   # 彻底放弃：回调清掉，放行重拷重试
        logging.error(f"❌ 【彻底放弃上传】...")
        if on_final_failure:
            on_final_failure(task.local_path)
        log_failed_task(task, error_msg)
        upload_queue.task_done()
```

**为什么用回调（而不是传 monitor 对象）**
- **无循环依赖**：`upload_worker` 不 import `monitor`（回调是 callable，鸭子类型）。现有 `monitor` 单向 lazy import `upload_worker` 的关系不变。
- **向后兼容**：`on_final_failure=None` 可选，`test_client.py` 等不传也不受影响。
- **改动面小**：2 个函数各加 1 个可选参数 + 1 个 monitor 方法 + 1 把锁。
- **线程安全 cheap**：`set.add/discard` 在 GIL 下本就原子，锁只兜底；跨线程写只有"彻底放弃"这一处，竞态最坏结果是多重传一次，正是期望行为。

**已知限制 / 注意**
- **只清"彻底放弃"，重试中不清**：1/3、2/3 失败走 `if` 分支，`dispatched_tasks` 不动，不会误删；只有 3/3 耗尽才回调清。重试期间重拷会被忽略（此时文件还在重试，可接受）。
- **成功文件仍永久去重**（符合本意）；本方案**覆盖不到"成功文件被删后重建"**（那种没失败过，`dispatched_tasks` 还在）--若将来需要，再叠加 mtime/TTL，属独立改进。
- `dispatched_tasks` 对成功文件仍只增不减（`set` 不变），长期运行累积；如需可加 LRU 淘汰，属另一独立改进。
- 目录走同一套：彻底放弃的目录任务也会被清，重拷可重试；云盘侧 `find_cid_by_parts` 命中已有 CID，无正确性问题。

**再犯排查**
- 现象"重拷失败文件不重传" -> `grep "已派发过" upload.log`（C 落地后）确认是否静默丢弃；若没这条日志说明根本没进防抖，查 watchdog。
- `grep -n "dispatched_tasks\|mark_upload_failed\|on_final_failure" monitor.py upload_worker.py` 确认回调链路接上：彻底放弃分支调了 `on_final_failure`，monitor 的 `mark_upload_failed` 真的 `discard`。
- 重启进程仍是临时解（清空 `dispatched_tasks`）；A 落地后失败文件重拷即可自动重传，无需重启。

---

## 通用排查思路

1. **先看 `error.log`（ERROR+）和 `upload.log`**，按 ERROR 消息前缀分类：`查询失败` / `请求失败` / `【彻底放弃上传】` / `[任务放弃]`。
2. **404 / 非 JSON** → 多半云盘 API 变更（域名 / 路径），查 p123client / p115client 的 base_url 与官方现行接口。
3. **405** → WAF 拦请求头 / payload，重点查 appversion、UA、签名；往往是客户端库版本问题。
4. **cookie 失效** → 先 `grep "logout("` 排查代码，再怀疑服务端踢；`test_client.py` 的 `reset_client` 是高危点。
5. **升级 p115client / p115oss / p123client 后**：检查 `P115Client` 构造参数、`upload_file` 签名、`upload_init` payload 字段——这几个库迭代快，参数常被删改。
6. **失败统计**：`【彻底放弃上传】网盘:[X]`（上传 3/3 耗尽）和 `[任务放弃]`（父目录 10/10 耗尽）是「最终失败」标记；统计去重时按云盘拆 + 求并集（同一文件可能两个云盘都失败）。日志大用 `grep`/`sed` 流式处理，别整份读进内存。
