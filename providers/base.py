"""云盘 Provider 抽象基类。

定义统一的网盘操作接口。每个网盘（123, 115, ...）实现此接口，
管线代码通过 Provider 调度，不直接依赖具体网盘 client。
"""
from abc import ABC, abstractmethod
from typing import Any


class CloudProvider(ABC):
    """网盘操作统一接口。

    方法签名刻意保持简单：只传业务参数，不传 client（client 在构造时注入）。
    """

    def __init__(self, name: str, client: Any):
        self.name = name      # "123" | "115"
        self.client = client  # 对应网盘的 SDK client 实例

    @abstractmethod
    def find_parent(self, parts: list) -> tuple:
        """根据路径段列表查找父目录 ID。

        Args:
            parts: 云端路径段列表，如 ["folder1", "folder2"]

        Returns:
            (parent_id, remaining_parts): parent_id 是最后找到的目录 ID，
            remaining_parts 是未找到的剩余段（空列表表示全部找到）。
        """
        ...

    @abstractmethod
    def mkdir(self, name: str, parent_id: str, cloud_path: str = "") -> str:
        """在指定父目录下创建子目录。

        Args:
            name: 目录名（仅最后一级）
            parent_id: 父目录 ID
            cloud_path: 完整云端路径（用于缓存入库等副作用，123 忽略）

        Returns:
            新创建的云盘目录 ID（str 类型，内部可能是 FileId 或 cid）。
        """
        ...

    @abstractmethod
    def upload(self, file_path: str, parent_id: str) -> Any:
        """上传文件到指定父目录。

        Raises:
            Exception: 上传失败时抛出异常，由 upload_worker 的重试机制处理。
        """
        ...