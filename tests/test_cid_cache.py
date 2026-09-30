"""PathIdCache 单元测试 —— 通用缓存实现的正确性 / 隔离性 / SQL 注入防御。

每个测试用 pytest tmp_path + monkeypatch 把 sqlite3.connect 重定向到临时文件，
不污染真实的 cid_mapping.db。pytest 不可用时退回到内置 run_all runner。
"""
import os
import sys
import sqlite3
import tempfile

# 让脚本无论从哪里执行都能导入项目根目录下的模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from providers._cid_cache import PathIdCache, format_path_key


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures（兼容 pytest + 直接运行两种模式）
# ══════════════════════════════════════════════════════════════════════════════

class _IsolatedCache:
    """为单个测试创建一组隔离的 PathIdCache 实例。

    重写 sqlite3.connect 到临时文件，绕过默认的 cid_mapping.db，
    防止测试污染生产数据。
    """
    def __init__(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        self._original_connect = sqlite3.connect
        # Monkey-patch sqlite3.connect 模块级（_cid_cache 调的是 sqlite3.connect）。
        # 忽略所有 positional args，只把 db_path + kwargs 给原函数：
        # 原函数 signature 是 connect(database, [timeout, ...])，第二个 positional 期望数字，
        # 不能透传调用方的 "cid_mapping.db" 字符串。
        sqlite3.connect = lambda *a, **kw: self._original_connect(self.db_path, **kw)
        # 清空单例连接 + 实例注册
        PathIdCache._conn = None
        PathIdCache._instances.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # 还原 + 清理
        sqlite3.connect = self._original_connect
        PathIdCache._conn = None
        PathIdCache._instances.clear()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# format_path_key：路径格式契约
# ══════════════════════════════════════════════════════════════════════════════

def test_format_path_key_basic():
    """基本拼接：领头 '/' + '/' join"""
    assert format_path_key(("a", "b", "c")) == "/a/b/c"


def test_format_path_key_single():
    """单元素也带前导 /"""
    assert format_path_key(("only",)) == "/only"


def test_format_path_key_empty():
    """空 tuple → 仅 '/'"""
    assert format_path_key(()) == "/"


# ══════════════════════════════════════════════════════════════════════════════
# PathIdCache: get / upsert / invalidate
# ══════════════════════════════════════════════════════════════════════════════

def test_upsert_then_get():
    """upsert 后 get 能读出"""
    with _IsolatedCache():
        c = PathIdCache(table_name="test_basic", id_column="xid")
        assert c.get("/a/b") is None
        c.upsert("/a/b", "123")
        assert c.get("/a/b") == "123"


def test_get_missing_returns_none():
    """不存在的 path 返回 None，不抛异常"""
    with _IsolatedCache():
        c = PathIdCache(table_name="test_missing", id_column="xid")
        assert c.get("/never/seen") is None


def test_upsert_overwrites():
    """重复 upsert 同一 path 会覆盖（INSERT OR REPLACE）"""
    with _IsolatedCache():
        c = PathIdCache(table_name="test_overwrite", id_column="xid")
        c.upsert("/a", "111")
        c.upsert("/a", "222")
        assert c.get("/a") == "222"


def test_invalidate_existing_returns_true():
    """invalidate 存在的 path 返回 True（表示真删了一行）"""
    with _IsolatedCache():
        c = PathIdCache(table_name="test_inv_existing", id_column="xid")
        c.upsert("/a/b", "id1")
        assert c.invalidate("/a/b") is True
        assert c.get("/a/b") is None


def test_invalidate_missing_returns_false():
    """invalidate 不存在的 path 返回 False，但**不抛异常**（幂等）"""
    with _IsolatedCache():
        c = PathIdCache(table_name="test_inv_missing", id_column="xid")
        assert c.invalidate("/not/there") is False


def test_invalidate_is_idempotent():
    """同一 path 连续 invalidate 多次：第二次起返回 False，不抛异常"""
    with _IsolatedCache():
        c = PathIdCache(table_name="test_inv_idem", id_column="xid")
        c.upsert("/a", "1")
        assert c.invalidate("/a") is True
        assert c.invalidate("/a") is False
        assert c.invalidate("/a") is False


def test_invalidate_does_not_affect_other_paths():
    """invalidate 只删单行，不级联"""
    with _IsolatedCache():
        c = PathIdCache(table_name="test_inv_isolation", id_column="xid")
        c.upsert("/a", "1")
        c.upsert("/a/b", "2")
        c.upsert("/a/b/c", "3")
        c.invalidate("/a/b")
        assert c.get("/a") == "1"
        assert c.get("/a/b") is None
        assert c.get("/a/b/c") == "3"


# ══════════════════════════════════════════════════════════════════════════════
# 安全：SQL 标识符白名单
# ══════════════════════════════════════════════════════════════════════════════

def test_invalid_table_name_rejected():
    """含特殊字符的 table_name 必须在 __init__ 就抛 ValueError（防止注入）"""
    with _IsolatedCache():
        try:
            PathIdCache(table_name="evil; DROP TABLE users;--", id_column="xid")
            assert False, "应抛出 ValueError"
        except ValueError:
            pass


def test_invalid_id_column_rejected():
    """含特殊字符的 id_column 同样必须抛 ValueError"""
    with _IsolatedCache():
        try:
            PathIdCache(table_name="ok", id_column="xid;--")
            assert False, "应抛出 ValueError"
        except ValueError:
            pass


def test_table_name_starting_with_digit_rejected():
    """标识符以数字开头也非法（SQL 语法不允许）"""
    with _IsolatedCache():
        try:
            PathIdCache(table_name="123table", id_column="xid")
            assert False, "应抛出 ValueError"
        except ValueError:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# 同一 table_name 重复创建防御
# ══════════════════════════════════════════════════════════════════════════════

def test_duplicate_table_name_rejected():
    """同一 table_name 不能创建两个实例（避免覆盖）"""
    with _IsolatedCache():
        PathIdCache(table_name="dup", id_column="xid")
        try:
            PathIdCache(table_name="dup", id_column="xid")
            assert False, "应抛出 ValueError"
        except ValueError as e:
            assert "已存在" in str(e)


def test_different_table_names_can_coexist():
    """不同 table_name 可以共存，对应 123 / 115 两张表"""
    with _IsolatedCache():
        c_123 = PathIdCache(table_name="dir_fileid_mapping_123", id_column="file_id")
        c_115 = PathIdCache(table_name="dir_cid_mapping", id_column="cid")
        c_123.upsert("/path/a", "fid_123")
        c_115.upsert("/path/a", "cid_115")
        assert c_123.get("/path/a") == "fid_123"
        assert c_115.get("/path/a") == "cid_115"
        # 互不影响：清 123 不影响 115
        c_123.invalidate("/path/a")
        assert c_123.get("/path/a") is None
        assert c_115.get("/path/a") == "cid_115"


def test_path_column_parameterized():
    """path_column 参数可定制：兼容老 cid_db_115.py 用 'dir_path' 列名"""
    with _IsolatedCache():
        # 用 dir_path 当 path 列名（与原 cid_db_115.py 一致）
        c = PathIdCache(
            table_name="legacy_dir_cid_mapping",
            id_column="cid",
            path_column="dir_path",
        )
        c.upsert("/a/b", "123")
        assert c.get("/a/b") == "123"
        assert c.invalidate("/a/b") is True


def test_legacy_table_with_dir_path_column_compat():
    """模拟老 cid_db_115.py 已建好的表（列名 dir_path），新 PathIdCache 接入不报错。

    模拟场景：
    1. 旧版已写入 (旧)  schema  (dir_path, cid)
    2. 新版 PathIdCache 用 path_column='dir_path' 接入
    3. 新版的 get / upsert / invalidate 都能正确工作
    """
    with _IsolatedCache():
        # 1. 模拟老 cid_db_115.py 建表（旧 schema）
        import sqlite3 as sqlite3_mod
        # 拿到底层 patched 连接
        conn = sqlite3_mod.connect()
        conn.execute("""
                CREATE TABLE dir_cid_mapping (
                    dir_path TEXT PRIMARY KEY,
                    cid TEXT NOT NULL UNIQUE,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        conn.execute(
            "INSERT INTO dir_cid_mapping (dir_path, cid) VALUES (?, ?)",
            ("/legacy/old", "99999"),
        )
        conn.commit()

        # 2. 新版 PathIdCache 用 path_column="dir_path" 接入
        c = PathIdCache(
            table_name="dir_cid_mapping",
            id_column="cid",
            path_column="dir_path",
        )

        # 3. 验证能读出老数据、能写入新数据、能 invalidate
        assert c.get("/legacy/old") == "99999", "应能读出老数据"
        c.upsert("/legacy/new", "88888")
        assert c.get("/legacy/new") == "88888"
        c.invalidate("/legacy/old")
        assert c.get("/legacy/old") is None


# ══════════════════════════════════════════════════════════════════════════════
# 调试 / 测试用：count / clear
# ══════════════════════════════════════════════════════════════════════════════

def test_count_and_clear():
    """count / clear 仅供测试"""
    with _IsolatedCache():
        c = PathIdCache(table_name="test_count", id_column="xid")
        assert c.count() == 0
        c.upsert("/a", "1")
        c.upsert("/b", "2")
        assert c.count() == 2
        c.clear()
        assert c.count() == 0


# ══════════════════════════════════════════════════════════════════════════════
# Runner（直接运行模式）
# ══════════════════════════════════════════════════════════════════════════════

def run_all():
    tests = [
        ("format_path_key 基本拼接", test_format_path_key_basic),
        ("format_path_key 单元素", test_format_path_key_single),
        ("format_path_key 空 tuple", test_format_path_key_empty),
        ("upsert 后 get 读出", test_upsert_then_get),
        ("get 不存在返回 None", test_get_missing_returns_none),
        ("upsert 覆盖", test_upsert_overwrites),
        ("invalidate 存在返回 True", test_invalidate_existing_returns_true),
        ("invalidate 不存在返回 False", test_invalidate_missing_returns_false),
        ("invalidate 幂等", test_invalidate_is_idempotent),
        ("invalidate 不级联", test_invalidate_does_not_affect_other_paths),
        ("非法 table_name 拒绝", test_invalid_table_name_rejected),
        ("非法 id_column 拒绝", test_invalid_id_column_rejected),
        ("标识符以数字开头拒绝", test_table_name_starting_with_digit_rejected),
        ("重复 table_name 拒绝", test_duplicate_table_name_rejected),
        ("不同 table_name 共存", test_different_table_names_can_coexist),
        ("path_column 参数化", test_path_column_parameterized),
        ("老表 dir_path 列兼容", test_legacy_table_with_dir_path_column_compat),
        ("count / clear", test_count_and_clear),
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