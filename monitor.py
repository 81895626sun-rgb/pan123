import os
import time
import platform
import posixpath
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from watchdog.utils.dirsnapshot import DirectorySnapshot, DirectorySnapshotDiff
from pan123 import convert_to_cloud_path #导入pan123中的方法
from pan123 import find_directory_path
from pan123 import find_cid_by_parts
from queue import Queue, Full
from client import CloudClientManager
import logging
import os
from dotenv import load_dotenv
from utils.onestrm_notifier import OneStrmNotifier
# from upload_worker import upload_task_worker
import threading

# 配置日志（只需在这里写一次）
log_file = os.path.join(os.getcwd(), 'upload.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename=log_file,
    filemode='a',
    encoding='utf-8'  # Python 3.9+ 支持
)

# 错误日志文件（只记录 ERROR 及以上级别）
error_log_file = os.path.join(os.getcwd(), 'error.log')
# 额外添加一个 ERROR 专用的 Handler
error_handler = logging.FileHandler(error_log_file, mode='a', encoding='utf-8')
error_handler.setLevel(logging.ERROR)  # 只记录 ERROR 及以上
error_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.getLogger().addHandler(error_handler)  # 添加到 root logger

# 全局上传任务队列
# MAX_RETRIES = 3  # 最大重试次数
# UPLOAD_DELAY=1.0
class UploadTask:
    __slots__ = ['local_path', 'is_dir', 'retries', 'cloud_path', 'dir_id', 'create_time', 'last_check_time', 'file_state','file_size','pan_name']
    
    def __init__(self, local_path, is_dir,pan_name):
        self.local_path = local_path  # 本地绝对路径
        self.is_dir = is_dir          # 是否为目录
        self.retries = 0             # 当前重试次数
        self.cloud_path = None       # 转换后的云路径（延迟计算）
        self.dir_id = None           # 云目录ID（延迟获取）
        self.create_time = time.time()  # 记录任务创建时间戳
        self.last_check_time = 0  # 上次检查时间
        self.file_state = None    # 文件状态（FileState）
        self.file_size = 0        # 最近检测到的文件大小  
        self.pan_name = pan_name  # 标识任务所属网盘（"123" 或 "115"）

class SimpleFileMonitor:
    """简化版文件监控器"""
    def __init__(self, path,client_115,client_123):
        self.path = path
        self.client_115 = client_115
        self.client_123 = client_123
        self.last_files = set()
        self.scan_interval = 300  # 5秒扫描间隔
        self.last_scan = 0
        self.snapshot = DirectorySnapshot(self.path)
        self.upload_queue = Queue(maxsize=1000) # 上传队列
        
    def take_snapshot(self):
        """使用DirectorySnapshot获取文件和文件夹快照"""
        new_snapshot = DirectorySnapshot(self.path)
        diff = DirectorySnapshotDiff(self.snapshot, new_snapshot)
        self.snapshot = new_snapshot

        # 打印所有变化（调试用）
        logging.info(f"[DEBUG] 新增文件: {diff.files_created}")
        logging.info(f"[DEBUG] 新增文件夹: {diff.dirs_created}")
        
        # 收集新增项
        new_items = set()
        new_items.update(diff.files_created)
        new_items.update(diff.dirs_created)
        
        # 过滤临时文件和隐藏文件
        return {item for item in new_items
                if not (os.path.basename(item).startswith(('.', '~')) or
                os.path.basename(item).lower().endswith((
                    '.tmp', '.temp', '.swp', '.bak', '.orig', '.backup', '-upload-tmp'
                )))}
        # return {item for item in new_items 
        #        if not os.path.basename(item).startswith(('~', '.'))}
    
    def check_changes(self):
        """检查文件变化并返回路径最短的新文件/文件夹"""
        now = time.time()
        if now - self.last_scan < self.scan_interval:
            return []
            
        current_files = self.take_snapshot()
        new_files = current_files - self.last_files
        self.last_files = current_files
        self.last_scan = now
        
        if not new_files:
            return []
            
        # 找到所有路径最短的文件/文件夹（基于路径深度）
        new_files_list = list(new_files)
        rel_paths = [os.path.relpath(x, self.path) for x in new_files_list]
        # 计算每个路径的深度（组成部分数量）
        # path_depths = [len(p.split(os.sep)) for p in rel_paths]
        path_depths = [len(p.replace('\\', '/').strip('/').split('/')) for p in rel_paths]
        min_depth = min(path_depths)
        shortest_indices = [i for i, depth in enumerate(path_depths) if depth == min_depth]
        result = [new_files_list[i] for i in shortest_indices]
        return result

class SimpleFileHandler(FileSystemEventHandler):
    """简化版文件处理器"""
    def __init__(self, monitor):
        super().__init__()
        self.monitor = monitor
        
    def on_created(self, _):
        pass
    def on_modified(self, _):
        pass
    def on_moved(self, _):
        pass
    def on_deleted(self, _):
        pass



#处理新增文件
def handle_file_creation_115(client, monitor, file):
    """
    新增文件直接加入上传队列（同时上传到 123 和 115 网盘）
    允许部分网盘上传，并记录未找到目录的错误
    
    Args:
        client: 115 网盘客户端实例
        monitor: 监控实例（包含上传队列）
        file: 文件绝对路径
    """
    logging.info(f"115网盘处理文件创建事件，文件路径: {file}")
    
    # 1. 计算云端路径（去掉本地前缀，替换为云端前缀）
    cloud_full_path = convert_to_cloud_path(
        local_root=os.getenv('local_root'),
        cloud_prefix=os.getenv('cloud_prefix'),
        local_full_path=file
    )
    
    # 2. 拆分路径（去掉文件名，只保留文件夹）
    parts = [p for p in cloud_full_path.split('/') if p][:-1]
    
    # 3. 查找 115 网盘的目录 ID
    result_cid_115, remaining_path_115 = find_cid_by_parts(client, parts)
    has_115 = not remaining_path_115
    
    # 4. 检查并处理 115 网盘任务
    if has_115:
        task_115 = UploadTask(
            local_path=file,
            is_dir=os.path.isdir(file),
            pan_name="115"
        )
        task_115.dir_id = result_cid_115
        task_115.cloud_path= cloud_full_path   
        try:
            monitor.upload_queue.put_nowait(task_115)
            # print(f"[手动测试] 队列大小: {monitor.upload_queue.qsize()}")  # 观察是否变化
            logging.info(f"文件 {file} 已加入 115 网盘上传队列")
        except Full:
            logging.error(f"警告：115 网盘上传队列已满，丢弃任务: {file}")
    else:
        logging.error(f"错误：115 网盘未找到路径: {remaining_path_115}")

def handle_file_creation_123(client: object, monitor: object, file: str) -> bool:
    """
    处理123网盘文件创建（完整独立逻辑）
    
    Args:
        client: 123网盘客户端实例
        monitor: 监控实例（含上传队列）
        file: 文件绝对路径
        
    Returns:
        bool: 是否成功加入队列（True=成功）
    """
        # 1. 计算云端路径（保留原逻辑）
    logging.info(f"123网盘处理文件创建事件，文件路径: {file}")
    cloud_full_path = convert_to_cloud_path(
        local_root=os.getenv('local_root'),
        cloud_prefix=os.getenv('cloud_prefix'),
        local_full_path=file
    )
        
    # 2. 拆分路径（保留原逻辑）
    parts = [p for p in cloud_full_path.split('/') if p][:-1]
        
        # 3. 123网盘专属处理
    dir_id, remaining = find_directory_path(client, parts)
    has_123 = not remaining
    if has_123:
        task_123 = UploadTask(
            local_path=file,
            is_dir=os.path.isdir(file),
            pan_name="123"
        )
        task_123.dir_id = dir_id
        task_123.cloud_path= cloud_full_path   
        try:
            monitor.upload_queue.put_nowait(task_123)
            # print(f"[手动测试] 队列大小: {monitor.upload_queue.qsize()}")  # 观察是否变化
            logging.info(f"文件 {file} 已加入 123 网盘上传队列")
        except Full:
            logging.error(f"警告：123 网盘上传队列已满，丢弃任务: {file}")
    else:
        logging.error(f"错误：123 网盘未找到路径: {remaining}")

#处理新增文件夹
def handle_dir_creation(client_123,client_115,monitor,file):
    """
        新增文件夹递归新建再加入上传队列
        :param client: 云存储客户端实例
        :monitor：监控实例
        :file：文件绝对路径
        """
    logging.info(f"处理文件夹创建事件，文件夹路径: {file}")
    cloud_full_path = convert_to_cloud_path(local_root=os.getenv('local_root'),cloud_prefix=os.getenv('cloud_prefix'),local_full_path=file)
    parts = [p for p in cloud_full_path.split('/') if p][:-1] #父目录列表
    # 3. 查找 123 网盘的目录 ID
    result_cid_123, remaining_path_123 = find_directory_path(client=client_123, parts=parts)
    
    # 4. 查找 115 网盘的目录 ID
    result_cid_115, remaining_path_115 = find_cid_by_parts(client=client_115, parts=parts)

    if not remaining_path_123:
        logging.info(f"123pan的文件夹 {file} 已加入上传队列，目标 CID: {result_cid_123}")
        process_folder_recursive(local_path=file,parent_id=result_cid_123,client=client_123,monitor=monitor)
    else:
        logging.error(f"123网盘无法找到文件夹 {file} 对应的 CID，跳过上传")

    if not remaining_path_115:
        logging.info(f"115pan的文件夹 {file} 已加入上传队列，目标 CID: {result_cid_115}")
        process_folder_recursive(local_path=file,parent_id=result_cid_115,client=client_115,monitor=monitor)
    else:
        logging.error(f"115网盘无法找到文件夹 {file} 对应的 CID，跳过上传")
    
#递归处理文件夹并立即创建云目录

def process_folder_recursive(client, local_path, parent_id, monitor=None):
    """
    通用递归处理文件夹函数（自动识别客户端类型）
    
    参数:
        client - 云存储客户端实例(需实现fs_mkdir方法)
        local_path - 本地文件夹路径
        parent_id - 父文件夹在云端的ID
        monitor - 监控对象(可选)
    """
    folder_name = os.path.basename(local_path)
    logging.info(f"[DEBUG] 扫描目录: {local_path}")
    # 判断客户端类型并获取当前目录ID
    client_type = None
    current_id = None
    
    try:
        if hasattr(client, 'fs_mkdir'):
            mkdir_res = client.fs_mkdir(folder_name, parent_id)
            
            # 根据返回结构判断客户端类型
            if 'code' in mkdir_res and mkdir_res['code'] == 0:
                # 123网盘返回结构
                current_id = mkdir_res['data']['Info']['FileId']
                client_type = '123'
            elif 'cid' in mkdir_res:
                # 115网盘返回结构
                current_id = mkdir_res['cid']
                client_type = '115'
            else:
                raise Exception(f"无法识别的返回结构: {str(mkdir_res)}")
        else:
            raise AttributeError("客户端没有实现fs_mkdir方法")
            
    except Exception as e:
        logging.error(f"创建云文件夹失败: {folder_name} - {str(e)}")
        return

    # 处理目录内容
    for item in os.listdir(local_path):
        item_path = os.path.join(local_path, item)
        
        if os.path.isdir(item_path):
            process_folder_recursive(client, item_path, current_id, monitor)
        else:
            try:
                if client_type == '123':
                    handle_file_creation_123(client, monitor, item_path)
                elif client_type == '115':
                    handle_file_creation_115(client, monitor, item_path)
                else:
                    logging.error(f"未知客户端类型，无法处理文件: {item_path}")
            except Exception as e:
                logging.error(f"处理文件失败: {item_path} - {str(e)}")


#启动目录监控
def start_monitoring(path,client_115,client_123):
    """启动目录监控"""
    monitor = SimpleFileMonitor(path,client_115,client_123)  
    event_handler = SimpleFileHandler(monitor)
    observer = Observer()
    observer.schedule(event_handler, path, recursive=True)
    observer.start()


    # 启动上传工作线程
    from upload_worker import upload_task_worker
    upload_thread = threading.Thread(target=upload_task_worker, args=(monitor.upload_queue,))
    upload_thread.daemon = True
    upload_thread.start()
    
    try:
        logging.info(f"开始监控目录: {path} (平台: {platform.system()})")
        logging.info(f"扫描间隔: {monitor.scan_interval}秒")
        last_check = time.monotonic()
        check_interval = monitor.scan_interval
        while True:
            now = time.time()
            next_check = last_check + check_interval
            if now >= next_check:
                new_items = monitor.check_changes()
            
                for item_path in new_items:
                    OneStrmNotifier.notify_file_creation()
                    try:
                    # 检查路径是否存在（避免竞态条件）
                        if not os.path.exists(item_path):
                            logging.error(f"警告: 路径已不存在: {item_path}")
                            continue
                    
                        # 判断是文件还是目录
                        if os.path.isdir(item_path):
                            # 处理目录
                            handle_dir_creation(client_123=client_123, client_115=client_115, monitor=monitor, file=item_path)
                        else:
                        # 处理文件
                            handle_file_creation_123(client_123, monitor, item_path)
                            handle_file_creation_115(client_115, monitor, item_path)

                    
                    except Exception as e:
                        logging.error(f"处理路径时出错 {item_path}: {str(e)}")
                last_check = now
            else:
                sleep_time = min(next_check - time.time(),6.0)
                if sleep_time > 0.1:
                    time.sleep(sleep_time)

            # time.sleep(0.1)
            
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    import sys
    load_dotenv()
    target_path = os.getenv('MONITOR_DIR')
    print(target_path)
    if not target_path:
        logging.error("错误: 未在 .env 文件中配置 MONITOR_DIR")
        sys.exit(1)
    if not os.path.isdir(target_path):
        logging.error(f"错误: 目录 '{target_path}' 不存在")
        sys.exit(1)
    client_123 = CloudClientManager().get_client('123')  # 123云盘客户端
    client_115 = CloudClientManager().get_client('115')  # 115云盘客户端
    start_monitoring(path=target_path,client_115=client_115,client_123=client_123)
