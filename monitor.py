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
from queue import Queue, Full, Empty
from client import CloudClientManager
import logging
import os
from dotenv import load_dotenv
from utils.onestrm_notifier import OneStrmNotifier
# from upload_worker import upload_task_worker
import threading
from file_checker import FileGrowthChecker, FileState

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
    """事件驱动版文件监控器（防抖队列管理）"""
    def __init__(self, path, client_115, client_123):
        self.path = path
        self.client_115 = client_115
        self.client_123 = client_123
        self.upload_queue = Queue(maxsize=1000) # 正式上传队列传递给 worker
        self.pending_queue = Queue(maxsize=1000) # 待确认防抖队列，收集即时事件
        self.pending_tasks = set() # 辅助集合，防止同一路径反复加入防抖队列

class SimpleFileHandler(FileSystemEventHandler):
    """事件驱动版文件处理器"""
    def __init__(self, monitor):
        super().__init__()
        self.monitor = monitor
        
    def is_temp_file(self, filepath):
        name = os.path.basename(filepath)
        if name.startswith(('.', '~')): 
            return True
        if name.lower().endswith(('.tmp', '.temp', '.swp', '.bak', '.orig', '.backup', '-upload-tmp')): 
            return True
        return False

    def handle_event(self, path):
        if self.is_temp_file(path):
            return
        
        # 只在不在待处理集合中时加入队列，防止重复事件轰炸
        if path not in self.monitor.pending_tasks:
            self.monitor.pending_tasks.add(path)
            try:
                self.monitor.pending_queue.put_nowait(path)
                logging.info(f"系统事件捕获 [加入防抖队列]: {path}")
            except Full:
                logging.warning(f"防抖队列已满，丢弃事件: {path}")

    def on_created(self, event):
        self.handle_event(event.src_path)

    def on_moved(self, event):
        # 针对在监听文件夹内外挪动的情况
        self.handle_event(event.dest_path)

def debounce_worker_thread(client_123, client_115, monitor):
    """守护线程：循环检查 pending_queue 里的文件是否已经结束跳动（防等待抢跑）"""
    logging.info("防抖工作线程 (Debounce Worker) 已启动，等待底层系统事件推送...")
    while True:
        try:
            # 阻塞获取新变化的文件，超时进行循环空转
            filepath = monitor.pending_queue.get(timeout=1.0)
            
            # 使用 FileGrowthChecker 采样
            state, size = FileGrowthChecker.check_growth(filepath)
            
            if state == FileState.NOT_FOUND:
                logging.warning(f"文件在其生长防抖期间被移除或重命名: {filepath}")
                monitor.pending_tasks.discard(filepath)
                monitor.pending_queue.task_done()
                
            elif state == FileState.GROWING:
                # 仍在增长中，将其送回队列尾部继续等待并放缓重试
                monitor.pending_queue.put(filepath)
                monitor.pending_queue.task_done()
                time.sleep(0.5)
                
            elif state == FileState.READY:
                # 状态稳定，可以安全地发到路由层处理
                logging.info(f"文件复制/生成完毕 (防抖就绪): {filepath} - 最终大小: {size} byte")
                monitor.pending_tasks.discard(filepath)
                monitor.pending_queue.task_done()
                
                # 执行原先的分发路由
                OneStrmNotifier.notify_file_creation()
                
                try:
                    if os.path.exists(filepath): # 再次确保瞬间没被干掉
                        if os.path.isdir(filepath):
                            handle_dir_creation(client_123=client_123, client_115=client_115, monitor=monitor, file=filepath)
                        else:
                            handle_file_creation_123(client_123, monitor, filepath)
                            handle_file_creation_115(client_115, monitor, filepath)
                except Exception as e:
                    logging.error(f"分派路径时出错 {filepath}: {str(e)}")
                    
            elif state == FileState.ERROR:
                logging.error(f"探测遇到不可恢复系统错误: {filepath}")
                monitor.pending_tasks.discard(filepath)
                monitor.pending_queue.task_done()
                
        except Empty:
            continue
        except Exception as e:
            logging.error(f"防抖工作线程捕获全局异常: {str(e)}")
            time.sleep(1)


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
    """启动事件驱动版目录监控"""
    monitor = SimpleFileMonitor(path,client_115,client_123)  
    event_handler = SimpleFileHandler(monitor)
    observer = Observer()
    observer.schedule(event_handler, path, recursive=True)
    observer.start()


    # 启动上传工作线程 (Upload Worker)
    from upload_worker import upload_task_worker
    upload_thread = threading.Thread(target=upload_task_worker, args=(monitor.upload_queue,))
    upload_thread.daemon = True
    upload_thread.start()
    
    # 启动防抖守护线程 (Debounce Worker)
    debounce_thread = threading.Thread(target=debounce_worker_thread, args=(client_123, client_115, monitor))
    debounce_thread.daemon = True
    debounce_thread.start()
    
    try:
        logging.info(f"开始事件驱动监控目录: {path} (平台: {platform.system()})")
        # 主线程挂起，由 watchdog 自动捕获底层的变动推送
        while True:
            time.sleep(1.0)
            
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
