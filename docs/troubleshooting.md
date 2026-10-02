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

## 8. 115 上传偶发 401 + IndexError（p115oss 解析 bug）

> **状态：修复方案计划中（B + C），未实施。** 2026-07-08 排查确认。

**症状**
- 115 上传偶发 `HTTPError: HTTP Error 401: Unauthorized` 与 `IndexError: index out of bounds on dimension 1`（后者是 numpy 报错口吻，p115oss 用 numpy 做 115 OSS 协议加解密）。
- 两个错误都来自 p115client/p115oss 内部，`upload_worker.py` 用 `e.__cause__ or e.__context__` 取出来记成「上传异常根因」。
- 2026-07-08 实测：115 共 68 次大文件上传请求，7 个文件踩到 401/IndexError，6 个在 3 次重试内自愈，1 个（少侠逆袭攻略 S01E06.mkv）4 次全废（1×401 + 3×IndexError）耗尽重试 `彻底放弃`。

**根因**（用户判断）
- **401 = 网络 / 内部 token 抖动，不是 cookie 失活**。115 是 cookie 鉴权，无 passport+password，**代码里无法重新认证**（`reset_client`+`get_client` 只是重读同一个 cookie，换不出新会话）。瞬时 401 靠重试自愈；持续 401（cookie 真死）只能人去浏览器重抓 cookie。
- **IndexError = p115oss 客户端 bug**，高度怀疑是 401/异常响应的「次生灾害」：115 返回异常/空响应，p115oss 没防御直接索引响应结构 -> IndexError。S01E06 先 401 后 3 次 IndexError 印证此假设。若成立，401 和 IndexError 其实是同一个问题，被 p115oss 糟糕的错误处理掩盖成 IndexError。

**修复方案（B + C，计划中，未落地）**
- **B（先做，零风险）**：补全 115 异常的完整 traceback。现在 `upload_worker.py` 只记 `__cause__` 的 `[:300]` 字符串，看不到 p115oss 哪一行炸的。改成 `logging.error(..., exc_info=True)` 或 `traceback.format_exc()`，跑一两天确认 IndexError 真身（是不是 401 化身）+ 定位 p115oss 出错行。
- **C（后做，据 B 的数据）**：定位到 p115oss 0.1.0.3 的 bug 后，二选一 -- ① 像 `upload_init` 那样 monkeypatch（见 #2）；② 评估升级 p115oss/p115client（但要重新验证 `upload_init`/`upload_file` 签名，#2/#4/#5 都被升级坑过）。
- **401 侧**：不可自愈，只能加可观测性 -- 连续 N 次 401 就发明确告警「115 cookie 可能失效，需手动刷新」，甚至快速失败不浪费重试。属独立小改进。
- **D/E（可选）**：READY 态失败加指数退避 + 适度提 `MAX_RETRIES`（对 IndexError 瞬时有用，对持续 401 无用）；`failed_tasks.txt` 挂出宿主机（2026-10-02 已修：repo compose 已 bind-mount，且路径支持 `FAILED_TASKS_FILE` 环境变量覆盖指向持久目录；NAS 等自定义部署需自行加挂载）。

**再犯排查**
- 115 上传报 401/IndexError -> 先看是不是偶发（重试能过就是瞬时网络/token）；连续失败才怀疑 cookie。
- `grep "IndexError\|401" error.log` 统计频率和涉及文件；B 落地后看完整 traceback 确认 IndexError 源头在 p115oss 哪一行。
- 升级 p115oss/p115client 后：重验 `P115Client` 构造参数、`upload_file` 签名、`upload_init` payload（见 #2/#4/#5）。

---

## 9. 123 云盘 `file/list/new` 返回 400 "driveId is required"

> **状态：已修复（绕过 `fs_list_new` + 防御性解析）。** 2026-09-29 排查并落地。

**症状**
- `file/list/new` 请求返回 `{"code":400, "data":null, "message":"driveId is required"}`。
- 应用层表现：`查询失败: 'NoneType' object has no attribute 'get'`——因为响应里 `data` 存在但为 `null`，`response.get('data', {})` 返回的是 `None`（默认值 `{}` 只在键缺失时生效），下一行 `None.get(...)` 直接 AttributeError。然后 10 次重试后 `[任务放弃] | 原因=父目录查找失败`。
- `user_info` 接口正常返回 `code:0`（session/token 完全有效，**不是**鉴权问题）。
- 115 网盘完全不受影响（走秒传，不依赖 123 的 list 接口）。

**根因**
123 上游约 2026-09-07 前后把 `file/list/new` 的参数名校验改成**严格驼峰**（`driveId`、`orderBy`、`inDirectSpace`、`fileCategory`、`OnlyLookAbnormalFile`、`Page` 等），不再接受全小写。`p123client` 的 `fs_list_new` 实现里有这样一段：

```python
payload = dict_key_to_lower_merge(payload, {
    "driveId": 0, "limit": 100, "orderBy": "file_id",
    "orderDirection": "asc", "parentFileId": 0, "inDirectSpace": False,
    "fileCategory": 0, "event": event, "OnlyLookAbnormalFile": 0, "Page": 1,
})
return self.request("file/list/new", params=payload, ...)
```

`dict_key_to_lower_merge` 把传入 payload 与默认值里所有驼峰键**统一打成小写**（`driveId` → `driveid`、`orderBy` → `orderby`、`inDirectSpace` → `indirectspace`、`fileCategory` → `filecategory`、`OnlyLookAbnormalFile` → `onlylookabnormalfile`、`Page` → `page`），然后作为 query params 发出。9/7 之前服务端对大小写不敏感所以能跑通，之后严格校验就返回 `400 "driveId is required"`，`data` 为 `null`。

**关键反证（无 token 实测 `123pan.com/api/file/list/new`）**：

| 请求 | 响应 |
|---|---|
| 不带 `Authorization` | `{"code":20101,"message":"未登录"}`（**无 `data` 字段**，不会触发崩溃 —— 排除"完全无登录态"） |
| 带无效 Bearer token | `{"code":401,"data":null,"message":"未登录"}`（`data:null` 的来源之一，但用户 `user_info` 正常说明不是这种） |
| 带账号实际 token + `fs_list_new` | `{"code":400,"data":null,"message":"driveId is required"}` ← **用户的真实响应** |

**修复**（`pan123.py` + `tests/test_123_api.py`）

绕过 `fs_list_new`，直接 `client.request` 透传驼峰键（`request` 内部不做小写化）。`pan123.py` 的 `find_directory_path`：

```python
response = client.request(
    "file/list/new",
    params=payload,                  # payload 已是驼峰键    base_url="https://123pan.com",
)
if response.get("code") != 0:
    logging.error(
        f"列目录失败: code={response.get('code')} "
        f"msg={response.get('message')!r}"
    )
    break
# 防御 data:null：键存在但为 None 时 .get('data', {}) 仍返回 None
data = response.get('data') or {}
```

`tests/test_123_api.py` 的冒烟测试同样改成 `client.request("file/list/new", params=payload, base_url="https://123pan.com")`，否则 CI 测试门从 9/7 起会一直挂红。

**验证脚本**（`D:\code\_archive\linshi\yunwei\` 下，只读）：
- `diag_123_session.py`：4 次只读调用，确认 `fs_list_new` → 400、直接 `request` + 驼峰 → `code:0`。
- `verify_after_patch.py`：8 次只读调用，模拟修复后走完 `nas → MPLink → teleplay → 国产剧 → 兰香如故 (2026) → Season 1` 整条父目录链，每层都拿到 FileId。

**再犯排查**
- 错误消息里有 `driveId is required` / `code:400` / `data:null` → 大概率是这个。
- 看请求日志的 query string：若所有键都是小写（`driveid`/`orderby`/`indirectspace`/`filecategory`/`onlylookabnormalfile`），就是 p123client 的 `dict_key_to_lower_merge` 在作怪——**不要**依赖 `fs_list_new`，直接 `client.request`。
- `data = response.get('data', {})` 务必加 `or {}` 防 `data:null`，并校验 `code != 0` 显式 break，避免把"参数错误"变成静默的"任务放弃"。
- 库升级到 `p123client==0.0.9.4` **不会修这个**（`fs_list_new` 实现没变，`dict_key_to_lower_merge` 仍把所有键小写化）。依赖升级不是这个问题的解法。

---

## 10. 通用 path→id 缓存（feat/cid-cache 分支实施）

> **状态：已实施。** 2026-09-30 架构层落地（替代 v1 复制粘贴 `cid_db_115.py` 的方案）。
> 2026-10-02 收尾：`cid_db_115.py` 死代码已删除（生产代码自 v2 起不再引用，仅测试残留），
> Bug#1 跨线程测试同步迁移到 `PathIdCache`。115 老表 `dir_cid_mapping`（`dir_path` 列）数据不变，仍由 `PathIdCache(path_column="dir_path")` 兼容读写。
> 影响面：`providers/base.py`（加 `resolve_path` 抽象）+ `providers/_cid_cache.py`（新增通用缓存）+ `providers/pan123.py` / `providers/pan115.py`（实现）+ `pan123.py`（删除原 `find_directory_path` / `find_cid_by_parts`）+ `monitor.py`（调用方 rename）。

### 10.1 为什么需要

9/7 修复后（见 ## 9），123 的 `find_directory_path` 每次上传都重走 API 列目录：6 段路径 = 6 次 `file/list/new`。同一剧集下的多个文件，**每一层都重复列一次**——热门剧目一天几十次上传，徒增 API 压力。

更糟的是**单线程 worker + 无 max-page 兜底**：18:20–22:46 曾因 123 `Next` 字段异常把整个 worker 卡死 4 小时，饿死几十个文件。加缓存后热路径零 API 调用，扫页 bug 的"土壤"基本消除。

### 10.2 架构改进（v2 替代 v1）

**v1 计划（不采用）**：复制 `cid_db_115.py` 改名为 `cid_db_123.py`。问题：
1. 复制粘贴——违反 DRY
2. 缓存在仓库根，与 `providers/` 抽象脱节——加新 provider 又要复制一份
3. `find_directory_path` 混合路径拼装 + 缓存 + API + 写入四种关注点
4. 路径格式 `"/" + "/".join(parts)` 是隐式契约

**v2（已实施）**：通用 `PathIdCache` + provider 自管的 `resolve_path`。

```
providers/
  base.py             # CloudProvider ABC：加 resolve_path 抽象方法
  _cid_cache.py       # 新建：通用 PathIdCache（单例连接 + RLock + 参数化表/列）
  pan123.py           # 实现 resolve_path + _walk_one_level（123 翻页逻辑）+ _maybe_invalidate_parent
  pan115.py           # 实现 resolve_path + _walk_one_level（115 翻页逻辑）+ _maybe_invalidate_parent
pan123.py             # 删除 find_directory_path / find_cid_by_parts（原逻辑整体迁移）
monitor.py            # provider.find_parent → provider.resolve_path（语义不变）
```

**核心契约**：
```python
@abstractmethod
def resolve_path(self, parts: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    """返回 (parent_id, remaining_parts)。
    - 先查缓存最大前缀，命中跳过 API
    - 未命中段逐层走 API（带分页/重试）
    - 每找到一个 segment 就 upsert 到缓存
    - API 找不到 → invalidate 缓存里对应的 parent 条目
    - remaining=() 表示完整解析；非空表示上层需退避重试
    """
```

### 10.3 缓存失效策略（懒失效）

| 触发条件 | 行为 |
|---|---|
| API 在缓存命中的 parent 下找不到目标段 | invalidate parent 这条缓存 |
| API 返回 `code != 0` / 瞬时 5xx | **不**invalidate（避免瞬时错误清缓存） |
| invalidate 不存在的 path | 返回 False，**不抛异常**（幂等） |
| SQLite 异常（写不进去等） | log WARNING，**不**让上传流程中断 |

**为什么只 invalidate parent 不级联**：parent 是缓存命中过的（可能脏），segment 是 cache miss（没存过、删了 0 行）。级联清是"过度修复"——下次 resolve 时缓存命中就跳过 API，根本不会被脏数据坑。极端场景下"深层失效"靠自然冒泡：parent 真被删了，下次 resolve 仍 miss → API 仍找不到 → 再 invalidate parent（DELETE 0 行无害）。

**为什么不用 TTL**：123 / 115 目录结构相对稳定；懒失效已覆盖"用户删目录"主场景。TTL 引入复杂度收益不大。

### 10.4 路径格式契约

`providers/_cid_cache.py::format_path_key(parts)` 提供：
```python
return "/" + "/".join(parts)
```

所有 `PathIdCache.get` / `upsert` / `invalidate` 的 path 参数都通过这个函数构造，**不允许调用方自己拼字符串**。统一格式后不同 provider 的缓存 key 不会出现"形似实异"的脏数据。

### 10.5 安全设计

- **SQL 标识符白名单**：`PathIdCache.__init__` 用 `_check_ident` 校验 `table_name` / `id_column` 必须是 `[A-Za-z_][A-Za-z0-9_]*`，禁止特殊字符（防 SQL 注入和拼写错误）。
- **同一 table_name 不能创建两个实例**（防覆盖）。
- **所有 SQLite 操作包在 `with self._lock:`** 内（上传是单线程，锁开销可忽略）。
- **get / upsert / invalidate 失败均 log 后吞掉**，不让上传流程崩——缓存是优化不是真理，下次 resolve 会再 walk 一次重建。

### 10.6 验证

`D:\code\_archive\linshi\yunwei\verify_cid_cache.py`（只读脚本，不传文件、不动仓库）：
- **Phase 1 冷启动 walk**：6 段路径 → 6 次 API（与原 verify_after_patch.py 一致，证明 walk 逻辑等价）
- **Phase 2 暖启动 resolve**：同一路径再 resolve → **0 次 API 调用**（缓存全命中）
- **Phase 3 懒失效契约**：invalidate `/nas/MPLink/teleplay` → 再 resolve → 4 次 API（前 2 段命中、后 4 段 walk）
- **Phase 4 重填后再 0 API**：Phase 3 重 walk 后再 resolve → 又 0 次 API

单元测试（`tests/test_cid_cache.py` 16 项 + `tests/test_provider.py` 23 项）覆盖 get / upsert / invalidate / SQL 注入防御 / 标识符白名单 / 双实例隔离 / resolve_path 全场景 mock。

### 10.7 实施后调用方无感知

`monitor.py` 只改了 2 行：`provider.find_parent(parts)` → `provider.resolve_path(tuple(parts))`。返回结构从 `(parent_id, remaining)` 同样变成 `(parent_id, remaining)`（remaining 类型从 list 变 tuple，鸭子类型兼容）。

`upload_worker.py` / `handle_dir_creation` / `handle_file_creation` 均无修改——它们通过 `provider` 接口访问，不知道也不关心具体实现。

### 10.8 显式不做（标记为后续）

- **异步缓存写入**：本次同步写入，单线程 worker 单次 walk ~6ms 开销可忽略；接口设计已留好替换空间（只换 `PathIdCache` 内部实现，不动抽象）。
- **TTL 失效**：懒失效覆盖主场景。
- **异步 I/O 改造 worker**：独立大重构，不在本次范围。
- **max-page 兜底**：等观察 1-2 周决定，cache 上完后翻页 bug 土壤基本消除。

### 10.9 再犯排查

- 新增网盘 provider → 写一个 `PathIdCache(table_name=..., id_column=...)` 单例 + 实现 `resolve_path` / `_walk_one_level` / `_maybe_invalidate_parent` 即可，无需关心缓存实现细节。
- 缓存表丢了 / 想重建 → 直接删 `cid_mapping.db`，下次 resolve 会自动重建表 + 填充。
- 缓存错乱（parent FileId 错了）→ 不必手动 SQL 修，让 lazy invalidation 自然修复：用户上传一个文件 → resolve 不到 → invalidate parent → 重 walk 填正确值。
- 想要"重启清缓存" → 删 `cid_mapping.db` + 重启容器（不推荐，会触发冷启动全 walk）。

## 11. 技术债清理（2026-10-02）

> **状态：已实施。** 架构优化（## 10）后的收尾清理，四项一次性落地。

1. **删除 `cid_db_115.py` 死代码**：生产代码自 v2 起不再 import（仅 `tests/test_latent_bugs.py` Bug#1 残留引用），Bug#1 跨线程测试迁移到 `PathIdCache`。115 老表数据不受影响（`dir_cid_mapping` + `dir_path` 列由 `PathIdCache(path_column="dir_path")` 继续读写）。
2. **Bug#5 修复——`failed_tasks.txt` 可配置**：`upload_worker.py::_failed_tasks_path()` 每次写入时读 `FAILED_TASKS_FILE` 环境变量，未配置默认 `cwd/failed_tasks.txt`（容器内 /app，repo compose 已 bind-mount）。NAS 等自定义部署：加 `-v ./failed_tasks.txt:/app/failed_tasks.txt` 挂载，或设 `FAILED_TASKS_FILE` 指向已挂载目录。注意 bind-mount 单文件需宿主先 `touch` 该文件，否则 Docker 会创建成目录。
3. **Bug#4 防护——p115oss monkey-patch 显式化**：`client.py` 的 `upload_init` 补丁应用改为 try/except + 启动日志（"已启用 p115oss upload_init 补丁…"）。升级 p115oss 后若 115 上传异常，启动日志可第一时间定位嫌疑，不再静默。
4. **空闲心跳去噪**：`upload_worker.py` 的 "✅ 所有上传任务已完成" 从每 30s 重复打改为每个空闲期只打一次（`idle_logged` 标志，取到新任务时重置）。长时间空闲下 upload.log 不再被刷屏。

## 通用排查思路

1. **先看 `error.log`（ERROR+）和 `upload.log`**，按 ERROR 消息前缀分类：`查询失败` / `请求失败` / `【彻底放弃上传】` / `[任务放弃]`。
2. **404 / 非 JSON** → 多半云盘 API 变更（域名 / 路径），查 p123client / p115client 的 base_url 与官方现行接口。
3. **405** → WAF 拦请求头 / payload，重点查 appversion、UA、签名；往往是客户端库版本问题。
4. **cookie 失效** → 先 `grep "logout("` 排查代码，再怀疑服务端踢；`test_client.py` 的 `reset_client` 是高危点。
5. **升级 p115client / p115oss / p123client 后**：检查 `P115Client` 构造参数、`upload_file` 签名、`upload_init` payload 字段——这几个库迭代快，参数常被删改。
6. **失败统计**：`【彻底放弃上传】网盘:[X]`（上传 3/3 耗尽）和 `[任务放弃]`（父目录 10/10 耗尽）是「最终失败」标记；统计去重时按云盘拆 + 求并集（同一文件可能两个云盘都失败）。日志大用 `grep`/`sed` 流式处理，别整份读进内存。
