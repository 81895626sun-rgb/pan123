"""CloudProvider 单元测试 —— 验证 Provider 接口一致性、行为正确性、resolve_path 缓存策略。

零网络依赖，使用 mock 验证各 Provider 的方法签名和 resolve_path 在
缓存命中 / 部分命中 / 失效 场景下的行为。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from unittest.mock import MagicMock, patch, call
from providers.base import CloudProvider
from providers.pan123 import Pan123Provider
from providers.pan115 import Pan115Provider
import providers.pan123 as pan123_mod
import providers.pan115 as pan115_mod


# ══════════════════════════════════════════════════════════════════════════════
# 接口一致性
# ══════════════════════════════════════════════════════════════════════════════

def test_providers_implement_all_methods():
    """两个 Provider 都实现了 CloudProvider 的全部抽象方法"""
    required = {"resolve_path", "mkdir", "upload"}
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
# Pan123Provider.resolve_path —— 缓存策略
# ══════════════════════════════════════════════════════════════════════════════

def test_pan123_resolve_path_empty_parts_returns_root():
    """空 parts 返回 ("0", ())，不查缓存、不走 API"""
    mock_client = MagicMock()
    p = Pan123Provider(mock_client)
    with patch.object(pan123_mod._cache, "get") as mock_get:
        result = p.resolve_path(())
    assert result == ("0", ())
    mock_get.assert_not_called()
    mock_client.request.assert_not_called()


def test_pan123_resolve_path_full_cache_hit():
    """全部缓存命中 → 0 次 API 调用，直接返回"""
    mock_client = MagicMock()
    p = Pan123Provider(mock_client)

    # 模拟缓存：/a → "111", /a/b → "222", /a/b/c → "333"
    cache_data = {
        "/a": "111",
        "/a/b": "222",
        "/a/b/c": "333",
    }
    with patch.object(pan123_mod._cache, "get", side_effect=lambda k: cache_data.get(k)):
        result = p.resolve_path(("a", "b", "c"))
    assert result == ("333", ())
    mock_client.request.assert_not_called()


def test_pan123_resolve_path_cache_miss_walks_api():
    """缓存全 miss → 逐层走 API，每层成功后写缓存"""
    mock_client = MagicMock()
    # 模拟 3 层 walk：第一层 FileId=111，第二层 FileId=222，第三层 FileId=333
    mock_client.request.side_effect = [
        {"code": 0, "data": {"InfoList": [{"Type": "1", "FileName": "a", "FileId": 111}], "Next": "-1"}},
        {"code": 0, "data": {"InfoList": [{"Type": "1", "FileName": "b", "FileId": 222}], "Next": "-1"}},
        {"code": 0, "data": {"InfoList": [{"Type": "1", "FileName": "c", "FileId": 333}], "Next": "-1"}},
    ]
    p = Pan123Provider(mock_client)

    with patch.object(pan123_mod._cache, "get", return_value=None), \
         patch.object(pan123_mod._cache, "upsert", return_value=True) as mock_upsert:
        result = p.resolve_path(("a", "b", "c"))

    assert result == ("333", ())
    assert mock_client.request.call_count == 3
    # 验证每层 walk 完后都写了缓存
    assert mock_upsert.call_args_list == [
        call("/a", "111"),
        call("/a/b", "222"),
        call("/a/b/c", "333"),
    ]


def test_pan123_resolve_path_partial_cache_hit():
    """前 N 层缓存命中，第 N+1 层走 API"""
    mock_client = MagicMock()
    mock_client.request.return_value = {
        "code": 0,
        "data": {"InfoList": [{"Type": "1", "FileName": "c", "FileId": 333}], "Next": "-1"},
    }
    p = Pan123Provider(mock_client)

    # /a 和 /a/b 命中，/a/b/c 走 API
    cache_data = {"/a": "111", "/a/b": "222"}
    with patch.object(pan123_mod._cache, "get", side_effect=lambda k: cache_data.get(k)), \
         patch.object(pan123_mod._cache, "upsert", return_value=True) as mock_upsert:
        result = p.resolve_path(("a", "b", "c"))

    assert result == ("333", ())
    # 只走了 1 次 API（第 3 层）
    assert mock_client.request.call_count == 1
    # 只写了 1 次缓存
    mock_upsert.assert_called_once_with("/a/b/c", "333")


def test_pan123_resolve_path_not_found_returns_remaining():
    """API 找不到目标段 → 返回 (parent_id, remaining)，上层退避重试"""
    mock_client = MagicMock()
    # 模拟 walk：第 1 层找到 a，第 2 层找不到 b
    mock_client.request.side_effect = [
        {"code": 0, "data": {"InfoList": [{"Type": "1", "FileName": "a", "FileId": 111}], "Next": "-1"}},
        {"code": 0, "data": {"InfoList": [], "Next": "-1"}},  # 找不到 b
    ]
    p = Pan123Provider(mock_client)

    with patch.object(pan123_mod._cache, "get", return_value=None), \
         patch.object(pan123_mod._cache, "upsert", return_value=True) as mock_upsert, \
         patch.object(pan123_mod._cache, "invalidate", return_value=True) as mock_inv:
        result = p.resolve_path(("a", "b", "c"))

    # 返回 ("111", ("b", "c"))：parent 找到，剩余未就绪
    assert result == ("111", ("b", "c"))
    # /a 已写缓存；/a/b 未写（因为没找到）
    mock_upsert.assert_called_once_with("/a", "111")
    # 不应 invalidate 根目录；这里 parent 是 /a，是 API walk 出来的临时值，不是缓存命中，
    # 所以也不应 invalidate（避免误删）
    mock_inv.assert_not_called()


def test_pan123_resolve_path_invalidate_cached_parent_on_not_found():
    """缓存命中的 parent 在其下找不到目标段 → invalidate parent"""
    mock_client = MagicMock()
    # 模拟 walk：缓存有 /a → 111（命中），但在 111 下找不到 b
    mock_client.request.return_value = {
        "code": 0,
        "data": {"InfoList": [], "Next": "-1"},  # 找不到 b
    }
    p = Pan123Provider(mock_client)

    cache_data = {"/a": "111"}  # /a 命中
    with patch.object(pan123_mod._cache, "get", side_effect=lambda k: cache_data.get(k)), \
         patch.object(pan123_mod._cache, "upsert", return_value=True), \
         patch.object(pan123_mod._cache, "invalidate", return_value=True) as mock_inv:
        result = p.resolve_path(("a", "b"))

    # parent 是缓存命中过 /a，应该被 invalidate
    assert result == ("111", ("b",))
    mock_inv.assert_called_once_with("/a")


# ══════════════════════════════════════════════════════════════════════════════
# Pan123Provider.mkdir / upload
# ══════════════════════════════════════════════════════════════════════════════

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

    # patch providers.pan123.smart_upload 而不是 pan123.smart_upload：
    # providers.pan123 通过 from pan123 import smart_upload 创建本地绑定，
    # patch 必须打在使用方的模块名上才能拦截
    with patch("providers.pan123.smart_upload") as mock_upload:
        mock_upload.return_value = {"data": {"Info": {"FileId": 123}}}
        file_id = p.upload("/tmp/test.txt", "parent_id")
        mock_upload.assert_called_once_with(
            client=mock_client,
            file_source="/tmp/test.txt",
            parent_id="parent_id"
        )
        assert file_id == "123", f"upload 应返回真实 FileId，实际: {file_id}"


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
# Pan115Provider.resolve_path —— 缓存策略
# ══════════════════════════════════════════════════════════════════════════════

def test_pan115_resolve_path_full_cache_hit():
    """115 全部缓存命中 → 0 次 fs_files 调用"""
    mock_client = MagicMock()
    p = Pan115Provider(mock_client)

    cache_data = {"/x": "cid_x", "/x/y": "cid_y"}
    with patch.object(pan115_mod._cache, "get", side_effect=lambda k: cache_data.get(k)):
        result = p.resolve_path(("x", "y"))
    assert result == ("cid_y", ())
    mock_client.fs_files.assert_not_called()


def test_pan115_resolve_path_cache_miss_walks_api():
    """115 缓存全 miss → 逐层 fs_files"""
    mock_client = MagicMock()
    # 115 fs_files 返回结构：{"state":True, "errNo":0, "data":[{"n":"x","cid":"cid_x"}, ...]}
    mock_client.fs_files.side_effect = [
        {"state": True, "errNo": 0, "data": [{"n": "x", "cid": "cid_x"}]},
        {"state": True, "errNo": 0, "data": [{"n": "y", "cid": "cid_y"}]},
    ]
    p = Pan115Provider(mock_client)

    with patch.object(pan115_mod._cache, "get", return_value=None), \
         patch.object(pan115_mod._cache, "upsert", return_value=True) as mock_upsert:
        result = p.resolve_path(("x", "y"))

    assert result == ("cid_y", ())
    assert mock_client.fs_files.call_count == 2
    assert mock_upsert.call_args_list == [
        call("/x", "cid_x"),
        call("/x/y", "cid_y"),
    ]


def test_pan115_resolve_path_invalidate_cached_parent_on_not_found():
    """115 缓存命中的 parent 在其下找不到目标段 → invalidate"""
    mock_client = MagicMock()
    mock_client.fs_files.return_value = {"state": True, "errNo": 0, "data": []}
    p = Pan115Provider(mock_client)

    cache_data = {"/x": "cid_x"}
    with patch.object(pan115_mod._cache, "get", side_effect=lambda k: cache_data.get(k)), \
         patch.object(pan115_mod._cache, "upsert", return_value=True), \
         patch.object(pan115_mod._cache, "invalidate", return_value=True) as mock_inv:
        result = p.resolve_path(("x", "y"))

    assert result == ("cid_x", ("y",))
    mock_inv.assert_called_once_with("/x")


# ══════════════════════════════════════════════════════════════════════════════
# Pan115Provider.mkdir / upload
# ══════════════════════════════════════════════════════════════════════════════

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
    """115 mkdir 传 cloud_path 时通过新 _cache 写入 dir_cid_mapping"""
    mock_client = MagicMock()
    mock_client.fs_mkdir.return_value = {"cid": 55555}
    p = Pan115Provider(mock_client)

    with patch.object(pan115_mod._cache, "upsert", return_value=True) as mock_upsert:
        result = p.mkdir("testdir", "parent_id", cloud_path="prefix/folder/testdir")
        assert result == "55555"
        mock_upsert.assert_called_once_with("/prefix/folder/testdir", "55555")


def test_pan115_upload_delegates():
    """115 upload 委托给 upload_file"""
    mock_client = MagicMock()
    p = Pan115Provider(mock_client)

    # patch providers.pan115.upload_file（不是 pan123.upload_file）：
    # providers.pan115 通过 from pan123 import upload_file 创建本地绑定
    with patch("providers.pan115.upload_file") as mock_upload:
        p.upload("/tmp/test.txt", "parent_id")
        mock_upload.assert_called_once_with(
            client=mock_client,
            file_path="/tmp/test.txt",
            pid="parent_id"
        )


# ══════════════════════════════════════════════════════════════════════════════
# handle_dir_creation / handle_file_creation 用 Provider 调度
# ══════════════════════════════════════════════════════════════════════════════

def test_handle_dir_creation_dispatches_to_all_providers():
    """handle_dir_creation 为每个 provider 调用 resolve_path + mkdir"""
    with patch("monitor.convert_to_cloud_path") as mock_convert:
        mock_convert.return_value = "prefix/a/b/testdir"

        mock_123 = MagicMock(spec=Pan123Provider)
        mock_123.name = "123"
        mock_123.resolve_path.return_value = ("123_id", ())

        mock_115 = MagicMock(spec=Pan115Provider)
        mock_115.name = "115"
        mock_115.resolve_path.return_value = ("115_id", ())

        providers = {"123": mock_123, "115": mock_115}
        mock_monitor = MagicMock()

        from monitor import handle_dir_creation
        handle_dir_creation(providers, mock_monitor, "/local/a/b/testdir")

        # 两个 provider 都被调用
        mock_123.resolve_path.assert_called_once()
        mock_123.mkdir.assert_called_once()
        mock_115.resolve_path.assert_called_once()
        mock_115.mkdir.assert_called_once()


def test_handle_dir_creation_skips_when_parent_not_found():
    """父目录未找到时跳过 mkdir"""
    with patch("monitor.convert_to_cloud_path") as mock_convert:
        mock_convert.return_value = "prefix/a/b/testdir"

        mock_123 = MagicMock(spec=Pan123Provider)
        mock_123.name = "123"
        # remaining 是元组
        mock_123.resolve_path.return_value = ("0", ("missing",))

        providers = {"123": mock_123}
        mock_monitor = MagicMock()

        from monitor import handle_dir_creation
        handle_dir_creation(providers, mock_monitor, "/local/a/b/testdir")

        # resolve_path 被调用，但 mkdir 不应被调用（父目录未找到）
        mock_123.resolve_path.assert_called_once()
        mock_123.mkdir.assert_not_called()


def test_handle_file_creation_dispatches_to_provider():
    """handle_file_creation 通过 provider.resolve_path 查找父目录"""
    with patch("monitor.convert_to_cloud_path") as mock_convert:
        mock_convert.return_value = "prefix/a/test.txt"

        mock_provider = MagicMock(spec=Pan123Provider)
        mock_provider.name = "123"
        mock_provider.resolve_path.return_value = ("123_dir_id", ())

        mock_monitor = MagicMock()
        mock_monitor.upload_queue.put_nowait = MagicMock()

        from monitor import handle_file_creation
        handle_file_creation(mock_provider, mock_monitor, "/local/a/test.txt")

        mock_provider.resolve_path.assert_called_once()
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
        ("123 resolve_path 空 parts", test_pan123_resolve_path_empty_parts_returns_root),
        ("123 resolve_path 全缓存命中", test_pan123_resolve_path_full_cache_hit),
        ("123 resolve_path 全 miss walk", test_pan123_resolve_path_cache_miss_walks_api),
        ("123 resolve_path 部分命中", test_pan123_resolve_path_partial_cache_hit),
        ("123 resolve_path 找不到返回 remaining", test_pan123_resolve_path_not_found_returns_remaining),
        ("123 resolve_path invalidate 缓存 parent", test_pan123_resolve_path_invalidate_cached_parent_on_not_found),
        ("123 mkdir", test_pan123_mkdir),
        ("123 mkdir 失败抛异常", test_pan123_mkdir_failure_raises),
        ("123 upload 委托", test_pan123_upload_delegates),
        ("123 upload 无效响应抛异常", test_pan123_upload_invalid_response_raises),
        ("115 resolve_path 全缓存命中", test_pan115_resolve_path_full_cache_hit),
        ("115 resolve_path 全 miss walk", test_pan115_resolve_path_cache_miss_walks_api),
        ("115 resolve_path invalidate 缓存 parent", test_pan115_resolve_path_invalidate_cached_parent_on_not_found),
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