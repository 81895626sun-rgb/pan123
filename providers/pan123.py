"""123 云盘 Provider 实现。

封装 123 云盘特定的目录查找、创建目录、上传逻辑。
"""
import logging
from providers.base import CloudProvider
from pan123 import find_directory_path, smart_upload


class Pan123Provider(CloudProvider):
    """123 云盘 Provider。"""

    def __init__(self, client):
        super().__init__(name="123", client=client)

    def find_parent(self, parts: list) -> tuple:
        return find_directory_path(client=self.client, parts=parts)

    def mkdir(self, name: str, parent_id: str, cloud_path: str = "") -> str:
        res = self.client.fs_mkdir(name, parent_id)
        if 'code' not in res or res['code'] != 0:
            raise RuntimeError(f"123 fs_mkdir 失败: {res}")
        new_id = res['data']['Info']['FileId']
        logging.info(f"123网盘目录已创建: {name} -> id={new_id}")
        return str(new_id)

    def upload(self, file_path: str, parent_id: str) -> str:
        """上传文件，返回实际文件 ID（用于成功日志）。失败时抛出异常。
        返回值与 upload_worker.py 原逻辑一致：优先 file_info.FileId，其次 Info.FileId。"""
        result = smart_upload(
            client=self.client,
            file_source=file_path,
            parent_id=parent_id
        )
        # 校验上传结果（与 upload_worker.py 原逻辑完全一致）
        if not (result and (
            result.get('data', {}).get('Info') or
            result.get('data', {}).get('file_info')
        )):
            raise RuntimeError(f"无效的API响应: {str(result)[:100]}")
        file_info = result['data'].get('file_info') or result['data'].get('Info')
        file_id = file_info.get('FileId') if file_info else '未知'
        logging.info(f"123上传成功: {file_path} -> 文件ID: {file_id}")
        return str(file_id)