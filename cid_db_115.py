# cid_db_115.py
import sqlite3
import threading
from typing import Optional
import logging
# 单例连接
_conn = None
# RLock 保护连接创建和所有读写：SQLite 连接即使 check_same_thread=False，
# 同一连接并发 execute 仍不安全，必须串行化（上传是单线程，此锁开销可忽略）
_lock = threading.RLock()

def get_connection():
    global _conn
    if _conn is None:
        with _lock:  # 双重检查 + 锁：避免竞态创建两个连接
            if _conn is None:
                _conn = sqlite3.connect("cid_mapping.db", check_same_thread=False)
                _init_db()
    return _conn

def _init_db():
    with _lock:
        get_connection().execute("""
            CREATE TABLE IF NOT EXISTS dir_cid_mapping (
                dir_path TEXT PRIMARY KEY,
                cid TEXT NOT NULL UNIQUE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """).connection.commit()

def upsert_mapping(dir_path: str, cid: str) -> bool:
    """插入或更新目录-CID映射

    Args:
        dir_path: 目录路径（需标准化，建议以/开头）
        cid: 对应的CID字符串

    Returns:
        bool: 是否成功执行

    Raises:
        sqlite3.Error: 数据库操作异常
    """
    try:
        with _lock:
            conn = get_connection()
            with conn:  # 使用上下文管理器自动提交/回滚
                conn.execute(
                    "INSERT OR REPLACE INTO dir_cid_mapping (dir_path, cid) VALUES (?, ?)",
                    (dir_path, cid)
                )
        return True
    except sqlite3.Error as e:
        logging.error(f"Failed to upsert mapping {dir_path}->{cid}: {e}")
        raise  # 重新抛出异常让调用方处理

def get_cid(dir_path: str) -> Optional[str]:
    """查询目录对应的CID
    Args:
        dir_path: 要查询的目录路径（需以/开头，如"/data/images"）
    Returns:
        str: 找到的CID
        None: 路径不存在
    """
    with _lock:
        cur = get_connection().execute(
            "SELECT cid FROM dir_cid_mapping WHERE dir_path = ?",
            (dir_path,)
        )
        if row := cur.fetchone():  # 使用海象运算符避免重复调用
            return row[0]
    return None
