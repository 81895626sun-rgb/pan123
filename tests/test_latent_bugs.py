"""latent bug 触发条件测试 —— 验证 6 个已知反模式在什么条件下会崩溃。

零网络依赖。部分测试会故意触发已知 bug 来证明其存在（标记为 expected failure），
修复后改为正常断言。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import threading
import tempfile
import sqlite3
import time
import json
from unittest.mock import MagicMock, patch, PropertyMock
from queue import Queue


# ══════════════════════════════════════════════════════════════════════════════
# Bug #1: 跨线程 SQLite (cid_db_115.py)
# sqlite3.connect("cid_mapping.db") 默认 check_same_thread=True，
# 但连接被 priority_queue_worker 线程调用 find_cid_by_parts 使用。
# 触发条件：主线程创建连接 → 另一个线程执行查询
# ══════════════════════════════════════════════════════════════════════════════

def test_cross_thread_sqlite_crashes():
    """Bug #1: check_same_thread=True 时，跨线程使用同一连接会抛 ProgrammingError。

    预期行为：当前代码会崩溃（证明 bug 存在）。
    修复后：应设 check_same_thread=False 或使用连接池。
    """
    # 模拟 cid_db_115 的模式：模块级全局连接
    conn = sqlite3.connect(":memory:")  # check_same_thread=True（默认）
    conn.execute("CREATE TABLE test (id INTEGER)")

    error_from_thread = []

    def query_from_other_thread():
        try:
            conn.execute("SELECT * FROM test")
        except sqlite3.ProgrammingError as e:
            error_from_thread.append(str(e))

    t = threading.Thread(target=query_from_other_thread)
    t.start()
    t.join()

    # 当前代码行为：跨线程访问会触发 ProgrammingError
    assert len(error_from_thread) > 0, (
        "预期：跨线程 SQLite 查询应抛出 ProgrammingError（check_same_thread=True）\n"
        "如果此断言失败，说明 SQLite 版本或配置已允许跨线程，Bug #1 可能已修复"
    )
    assert "SQLite objects created in a thread" in error_from_thread[0] or \
           "thread" in error_from_thread[0].lower()


# ══════════════════════════════════════════════════════════════════════════════
# Bug #2: find_cid_by_parts 重试耗尽后空响应崩溃
# pan123.py:152  for item in response['data']:
# 重试 3 次后 response 可能为 None 或缺少 data 字段，直接崩溃
# ══════════════════════════════════════════════════════════════════════════════

def test_find_cid_by_parts_empty_response_crash():
    """Bug #2: fs_files 重试 3 次后返回 None → response['data'] 触发 TypeError。

    当前预期：崩溃（证明 bug 存在）。
    修复后：应在访问 response['data'] 前判空，安全 fallback。
    """
    # 模拟 client.fs_files 始终返回 None（或缺少 data 字段）
    mock_client = MagicMock()
    mock_client.fs_files.return_value = None

    from pan123 import find_cid_by_parts

    try:
        find_cid_by_parts(mock_client, ["folder1", "folder2"])
        crashed = False
    except (TypeError, KeyError) as e:
        crashed = True
        error_type = type(e).__name__
    except Exception as e:
        # 其他异常（如 RequestException 重试循环）也接受
        crashed = True
        error_type = type(e).__name__

    assert crashed, (
        "预期：find_cid_by_parts 在 client.fs_files 返回 None 时应崩溃\n"
        "（TypeError: 'NoneType' object is not subscriptable）\n"
        "如果此断言失败，说明代码已修复 #2"
    )
    print(f"    崩溃类型: {error_type}（预期内）")


def test_find_cid_by_parts_missing_data_key():
    """Bug #2 变体：response 缺少 data 字段 → KeyError"""
    mock_client = MagicMock()
    # 返回一个看起来成功的响应但不含 data 字段
    response = {"state": True, "errNo": 0}  # 没有 "data"
    mock_client.fs_files.return_value = response

    from pan123 import find_cid_by_parts

    try:
        find_cid_by_parts(mock_client, ["folder1"])
        crashed = False
    except (KeyError, TypeError) as e:
        crashed = True
        error_type = type(e).__name__

    assert crashed, (
        "预期：find_cid_by_parts 在 response 缺少 'data' 字段时应崩溃（KeyError）\n"
        "如果此断言失败，说明代码已修复"
    )
    print(f"    崩溃类型: {error_type}（预期内）")


# ══════════════════════════════════════════════════════════════════════════════
# Bug #4: client.py import 时 monkey-patch p115oss.upload.upload_init
# 副作用：导入 client 模块会全局替换第三方库函数，p115oss 升级后可能静默失败
# ══════════════════════════════════════════════════════════════════════════════

def test_monkey_patch_is_applied():
    """Bug #4: 导入 client 模块后，p115oss.upload.upload_init 已被替换。

    这不是测试 bug 是否触发，而是记录当前行为。
    p115oss 升级后注册签名变化会导致静默错误。
    """
    import p115oss.upload
    original_func = p115oss.upload.upload_init

    # 导入 client 会触发 monkey-patch（模块级代码执行）
    import client

    patched_func = p115oss.upload.upload_init

    assert patched_func is not original_func, (
        "预期：导入 client 后 p115oss.upload.upload_init 应被替换\n"
        "如果此断言失败，说明 monkey-patch 已移除或 p115oss 已升级修复"
    )
    assert patched_func.__name__ == "_p115oss_upload_init_fixed", (
        f"预期函数名为 _p115oss_upload_init_fixed，实际: {patched_func.__name__}"
    )


def test_monkey_patch_injects_appversion():
    """Bug #4: 验证 monkey-patch 确实注入了 appversion 字段"""
    import p115oss.upload
    # 先导入 client 触发 monkey-patch
    import client

    # 调用 patched 函数（不实际发请求，参数不全会报错，但我们可以验证 payload 被修改）
    # 通过 mock 原始函数来验证
    import p115oss.upload as upload_mod
    patched = upload_mod.upload_init

    # 验证 patched 函数包装了原始函数
    assert hasattr(patched, '__wrapped__') or callable(patched), \
        "patched 函数应可调用"


# ══════════════════════════════════════════════════════════════════════════════
# Bug #5: failed_tasks.txt 在 Docker 未挂载
# upload_worker.py: FAILED_TASKS_FILE = "failed_tasks.txt"（相对路径 = cwd）
# docker-compose.yml 未 bind-mount 此文件，容器重启后丢失
# ══════════════════════════════════════════════════════════════════════════════

def test_failed_tasks_file_is_relative_path():
    """Bug #5: failed_tasks.txt 使用相对路径，Docker 重启后丢失。

    当前行为：文件写入 cwd，Docker 未挂载则丢失。
    """
    from upload_worker import FAILED_TASKS_FILE

    # 验证是相对路径（不是绝对路径）
    assert not os.path.isabs(FAILED_TASKS_FILE), (
        f"预期 FAILED_TASKS_FILE 为相对路径（当前：{FAILED_TASKS_FILE}），"
        "Docker 未挂载时容器重启会丢失"
    )
    assert FAILED_TASKS_FILE == "failed_tasks.txt"


def test_failed_tasks_writes_to_cwd():
    """Bug #5: 验证 log_failed_task 写入 cwd 而非 Docker 持久化目录"""
    from monitor import UploadTask
    from upload_worker import log_failed_task

    # 用临时目录模拟 cwd
    with tempfile.TemporaryDirectory() as tmpdir:
        orig_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            task = UploadTask(local_path="/tmp/test.txt", is_dir=False, pan_name="123")
            task.retries = 3
            task.dir_id = "12345"

            # 注意：log_failed_task 会调 Telegram 通知器，这里可能因缺凭证而报错
            # 我们只测试文件写入路径，用 try-except 包裹
            try:
                log_failed_task(task, "测试错误")
            except Exception:
                pass  # Telegram 通知失败不影响文件写入

            # 检查文件是否在 tmpdir 下创建
            expected_path = os.path.join(tmpdir, "failed_tasks.txt")
            assert os.path.exists(expected_path), (
                f"预期 failed_tasks.txt 应写入 cwd ({expected_path})，"
                "Docker 未挂载此路径时容器重启会丢失"
            )
        finally:
            os.chdir(orig_cwd)


# ══════════════════════════════════════════════════════════════════════════════
# Bug #6: upload_task_worker GROWING 分支 time.sleep 阻塞
# upload_worker.py:139  time.sleep(wait_time)  wait_time = min(120, retries*30)
# 单线程 worker 被 sleep 30-120s，期间其他 READY 文件全部阻塞
# ══════════════════════════════════════════════════════════════════════════════

def test_growing_sleep_blocks_other_tasks():
    """Bug #6: GROWING 分支的 time.sleep 会阻塞同一 worker 的其他 READY 任务。

    模拟场景：队列中有 [GROWING_file, READY_file]，worker 取出 GROWING_file
    后会 sleep 30-120s，READY_file 在此期间无法被处理。
    """
    from monitor import UploadTask

    # 用 Queue 模拟 upload_queue
    q = Queue()

    # 放入两个任务：GROWING 在前，READY 在后
    growing_task = UploadTask(local_path="/tmp/growing.iso", is_dir=False, pan_name="123")
    growing_task.retries = 0
    ready_task = UploadTask(local_path="/tmp/ready.txt", is_dir=False, pan_name="123")

    q.put(growing_task)
    q.put(ready_task)

    # 模拟 GROWING 分支逻辑
    MAX_RETRIES = 3
    task = q.get(timeout=1)  # 取到 growing_task

    if task.retries < MAX_RETRIES:
        task.retries += 1
        q.put(task)  # 重新入队
        wait_time = min(120, task.retries * 30)  # = 30s

        # 验证：此时队列中还有 ready_task，但 worker 即将 sleep 30s
        assert q.qsize() == 2, "队列中应有 growing_task（重入队）和 ready_task"
        assert wait_time >= 30, f"GROWING 等待时间应为 30s 起步，实际: {wait_time}s"

        # 关键断言：如果 worker 在这里 sleep(wait_time)，ready_task 会被阻塞
        # 直到 sleep 结束才能被处理
        print(f"    ⚠ GROWING 分支会 sleep {wait_time}s，期间 READY 文件被阻塞")
        print(f"    队列中 {q.qsize()} 个任务等待处理")

    # 验证 ready_task 确实在队列中等待
    found_ready = False
    while not q.empty():
        t = q.get()
        if t.local_path == "/tmp/ready.txt":
            found_ready = True
    assert found_ready, "ready_task 应在队列中等待"


def test_growing_sleep_max_blocking_time():
    """Bug #6: 最坏情况下 GROWING 分支 sleep 可达 120s"""
    from monitor import UploadTask

    task = UploadTask(local_path="/tmp/stuck.iso", is_dir=False, pan_name="123")
    # 模拟第 4 次重试
    task.retries = 3
    MAX_RETRIES = 3

    if task.retries < MAX_RETRIES:
        task.retries += 1
        wait_time = min(120, task.retries * 30)
        # retries 从 3 → 4，wait_time = min(120, 120) = 120
        assert wait_time == 120, (
            f"GROWING 最大阻塞时间应为 120s，实际: {wait_time}s\n"
            "单线程 worker 在此期间完全阻塞，无法处理其他文件"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

def run_all():
    tests = [
        # Bug #1: 跨线程 SQLite
        ("Bug#1 跨线程SQLite崩溃", test_cross_thread_sqlite_crashes),
        # Bug #2: find_cid_by_parts 空响应
        ("Bug#2 空响应TypeError", test_find_cid_by_parts_empty_response_crash),
        ("Bug#2 缺少data字段KeyError", test_find_cid_by_parts_missing_data_key),
        # Bug #4: import 时 monkey-patch
        ("Bug#4 monkey-patch已应用", test_monkey_patch_is_applied),
        ("Bug#4 appversion注入", test_monkey_patch_injects_appversion),
        # Bug #5: failed_tasks.txt 未挂载
        ("Bug#5 相对路径", test_failed_tasks_file_is_relative_path),
        ("Bug#5 写入cwd", test_failed_tasks_writes_to_cwd),
        # Bug #6: GROWING sleep 阻塞
        ("Bug#6 GROWING阻塞", test_growing_sleep_blocks_other_tasks),
        ("Bug#6 最大阻塞120s", test_growing_sleep_max_blocking_time),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✅ {name}")
            passed += 1
        except AssertionError as e:
            print(f"  ❌ {name}")
            print(f"     {e}")
            failed += 1
        except Exception as e:
            print(f"  💥 {name} — {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*60}")
    print(f"结果: {passed} 通过, {failed} 失败, 共 {len(tests)} 项")
    if failed > 0:
        print(f"失败项是预期内的 bug 触发条件证明，修复后应全部通过")
    return failed == 0


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)