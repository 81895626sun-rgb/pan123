"""123 云盘 Provider 实现。

封装 123 云盘特定的目录查找（含 path→FileId 缓存）、创建目录、上传逻辑。
"""
import logging
from providers._cid_cache import PathIdCache, format_path_key
from providers.base import CloudProvider
from pan123 import smart_upload


# 123 专用缓存：表名与 115 的 dir_cid_mapping 不冲突，独立管理
_cache = PathIdCache(table_name="dir_fileid_mapping_123", id_column="file_id")


class Pan123Provider(CloudProvider):
    """123 网盘 Provider。"""

    def __init__(self, client):
        super().__init__(name="123", client=client)

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
            if cached:
                current_id = cached
                cached_idx += 1
            else:
                break

        if cached_idx == len(parts):
            return current_id, ()

        # Phase 2: 走 API 补全剩余段
        remaining = parts[cached_idx:]
        for i, part in enumerate(remaining):
            # 当前要查找的 sub_path 用于成功后写入缓存
            sub_path_key = format_path_key(parts[:cached_idx + i + 1])
            found, new_id = self._walk_one_level(current_id, part)

            if not found:
                # API 找不到 → 怀疑 parent 缓存失效，主动清掉 parent
                # parent 的 path_key：parts[:cached_idx + i]
                # （cached_idx == 0 时没有 parent，不清）
                self._maybe_invalidate_parent(parts, cached_idx + i, current_id)
                return current_id, remaining[i:]

            _cache.upsert(sub_path_key, str(new_id))
            current_id = str(new_id)

        return current_id, ()

    def _maybe_invalidate_parent(
        self, parts: tuple[str, ...], parent_idx: int, parent_id: str
    ) -> None:
        """当 API 在 parent 下找不到目标段时，怀疑 parent 缓存可能已脏。

        仅当 parent 在缓存里**确实是这个 id**时才 invalidate（避免误删刚 walk 出的临时值）。
        cached_idx == 0 表示 parent 是根 "0"，永不清根。
        """
        if parent_idx <= 0:
            return  # 根目录 / 未知 parent，不清
        parent_path_key = format_path_key(parts[:parent_idx])
        cached = _cache.get(parent_path_key)
        if cached == parent_id:
            _cache.invalidate(parent_path_key)
            logging.warning(
                f"123 缓存失效：{parent_path_key!r}（API walk 在其下找不到目标段，"
                f"疑似目录被删/改名，下次重 walk）"
            )

    def _walk_one_level(self, parent_id: str, segment: str) -> tuple[bool, str | None]:
        """走一层（不带缓存）：在 parent_id 下翻页找名为 segment 的目录。

        与原 pan123.find_directory_path 内层循环完全一致的语义：
        - 走驼峰参数、code != 0 视为失败、Next == -1 终止
        - 找不到 / 异常 / code != 0 都返回 (False, None)
        """
        page = 1
        while True:
            payload = {
                "parentFileId": parent_id,
                "driveId": "0",
                "limit": "100",
                "Page": str(page),
                "orderBy": "file_id",
                "orderDirection": "desc",
                "event": "homeListFile",
                "inDirectSpace": "false",
                "trashed": "false",
            }
            try:
                # 123 上游 9/7 起要求参数名严格驼峰，p123client.fs_list_new
                # 内部 dict_key_to_lower_merge 把驼峰打成小写导致 400。
                # 绕过 fs_list_new，直接 client.request 透传驼峰键。
                response = self.client.request(
                    "file/list/new",
                    params=payload,
                    base_url="https://123pan.com",
                )
                if response.get("code") != 0:
                    logging.error(
                        f"123 列目录失败: code={response.get('code')} "
                        f"msg={response.get('message')!r}"
                    )
                    return False, None
                # 防御 data 显式为 null：.get('data', {}) 在键存在但值为 None
                # 时仍返回 None，下一行 None.get 会 AttributeError
                data = response.get("data") or {}

                for item in data.get("InfoList", []):
                    if str(item.get("Type")) == "1" and item.get("FileName") == segment:
                        return True, str(item["FileId"])

                if data.get("Next") == "-1":
                    return False, None

                page += 1
            except Exception as e:
                logging.error(f"123 查询失败: {e}")
                return False, None

    # ──────────────────────────────────────────────────────────────────
    # mkdir / upload
    # ──────────────────────────────────────────────────────────────────

    def mkdir(self, name: str, parent_id: str, cloud_path: str = "") -> str:
        """在 123 上创建子目录。

        与原行为完全一致：不主动写缓存（callers 之后会再调 resolve_path，
        那时会自动 walk + 缓存）。
        """
        res = self.client.fs_mkdir(name, parent_id)
        if "code" not in res or res["code"] != 0:
            raise RuntimeError(f"123 fs_mkdir 失败: {res}")
        new_id = res["data"]["Info"]["FileId"]
        logging.info(f"123网盘目录已创建: {name} -> id={new_id}")
        return str(new_id)

    def upload(self, file_path: str, parent_id: str) -> str:
        """上传文件，返回实际文件 ID（用于成功日志）。失败时抛出异常。
        返回值与原逻辑一致：优先 file_info.FileId，其次 Info.FileId。"""
        result = smart_upload(
            client=self.client,
            file_source=file_path,
            parent_id=parent_id,
        )
        # 校验上传结果（与原逻辑完全一致）
        if not (result and (
            result.get("data", {}).get("Info") or
            result.get("data", {}).get("file_info")
        )):
            raise RuntimeError(f"无效的API响应: {str(result)[:100]}")
        file_info = result["data"].get("file_info") or result["data"].get("Info")
        file_id = file_info.get("FileId") if file_info else "未知"
        logging.info(f"123上传成功: {file_path} -> 文件ID: {file_id}")
        # FileId 缺失时返回空串，让 worker 的 `file_id or task.dir_id` 回退到 dir_id
        return str(file_id) if file_id else ""