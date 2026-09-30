"""通用 path → id 缓存。

123 / 115 共用同一套实现，仅表名和列名不同。
与原 cid_db_115.py 同样的约束：单例 SQLite 连接 + RLock，
因为同一连接并发 execute 在 SQLite 里不安全（即使 check_same_thread=False）。
上传是单线程工作，锁开销可忽略。

设计要点：
- 实例按 table_name 唯一：避免重复创建 / 表冲突
- get / upsert / invalidate 全部带锁，调用方无需加锁
- invalidate 是幂等的：不存在的 path 返回 False，不抛异常
- 路径键的格式化走 format_path_key() 显式契约，避免散落 "/" + "/".join()
"""
import logging
import re
import sqlite3
import threading
from typing import Optional


# 标识符白名单（仅字母/数字/下划线），防止 SQL 注入和拼写错误
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check_ident(name: str, kind: str) -> None:
    if not _IDENT.match(name):
        raise ValueError(
            f"非法的 {kind} 标识符: {name!r}（只允许字母/数字/下划线，且不能以数字开头）"
        )


def format_path_key(parts: tuple[str, ...]) -> str:
    """路径缓存键的格式契约：领头 '/' + '/' join。

    所有调用方必须走这个函数，不要自己拼字符串。统一格式后，
    不同 provider 的缓存 key 不会出现"形似实异"的脏数据。
    """
    return "/" + "/".join(parts)


class PathIdCache:
    """通用 path → id 缓存（单例连接 + 表名参数化）。"""

    # 模块级单例 SQLite 连接 + RLock，与 cid_db_115.py 同样的约束
    _conn: Optional[sqlite3.Connection] = None
    _lock = threading.RLock()

    # 注册表：table_name -> PathIdCache 实例，避免重复创建
    _instances: dict[str, "PathIdCache"] = {}
    _instances_lock = threading.Lock()

    def __init__(self, table_name: str, id_column: str):
        """初始化缓存。table_name 与 id_column 必须是合法的 SQL 标识符。

        同一 table_name 只能创建一个实例；重复创建会抛 ValueError。
        """
        _check_ident(table_name, "table_name")
        _check_ident(id_column, "id_column")

        with PathIdCache._instances_lock:
            if table_name in PathIdCache._instances:
                raise ValueError(
                    f"PathIdCache({table_name}) 已存在，"
                    f"指向 {PathIdCache._instances[table_name]!r}"
                )
            self.table = table_name
            self.id_col = id_column
            PathIdCache._instances[table_name] = self

        self._ensure_table()

    # ------------------------------------------------------------------ #
    # 连接管理
    # ------------------------------------------------------------------ #

    @classmethod
    def _get_connection(cls) -> sqlite3.Connection:
        """获取单例 SQLite 连接（双重检查 + RLock，与 cid_db_115.py 一致）。"""
        if cls._conn is not None:
            return cls._conn
        with cls._lock:
            if cls._conn is None:
                cls._conn = sqlite3.connect(
                    "cid_mapping.db", check_same_thread=False
                )
        return cls._conn

    def _ensure_table(self) -> None:
        """建表（若不存在）。所有 PathIdCache 实例共享同一连接。"""
        with self._lock:
            conn = self._get_connection()
            # f-string 拼 DDL 是安全的：table/id_col 已被 _check_ident 校验
            ddl = (
                f"CREATE TABLE IF NOT EXISTS {self.table} ("
                f"path TEXT PRIMARY KEY, "
                f"{self.id_col} TEXT NOT NULL UNIQUE, "
                f"updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                f")"
            )
            with conn:
                conn.execute(ddl)

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #

    def get(self, path: str) -> Optional[str]:
        """查询 path 对应的 id。不存在或 SQLite 异常时返回 None（不抛出）。"""
        try:
            with self._lock:
                cur = self._get_connection().execute(
                    f"SELECT {self.id_col} FROM {self.table} WHERE path = ?",
                    (path,),
                )
                row = cur.fetchone()
            return row[0] if row else None
        except sqlite3.Error as e:
            logging.warning(
                f"PathIdCache({self.table}).get({path!r}) 失败（返回 None 兜底）: {e}"
            )
            return None

    def upsert(self, path: str, id_value: str) -> bool:
        """插入或更新 path → id_value 的映射。

        SQLite 异常会被 log 后**吞掉**，避免单次缓存写入失败导致上传流程崩溃
        （缓存是优化，不是真理；下次 resolve 会再 walk 一次重建）。
        """
        try:
            with self._lock:
                with self._get_connection():
                    self._get_connection().execute(
                        f"INSERT OR REPLACE INTO {self.table} "
                        f"(path, {self.id_col}) VALUES (?, ?)",
                        (path, id_value),
                    )
            return True
        except sqlite3.Error as e:
            logging.warning(
                f"PathIdCache({self.table}).upsert({path!r}, {id_value!r}) 失败（非致命）: {e}"
            )
            return False

    def invalidate(self, path: str) -> bool:
        """主动失效：删除 path 对应的缓存条目（API 返回 404 / 用户删目录时调用）。

        - 幂等：不存在的 path 返回 False，不抛异常
        - SQLite 异常被 log 后**吞掉**，不让上传流程崩
        """
        try:
            with self._lock:
                with self._get_connection():
                    cur = self._get_connection().execute(
                        f"DELETE FROM {self.table} WHERE path = ?",
                        (path,),
                    )
                    deleted = cur.rowcount > 0
            return deleted
        except sqlite3.Error as e:
            logging.warning(
                f"PathIdCache({self.table}).invalidate({path!r}) 失败（非致命）: {e}"
            )
            return False

    # ------------------------------------------------------------------ #
    # 调试 / 测试用
    # ------------------------------------------------------------------ #

    def count(self) -> int:
        """返回表内总行数（调试 / 测试用）。"""
        try:
            with self._lock:
                cur = self._get_connection().execute(
                    f"SELECT COUNT(*) FROM {self.table}"
                )
                row = cur.fetchone()
            return row[0] if row else 0
        except sqlite3.Error as e:
            logging.warning(f"PathIdCache({self.table}).count() 失败: {e}")
            return 0

    def clear(self) -> None:
        """清空整张表（仅供测试使用）。"""
        with self._lock:
            with self._get_connection():
                self._get_connection().execute(f"DELETE FROM {self.table}")