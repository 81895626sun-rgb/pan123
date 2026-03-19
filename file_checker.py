import time
from pathlib import Path
from typing import Tuple
from enum import Enum, auto

class FileState(Enum):
    READY   = auto()
    GROWING = auto()
    NOT_FOUND = auto()
    ERROR   = auto()

class FileGrowthChecker:
    """
    通过两次物理采样对比文件大小，判定是否复制完成。
    不再依赖 _state_map，解决脚本重启或长时间运行后的误判问题。
    """
    
    # 采样间隔，可以根据需要调整，通常 1 秒足以判断
    sample_interval: float = 1.0

    @staticmethod
    def check_growth(filepath: str) -> Tuple[FileState, int]:
        """
        核心逻辑：
        1. 获取当前大小。
        2. 强制等待 1 秒。
        3. 再次获取大小。
        4. 对比：一致则 READY，不一致则 GROWING。
        """
        try:
            p = Path(filepath).resolve()
            
            # 1. 检查是否存在
            if not p.exists():
                return (FileState.NOT_FOUND, 0)

            # 2. 获取第一次大小采样
            size_before = p.stat().st_size
            
            # 3. 强制等待一小段时间 (默认 1 秒)
            time.sleep(FileGrowthChecker.sample_interval)
            
            # 4. 获取第二次大小采样
            size_after = p.stat().st_size

            # 5. 核心对比逻辑
            # 如果两次大小完全一样，且文件不为空，说明已经复制完成
            if size_before == size_after and size_before > 0:
                return (FileState.READY, size_after)
            
            # 如果大小在变化，或者文件还是 0 字节，判定为增长中
            return (FileState.GROWING, size_after)

        except OSError:
            # 发生权限错误或文件被独占锁定时，通常意味着文件正在被写入
            return (FileState.GROWING, 0)

# 使用示例：
# state, size = FileGrowthChecker.check_growth("D:/115下载/movie.mp4")
# print(f"状态: {state.name}, 大小: {size}")