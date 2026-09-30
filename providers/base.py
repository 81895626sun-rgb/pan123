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
    def resolve_path(self, parts: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
        """根据路径段解析为 (parent_id, remaining_parts)。

        实现规约：
        - 先查本地缓存（最大前缀匹配），命中即跳过 API
        - 未命中段逐层走 API（带分页）
        - 每找到一个 segment 就 upsert 到缓存
        - API 返回 404 / 列表中无匹配项时 invalidate 缓存中对应段的条目
        - parent_id 是 str 类型（123 用 FileId 字符串，115 用 cid 字符串）
        - remaining_parts=() 表示完全解析；非空表示未就绪，上层应退避重试

        Args:
            parts: 云端路径段元组，如 ("folder1", "folder2")

        Returns:
            (parent_id, remaining_parts)
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