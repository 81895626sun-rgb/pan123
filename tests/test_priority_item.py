"""PriorityItem dataclass 单元测试 —— 验证排序语义与旧元组协议完全等价。

零网络依赖，纯数据结构测试。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from queue import PriorityQueue
from monitor import PriorityItem, UploadTask, SimpleFileMonitor, _enqueue_priority


def test_monitor_import_chain():
    """monitor 模块完整 import 链无异常"""
    import monitor
    assert hasattr(monitor, "PriorityItem")
    assert hasattr(monitor, "UploadTask")
    assert hasattr(monitor, "SimpleFileMonitor")
    assert hasattr(monitor, "_enqueue_priority")
    assert hasattr(monitor, "priority_queue_worker")
    assert hasattr(monitor, "handle_file_creation_115")
    assert hasattr(monitor, "handle_file_creation_123")


def test_construction_without_task():
    """无 task 构造：只传 filepath"""
    item = PriorityItem(is_file=0, depth=2, seq=5, filepath="/a/b")
    assert item.is_file == 0
    assert item.depth == 2
    assert item.seq == 5
    assert item.filepath == "/a/b"
    assert item.task is None


def test_construction_with_task():
    """带 task 构造"""
    mock_task = object()
    item = PriorityItem(is_file=1, depth=3, seq=7, filepath="/x/y/z", task=mock_task)
    assert item.task is mock_task


def test_dir_before_file():
    """目录 (is_file=0) 排在文件 (is_file=1) 前面"""
    dir_item = PriorityItem(is_file=0, depth=5, seq=10, filepath="/a")
    file_item = PriorityItem(is_file=1, depth=1, seq=1, filepath="/b")
    assert dir_item < file_item


def test_shallow_before_deep():
    """浅目录排在深目录前面"""
    shallow = PriorityItem(is_file=0, depth=2, seq=1, filepath="/a/b")
    deep = PriorityItem(is_file=0, depth=5, seq=1, filepath="/a/b/c/d/e")
    assert shallow < deep


def test_fifo_within_same_priority():
    """同优先级下 seq 小的先出（FIFO）"""
    first = PriorityItem(is_file=0, depth=2, seq=1, filepath="/a")
    second = PriorityItem(is_file=0, depth=2, seq=2, filepath="/b")
    assert first < second


def test_filepath_not_in_comparison():
    """filepath 不参与排序比较 —— 同 is_file/depth/seq 的两个 item 应相等"""
    a = PriorityItem(is_file=0, depth=2, seq=5, filepath="/zzz")
    b = PriorityItem(is_file=0, depth=2, seq=5, filepath="/aaa")
    # 排序字段相同 → 既不小于也不大于（== 为 True 需要 __eq__ 配合）
    assert not (a < b)
    assert not (b < a)
    assert a == b  # dataclass(order=True) 会生成 __eq__


def test_task_not_in_comparison():
    """task 不参与排序"""
    t1 = object()
    t2 = object()
    a = PriorityItem(is_file=1, depth=3, seq=1, filepath="/x", task=t1)
    b = PriorityItem(is_file=1, depth=3, seq=1, filepath="/x", task=t2)
    assert a == b


def test_priority_queue_integration():
    """PriorityQueue 实际出队顺序验证"""
    pq = PriorityQueue()

    # 乱序入队
    pq.put(PriorityItem(is_file=1, depth=3, seq=3, filepath="/deep/file"))       # 文件、深
    pq.put(PriorityItem(is_file=0, depth=3, seq=2, filepath="/deep/dir"))         # 目录、深（后入）
    pq.put(PriorityItem(is_file=0, depth=1, seq=1, filepath="/shallow"))          # 目录、浅
    pq.put(PriorityItem(is_file=1, depth=1, seq=4, filepath="/shallow/file"))     # 文件、浅

    # 期望出队顺序：目录优先 → 浅优先 → FIFO
    first = pq.get()
    assert first.filepath == "/shallow"          # 目录+浅
    second = pq.get()
    assert second.filepath == "/deep/dir"        # 目录+深
    third = pq.get()
    assert third.filepath == "/shallow/file"     # 文件+浅
    fourth = pq.get()
    assert fourth.filepath == "/deep/file"       # 文件+深


def test_task_default_none():
    """task 默认值为 None"""
    item = PriorityItem(is_file=1, depth=1, seq=1, filepath="/test")
    assert item.task is None


def test_repr_readable():
    """__repr__ 包含所有字段"""
    item = PriorityItem(is_file=0, depth=2, seq=5, filepath="/a/b")
    r = repr(item)
    assert "is_file=0" in r
    assert "depth=2" in r
    assert "seq=5" in r
    assert "filepath='/a/b'" in r


def test_field_assignment():
    """dataclass 字段可赋值"""
    item = PriorityItem(is_file=0, depth=2, seq=5, filepath="/a")
    item.task = "changed"
    assert item.task == "changed"


def test_upload_task_compat():
    """UploadTask 可赋值给 PriorityItem.task"""
    task = UploadTask(local_path="/tmp/test.txt", is_dir=False, pan_name="123")
    item = PriorityItem(is_file=1, depth=3, seq=1, filepath="/tmp/test.txt", task=task)
    assert item.task is task
    assert item.task.pan_name == "123"
    assert item.task.local_path == "/tmp/test.txt"


def test_simple_file_monitor_priority_queue_type():
    """SimpleFileMonitor.priority_queue 接受 PriorityItem 类型"""
    import tempfile
    import threading
    tmpdir = tempfile.mkdtemp()
    try:
        m = SimpleFileMonitor(tmpdir, client_115=None, client_123=None)
        # 能正常放入 PriorityItem
        m.priority_queue.put(PriorityItem(is_file=0, depth=0, seq=0, filepath="/test"))
        assert m.priority_queue.qsize() == 1
        item = m.priority_queue.get()
        assert isinstance(item, PriorityItem)
        assert item.filepath == "/test"
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_all():
    tests = [
        ("import链完整", test_monitor_import_chain),
        ("构造(无task)", test_construction_without_task),
        ("构造(带task)", test_construction_with_task),
        ("目录优先于文件", test_dir_before_file),
        ("浅目录优先于深目录", test_shallow_before_deep),
        ("同优先级FIFO", test_fifo_within_same_priority),
        ("filepath不参与排序", test_filepath_not_in_comparison),
        ("task不参与排序", test_task_not_in_comparison),
        ("PriorityQueue集成", test_priority_queue_integration),
        ("task默认None", test_task_default_none),
        ("repr可读", test_repr_readable),
        ("字段可赋值", test_field_assignment),
        ("UploadTask兼容", test_upload_task_compat),
        ("SimpleFileMonitor兼容", test_simple_file_monitor_priority_queue_type),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✅ {name}")
            passed += 1
        except AssertionError as e:
            print(f"  ❌ {name} — {e}")
            failed += 1
        except Exception as e:
            print(f"  💥 {name} — {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"结果: {passed} 通过, {failed} 失败, 共 {len(tests)} 项")
    return failed == 0


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)