"""CloudProvider 单元测试 —— 验证 Provider 接口一致性和行为正确性。

零网络依赖，使用 mock 验证各 Provider 的方法签名和调度逻辑。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from unittest.mock import MagicMock, patch
from providers.base import CloudProvider
from providers.pan123 import Pan123Provider
from providers.pan115 import Pan115Provider


# ══════════════════════════════════════════════════════════════════════════════
# 接口一致性
# ══════════════════════════════════════════════════════════════════════════════

def test_providers_implement_all_methods():
    """两个 Provider 都实现了 CloudProvider 的全部抽象方法"""
    required = {"find_parent", "mkdir", "upload"}
    for cls in (Pan123Provider, Pan115Provider):
        for method in required:
            assert hasattr(cls, method), f"{cls.__name__} 缺少方法 {method}"


def test_provider_name_matches():
    """Provider name 属性正确"""
    mock_123 = MagicMock()
    mock_115 = MagicMock()
    p123 = Pan123Provider(mock_123)
    p115 = Pan115Provider(mock_115)
    assert p123.name == "123"
    assert p115.name == "115"


def test_provider_stores_client():
    """Provider 持有传入的 client 引用"""
    client = object()
    p = Pan123Provider(client)
    assert p.client is client


# ══════════════════════════════════════════════════════════════════════════════
# Pan123Provider
# ══════════════════════════════════════════════════════════════════════════════

def test_pan123_find_parent_delegates():
    """123 find_parent 委托给 find_directory_path"""
    mock_client = MagicMock()
    p = Pan123Provider(mock_client)

    with patch("providers.pan123.find_directory_path") as mock_find:
        mock_find.return_value = ("file_id_123", [])
        result = p.find_parent(["a", "b"])
        mock_find.assert_called_once_with(client=mock_client, parts=["a", "b"])
        assert result == ("file_id_123", [])


def test_pan123_mkdir():
    """123 mkdir 调用 fs_mkdir 并返回 FileId"""
    mock_client = MagicMock()
    mock_client.fs_mkdir.return_value = {
        "code": 0,
        "data": {"Info": {"FileId": 99999}}
    }
    p = Pan123Provider(mock_client)
    result = p.mkdir("testdir", "parent_id")
    mock_client.fs_mkdir.assert_called_once_with("testdir", "parent_id")
    assert result == "99999"


def test_pan123_mkdir_failure_raises():
    """123 mkdir 失败时抛出异常"""
    mock_client = MagicMock()
    mock_client.fs_mkdir.return_value = {"code": 1, "message": "fail"}
    p = Pan123Provider(mock_client)
    try:
        p.mkdir("testdir", "parent_id")
        assert False, "应抛出 RuntimeError"
    except RuntimeError:
        pass


def test_pan123_upload_delegates():
    """123 upload 委托给 smart_upload"""
    mock_client = MagicMock()
    p = Pan123Provider(mock_client)

    with patch("providers.pan123.smart_upload") as mock_upload:
        mock_upload.return_value = {"data": {"Info": {"FileId": 123}}}
        p.upload("/tmp/test.txt", "parent_id")
        mock_upload.assert_called_once_with(
            client=mock_client,
            file_source="/tmp/test.txt",
            parent_id="parent_id"
        )


def test_pan123_upload_invalid_response_raises():
    """123 upload 返回无效响应时抛出异常"""
    mock_client = MagicMock()
    p = Pan123Provider(mock_client)

    with patch("providers.pan123.smart_upload") as mock_upload:
        mock_upload.return_value = None  # 无效响应
        try:
            p.upload("/tmp/test.txt", "parent_id")
            assert False, "应抛出 RuntimeError"
        except RuntimeError as e:
            assert "无效的API响应" in str(e)


# ══════════════════════════════════════════════════════════════════════════════
# Pan115Provider
# ══════════════════════════════════════════════════════════════════════════════

def test_pan115_find_parent_delegates():
    """115 find_parent 委托给 find_cid_by_parts"""
    mock_client = MagicMock()
    p = Pan115Provider(mock_client)

    with patch("providers.pan115.find_cid_by_parts") as mock_find:
        mock_find.return_value = (12345, [])
        result = p.find_parent(["a", "b"])
        mock_find.assert_called_once_with(client=mock_client, parts=["a", "b"])
        assert result == (12345, [])


def test_pan115_mkdir():
    """115 mkdir 调用 fs_mkdir 并返回 cid"""
    mock_client = MagicMock()
    mock_client.fs_mkdir.return_value = {"cid": 88888}
    p = Pan115Provider(mock_client)
    result = p.mkdir("testdir", "parent_id")
    mock_client.fs_mkdir.assert_called_once_with("testdir", "parent_id")
    assert result == "88888"


def test_pan115_mkdir_failure_raises():
    """115 mkdir 失败时抛出异常"""
    mock_client = MagicMock()
    mock_client.fs_mkdir.return_value = {"error": "no cid"}
    p = Pan115Provider(mock_client)
    try:
        p.mkdir("testdir", "parent_id")
        assert False, "应抛出 RuntimeError"
    except RuntimeError:
        pass


def test_pan115_mkdir_with_cloud_path_caches():
    """115 mkdir 传 cloud_path 时调用 upsert_mapping 入库"""
    mock_client = MagicMock()
    mock_client.fs_mkdir.return_value = {"cid": 55555}
    p = Pan115Provider(mock_client)

    with patch("cid_db_115.upsert_mapping") as mock_upsert:
        result = p.mkdir("testdir", "parent_id", cloud_path="prefix/folder/testdir")
        assert result == "55555"
        mock_upsert.assert_called_once_with("/prefix/folder/testdir", 55555)


def test_pan115_upload_delegates():
    """115 upload 委托给 upload_file"""
    mock_client = MagicMock()
    p = Pan115Provider(mock_client)

    with patch("providers.pan115.upload_file") as mock_upload:
        p.upload("/tmp/test.txt", "parent_id")
        mock_upload.assert_called_once_with(
            client=mock_client,
            file_path="/tmp/test.txt",
            pid="parent_id"
        )


# ══════════════════════════════════════════════════════════════════════════════
# handle_dir_creation 用 Provider 调度
# ══════════════════════════════════════════════════════════════════════════════

def test_handle_dir_creation_dispatches_to_all_providers():
    """handle_dir_creation 为每个 provider 调用 find_parent + mkdir"""
    with patch("monitor.convert_to_cloud_path") as mock_convert:
        mock_convert.return_value = "prefix/a/b/testdir"

        mock_123 = MagicMock(spec=Pan123Provider)
        mock_123.name = "123"
        mock_123.find_parent.return_value = ("123_id", [])

        mock_115 = MagicMock(spec=Pan115Provider)
        mock_115.name = "115"
        mock_115.find_parent.return_value = ("115_id", [])

        providers = {"123": mock_123, "115": mock_115}
        mock_monitor = MagicMock()

        from monitor import handle_dir_creation
        handle_dir_creation(providers, mock_monitor, "/local/a/b/testdir")

        # 两个 provider 都被调用
        mock_123.find_parent.assert_called_once()
        mock_123.mkdir.assert_called_once()
        mock_115.find_parent.assert_called_once()
        mock_115.mkdir.assert_called_once()


def test_handle_dir_creation_skips_when_parent_not_found():
    """父目录未找到时跳过 mkdir"""
    with patch("monitor.convert_to_cloud_path") as mock_convert:
        mock_convert.return_value = "prefix/a/b/testdir"

        mock_123 = MagicMock(spec=Pan123Provider)
        mock_123.name = "123"
        mock_123.find_parent.return_value = ("0", ["missing"])

        providers = {"123": mock_123}
        mock_monitor = MagicMock()

        from monitor import handle_dir_creation
        handle_dir_creation(providers, mock_monitor, "/local/a/b/testdir")

        # find_parent 被调用，但 mkdir 不应被调用（父目录未找到）
        mock_123.find_parent.assert_called_once()
        mock_123.mkdir.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# handle_file_creation 用 Provider 调度
# ══════════════════════════════════════════════════════════════════════════════

def test_handle_file_creation_dispatches_to_provider():
    """handle_file_creation 通过 provider.find_parent 查找父目录"""
    with patch("monitor.convert_to_cloud_path") as mock_convert:
        mock_convert.return_value = "prefix/a/test.txt"

        mock_provider = MagicMock(spec=Pan123Provider)
        mock_provider.name = "123"
        mock_provider.find_parent.return_value = ("123_dir_id", [])

        mock_monitor = MagicMock()
        mock_monitor.upload_queue.put_nowait = MagicMock()

        from monitor import handle_file_creation
        handle_file_creation(mock_provider, mock_monitor, "/local/a/test.txt")

        mock_provider.find_parent.assert_called_once()
        # 父目录找到 → 任务入上传队列
        mock_monitor.upload_queue.put_nowait.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

def run_all():
    tests = [
        ("接口完整性", test_providers_implement_all_methods),
        ("Provider name", test_provider_name_matches),
        ("Provider 持有 client", test_provider_stores_client),
        ("123 find_parent 委托", test_pan123_find_parent_delegates),
        ("123 mkdir", test_pan123_mkdir),
        ("123 mkdir 失败抛异常", test_pan123_mkdir_failure_raises),
        ("123 upload 委托", test_pan123_upload_delegates),
        ("123 upload 无效响应抛异常", test_pan123_upload_invalid_response_raises),
        ("115 find_parent 委托", test_pan115_find_parent_delegates),
        ("115 mkdir", test_pan115_mkdir),
        ("115 mkdir 失败抛异常", test_pan115_mkdir_failure_raises),
        ("115 mkdir+cloud_path 缓存入库", test_pan115_mkdir_with_cloud_path_caches),
        ("115 upload 委托", test_pan115_upload_delegates),
        ("handle_dir_creation 调度", test_handle_dir_creation_dispatches_to_all_providers),
        ("handle_dir_creation 父目录未找到", test_handle_dir_creation_skips_when_parent_not_found),
        ("handle_file_creation 调度", test_handle_file_creation_dispatches_to_provider),
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
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*60}")
    print(f"结果: {passed} 通过, {failed} 失败, 共 {len(tests)} 项")
    return failed == 0


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)