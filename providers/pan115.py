"""115 网盘 Provider 实现。

封装 115 网盘特定的目录查找（含 SQLite 缓存）、创建目录（含缓存入库）、上传逻辑。
"""
import logging
import random
import time
from typing import Optional

from requests.exceptions import RequestException

from providers._cid_cache import PathIdCache, format_path_key
from providers.base import CloudProvider
from pan123 import upload_file


# 115 专用缓存：复用原 cid_db_115 的 dir_cid_mapping 表（保证已有数据兼容）
_cache = PathIdCache(table_name="dir_cid_mapping", id_column="cid")


class Pan115Provider(CloudProvider):
    """115 网盘 Provider。"""

    def __init__(self, client):
        super().__init__(name="115", client=client)

    # ──────────────────────────────────────────────────────────────────
    # resolve_path：先查缓存最大前缀，未命中段走 API + 写缓存 + 懒失效
    # ──────────────────────────────────────────────────────────────────

    def resolve_path(self, parts: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
        if not parts:
            return "0", ()

        # Phase 1: 用缓存找最大已解析前缀
        current_id = "0"
        cached_idx = 0
        while cached_idx < len(parts):
            cached = _cache.get(format_path_key(parts[:cached_idx + 1]))
            # 缓存里 0 是无效值（根目录不需要缓存）
            if cached and cached != "0":
                current_id = cached
                cached_idx += 1
            else:
                break

        if cached_idx == len(parts):
            return current_id, ()

        # Phase 2: 走 API 补全
        remaining = parts[cached_idx:]
        for i, part in enumerate(remaining):
            sub_path_key = format_path_key(parts[:cached_idx + i + 1])
            found, new_cid = self._walk_one_level(current_id, part)

            if not found:
                self._maybe_invalidate_parent(parts, cached_idx + i, current_id)
                return current_id, remaining[i:]

            _cache.upsert(sub_path_key, str(new_cid))
            current_id = str(new_cid)

        return current_id, ()

    def _maybe_invalidate_parent(
        self, parts: tuple[str, ...], parent_idx: int, parent_id: str
    ) -> None:
        """115 版懒失效：与 123 同语义，仅当 parent 缓存里**确实是这个 cid**时才清。"""
        if parent_idx <= 0:
            return
        parent_path_key = format_path_key(parts[:parent_idx])
        cached = _cache.get(parent_path_key)
        if cached == parent_id:
            _cache.invalidate(parent_path_key)
            logging.warning(
                f"115 缓存失效：{parent_path_key!r}（API walk 在其下找不到目标段，"
                f"疑似目录被删/改名，下次重 walk）"
            )

    def _walk_one_level(self, parent_id: str, segment: str) -> tuple[bool, Optional[str]]:
        """走一层（不带缓存）：在 parent_id 下翻页找名为 segment 的目录。

        与原 pan123.find_cid_by_parts 内层循环一致：
        - payload: cid, limit=28, offset, o=user_ptime, asc=0
        - 单次失败重试 3 次（随机 1~3s）
        - 找不到 / 重试耗尽 / 异常都返回 (False, None)
        """
        offset = 0
        max_retries = 3
        while True:
            payload = {
                "cid": parent_id,
                "limit": 28,
                "offset": offset,
                "o": "user_ptime",
                "asc": 0,
            }

            retry_count = 0
            response = None
            while retry_count < max_retries:
                try:
                    response = self.client.fs_files(payload)
                    time.sleep(random.uniform(1, 3))
                    if (
                        isinstance(response, dict)
                        and response.get("state") is True
                        and response.get("errNo") == 0
                    ):
                        break
                    err_no = response.get("errNo", "Unknown") if isinstance(response, dict) else "Unknown"
                    err_msg = response.get("error", "Unknown error") if isinstance(response, dict) else "Unknown error"
                    logging.error(
                        f"115 fs_files 失败（{retry_count + 1}/{max_retries}），"
                        f"errNo={err_no} msg={err_msg}"
                    )
                    raise RequestException(f"API Error: {err_msg}")
                except RequestException as e:
                    logging.error(f"115 请求异常（{retry_count + 1}/{max_retries}）：{e}")
                    time.sleep(random.uniform(1, 3))
                    retry_count += 1

            if retry_count >= max_retries:
                err_no = response.get("errNo", "Unknown") if isinstance(response, dict) else "Unknown"
                logging.error(f"115 重试 {max_retries} 次仍失败，最终 errNo={err_no}")
                return False, None

            # 找到目标段（response 已确认有效）
            for item in response.get("data", []):
                if isinstance(item, dict) and item.get("n") == segment:
                    return True, str(item["cid"])

            # 当前页没找到，看是否还有下一页
            if len(response.get("data", [])) < 28:
                return False, None

            offset += 28
            time.sleep(0.5)  # 避免请求过快

    # ──────────────────────────────────────────────────────────────────
    # mkdir / upload
    # ──────────────────────────────────────────────────────────────────

    def mkdir(self, name: str, parent_id: str, cloud_path: str = "") -> str:
        """在 115 上创建子目录，成功后立即写入 path→cid 缓存。

        与原行为完全一致：传 cloud_path 时立刻 upsert_mapping，
        避免后续子文件事件重复 API 查询（详见原 handle_dir_creation 注释）。
        这里改用新 _cache，行为等价（同一张表同一列）。
        """
        res = self.client.fs_mkdir(name, parent_id)
        if "cid" not in res:
            raise RuntimeError(f"115 fs_mkdir 失败: {res}")

        new_cid = res["cid"]
        logging.info(f"115网盘目录已创建: {name} -> cid={new_cid}")

        if cloud_path:
            parts = tuple(p for p in cloud_path.split("/") if p)
            try:
                _cache.upsert(format_path_key(parts), str(new_cid))
                logging.info(f"115目录缓写入库: {format_path_key(parts)} -> {new_cid}")
            except Exception as e:
                # 与原行为一致：缓存失败非致命
                logging.warning(f"115目录入库失败（非致命）: {e}")

        return str(new_cid)

    def upload(self, file_path: str, parent_id: str) -> None:
        """上传文件。失败时抛出异常，由上层重试机制处理。"""
        upload_file(
            client=self.client,
            file_path=file_path,
            pid=parent_id,
        )
        logging.info(f"115上传成功: {file_path}")