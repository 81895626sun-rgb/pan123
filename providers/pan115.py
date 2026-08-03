"""115 网盘 Provider 实现。

封装 115 网盘特定的目录查找（含 SQLite 缓存）、创建目录（含缓存入库）、上传逻辑。
"""
import logging
from providers.base import CloudProvider
from pan123 import find_cid_by_parts, upload_file


class Pan115Provider(CloudProvider):
    """115 网盘 Provider。"""

    def __init__(self, client):
        super().__init__(name="115", client=client)

    def find_parent(self, parts: list) -> tuple:
        return find_cid_by_parts(client=self.client, parts=parts)

    def mkdir(self, name: str, parent_id: str, cloud_path: str = "") -> str:
        res = self.client.fs_mkdir(name, parent_id)
        if 'cid' not in res:
            raise RuntimeError(f"115 fs_mkdir 失败: {res}")

        new_cid = res['cid']
        logging.info(f"115网盘目录已创建: {name} -> cid={new_cid}")

        # 立即入库，避免后续子文件事件来时重复 API 查询（与原 handle_dir_creation 行为一致）
        if cloud_path:
            try:
                from cid_db_115 import upsert_mapping
                upsert_mapping("/" + cloud_path, new_cid)
                logging.info(f"115目录缓写入库: /{cloud_path} -> {new_cid}")
            except Exception as e:
                logging.warning(f"115目录入库失败（非致命）: {e}")

        return str(new_cid)

    def upload(self, file_path: str, parent_id: str) -> None:
        """上传文件。失败时抛出异常，由上层重试机制处理。"""
        upload_file(
            client=self.client,
            file_path=file_path,
            pid=parent_id
        )
        logging.info(f"115上传成功: {file_path}")