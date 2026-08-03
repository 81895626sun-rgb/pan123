import os
import time
import platform
import posixpath
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from watchdog.utils.dirsnapshot import DirectorySnapshot, DirectorySnapshotDiff
from pan123 import convert_to_cloud_path #导入pan123中的方法
from queue import Queue, PriorityQueue, Full, Empty
from providers import CloudProvider
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

# 最大重试次数（父目录未就绪时的重试上限）
MAX_RETRIES = 10

class UploadTask:
    __slots__ = ['local_path', 'is_dir', 'retries', 'cloud_path', 'dir_id', 'create_time', 'last_check_time', 'file_state', 'file_size', 'pan_name', 'available_after']
    
    def __init__(self, local_path, is_dir, pan_name):
        self.local_path = local_path  # 本地绝对路径
        self.is_dir = is_dir          # 是否为目录
        self.retries = 0              # 当前重试次数
        self.cloud_path = None        # 转换后的云路径（延迟计算）
        self.dir_id = None            # 云目录ID（延迟获取）
        self.create_time = time.time()  # 记录任务创建时间戳
        self.last_check_time = 0      # 上次检查时间
        self.file_state = None        # 文件状态（FileState）
        self.file_size = 0            # 最近检测到的文件大小
        self.pan_name = pan_name      # 标识任务所属网盘（"123" 或 "115"）
        self.available_after = 0      # 时间戳：0=立即可处理，>0=需等到该时间后再取出

class SimpleFileMonitor:
    """事件驱动版文件监控器（防抖 + 优先级队列）"""
    def __init__(self, path, providers):
        self.path = path
        self.providers = providers  # dict[str, CloudProvider]
        self.upload_queue = Queue(maxsize=1000)          # 最终上传队列，传递给 upload_worker
        self.pending_queue = Queue(maxsize=1000)         # 防抖队列，收集 watchdog 原始事件
        self.priority_queue = PriorityQueue(maxsize=2000) # 优先级队列：先目录后文件、深度由浅到深
        self.pending_tasks = set()                       # 防止同一路径反复加入防抖队列
        self.dispatched_tasks = set()                    # 防止同一路径重复入优先级队列
        self.pq_sequence = 0                             # 入队自增序号，保证同优先级下 FIFO
        self.pq_lock = threading.Lock()                  # sequence 自增线程安全锁

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

def _enqueue_priority(monitor, filepath):
    """将路径按优先级（先目录后文件、深度由浅到深）放入优先级队列"""
    is_file = 0 if os.path.isdir(filepath) else 1
    depth = filepath.count(os.sep)
    with monitor.pq_lock:
        seq = monitor.pq_sequence
        monitor.pq_sequence += 1
    try:
        monitor.priority_queue.put_nowait((is_file, depth, seq, filepath))
        logging.info(f"[优先级队列] 入队: {'文件' if is_file else '目录'} depth={depth} {filepath}")
    except Full:
        logging.error(f"[优先级队列] 队列已满，丢弃: {filepath}")


def debounce_worker_thread(monitor):
    """守护线程：循环检查 pending_queue 里的路径是否稳定，稳定后入优先级队列"""
    logging.info("防抖工作线程 (Debounce Worker) 已启动，等待底层系统事件推送...")
    while True:
        try:
            filepath = monitor.pending_queue.get(timeout=1.0)

            # 重复入队校验
            if filepath in monitor.dispatched_tasks:
                monitor.pending_tasks.discard(filepath)
                monitor.pending_queue.task_done()
                continue

            # 目录免防抖直接放行，文件需等到写入稳定
            if os.path.isdir(filepath):
                state = FileState.READY
            else:
                state, _ = FileGrowthChecker.check_growth(filepath)

            if state == FileState.NOT_FOUND:
                logging.warning(f"路径在防抖期间消失: {filepath}")
                monitor.pending_tasks.discard(filepath)
                monitor.pending_queue.task_done()

            elif state == FileState.GROWING:
                # 文件仍在写入，送回队列尾部继续等待
                logging.info(f"⏳ 文件体积仍在增长，回炉继续等待拷贝: {filepath}")
                monitor.pending_queue.put(filepath)
                monitor.pending_queue.task_done()
                time.sleep(0.5)

            elif state == FileState.READY:
                logging.info(f"路径就绪，准备入优先级队列: {filepath}")
                monitor.pending_tasks.discard(filepath)
                monitor.pending_queue.task_done()

                # 双重校验后标记已派发，入优先级队列
                if filepath in monitor.dispatched_tasks:
                    continue
                monitor.dispatched_tasks.add(filepath)

                if os.path.exists(filepath):
                    OneStrmNotifier.trigger_qmediasync()
                    _enqueue_priority(monitor, filepath)

            elif state == FileState.ERROR:
                logging.error(f"探测遇到不可恢复系统错误: {filepath}")
                monitor.pending_tasks.discard(filepath)
                monitor.pending_queue.task_done()

        except Empty:
            continue
        except Exception as e:
            logging.error(f"防抖工作线程捕获全局异常: {e}")
            time.sleep(1)


#处理新增文件
def handle_file_creation(provider, monitor, file, task=None):
    """统一文件创建处理：查父目录ID，找到则加上传队列，找不到则退避重试。
    provider: CloudProvider 实例
    task 参数用于重试场景传入已有任务对象（保留retries计数）。"""
    logging.info(f"{provider.name}网盘处理文件创建事件，文件路径: {file}")

    cloud_full_path = convert_to_cloud_path(
        local_root=os.getenv('local_root'),
        cloud_prefix=os.getenv('cloud_prefix'),
        local_full_path=file
    )
    parts = [p for p in cloud_full_path.split('/') if p][:-1]
    dir_id, remaining = provider.find_parent(parts)
    found = not remaining

    if found:
        if task is None:
            task = UploadTask(local_path=file, is_dir=False, pan_name=provider.name)
        task.dir_id = dir_id
        task.cloud_path = cloud_full_path
        try:
            monitor.upload_queue.put_nowait(task)
            logging.info(f"文件 {file} 已加入 {provider.name} 网盘上传队列")
        except Full:
            logging.error(f"警告：{provider.name} 网盘上传队列已满，丢弃任务: {file}")
    else:
        # 父目录尚未同步，退避重试
        if task is None:
            task = UploadTask(local_path=file, is_dir=False, pan_name=provider.name)
            task.cloud_path = cloud_full_path
        task.retries += 1
        if task.retries > MAX_RETRIES:
            logging.error(
                f"[任务放弃] 超过最大重试次数({MAX_RETRIES}次) | 网盘={provider.name} | "
                f"本地路径={file} | 云端路径={cloud_full_path} | "
                f"原因=父目录查找失败，remaining={remaining}"
            )
            return
        delay = task.retries * 2
        task.available_after = time.time() + delay
        is_file = 1
        depth = file.count(os.sep)
        with monitor.pq_lock:
            seq = monitor.pq_sequence
            monitor.pq_sequence += 1
        monitor.priority_queue.put((is_file, depth, seq, file, task))
        logging.warning(
            f"{provider.name}网盘父目录未就绪，{delay}秒后重试({task.retries}/{MAX_RETRIES}): "
            f"{file}，缺失段: {remaining}"
        )

#处理新增文件夹：只在云端建立对应目录，不遍历子项
# watchdog 会对目录内每个新增文件/子目录单独触发事件，各自走独立处理流程
def handle_dir_creation(providers, monitor, file):
    """
    纯 watchdog 模式下的目录创建处理。
    只负责在云端 fs_mkdir 建立当前目录，拿到 CID 后缓存，然后结束。
    子项（子文件夹、文件）由 watchdog 逐一触发各自的 on_created 事件处理，
    无需也不应在此主动遍历 os.listdir，避免拷贝过程中扫到空目录导致子文件漏传。
    """
    logging.info(f"处理文件夹创建事件，文件夹路径: {file}")
    folder_name = os.path.basename(file)
    cloud_full_path = convert_to_cloud_path(
        local_root=os.getenv('local_root'),
        cloud_prefix=os.getenv('cloud_prefix'),
        local_full_path=file
    )
    # 父目录路径列表（去掉当前目录名，只保留上层）
    parts = [p for p in cloud_full_path.split('/') if p][:-1]

    for provider in providers.values():
        try:
            parent_id, remaining = provider.find_parent(parts)
            if remaining:
                logging.error(f"{provider.name}网盘找不到父目录，跳过建文件夹: {file}，缺失段: {remaining}")
            else:
                provider.mkdir(folder_name, parent_id, cloud_path=cloud_full_path)
        except Exception as e:
            logging.error(f"{provider.name}网盘创建目录异常: {file} - {e}")
    
def priority_queue_worker(providers, monitor):
    """
    单线程消费优先级队列：先目录后文件、深度由浅到深。

    目录任务：直接调 handle_dir_creation 在云端建目录。
    文件任务：调 handle_file_creation，父目录未就绪时函数内部会退避重新入队。
    未到重试时间的任务：放回队列尾部，短暂让出CPU，避免忙等空转。
    """
    logging.info("优先级队列消费线程 (Priority Queue Worker) 已启动...")
    while True:
        try:
            item = monitor.priority_queue.get(timeout=1.0)

            # item 格式有两种：
            # 防抖层入队：(is_file, depth, seq, filepath)          ← 新事件，无task对象
            # 重试入队：  (is_file, depth, seq, filepath, task)    ← 带已有task对象
            if len(item) == 4:
                is_file, depth, seq, filepath = item
                task = None
            else:
                is_file, depth, seq, filepath, task = item

            # 检查是否到了重试时间（退避未到期则放回队列尾部）
            if task is not None and time.time() < task.available_after:
                monitor.priority_queue.put(item)
                monitor.priority_queue.task_done()
                time.sleep(0.1)  # 短暂让出CPU，避免空转刷屏
                continue

            monitor.priority_queue.task_done()

            if not os.path.exists(filepath) and not is_file == 0:
                # 文件已消失（目录不做此检查，目录消失也要尝试建云端）
                logging.warning(f"[优先级队列] 路径已不存在，跳过: {filepath}")
                continue

            try:
                if is_file == 0:
                    # 目录任务：在云端建目录
                    handle_dir_creation(
                        providers=providers,
                        monitor=monitor,
                        file=filepath
                    )
                else:
                    # 文件任务：
                    # task=None 说明是首次入队，两个网盘都要处理
                    # task不为None 说明是某个网盘的退避重试，只发给 task.pan_name 对应的网盘
                    # 严禁将带pan_name的task传给另一个网盘，否则会发生CID串台导致上传崩溃
                    if task is None:
                        for provider in providers.values():
                            handle_file_creation(provider, monitor, filepath, task=None)
                    elif task.pan_name in providers:
                        handle_file_creation(providers[task.pan_name], monitor, filepath, task=task)
                    else:
                        logging.error(f"[优先级队列] 未知pan_name={task.pan_name}，跳过: {filepath}")
            except Exception as e:
                logging.error(f"[优先级队列] 处理任务时出错: {filepath} - {e}")

        except Empty:
            continue
        except Exception as e:
            logging.error(f"优先级队列消费线程捕获全局异常: {e}")
            time.sleep(1)


#启动目录监控
def start_monitoring(path, providers):
    """启动事件驱动版目录监控"""
    monitor = SimpleFileMonitor(path, providers)
    event_handler = SimpleFileHandler(monitor)
    observer = Observer()
    observer.schedule(event_handler, path, recursive=True)
    observer.start()

    # 启动上传工作线程 (Upload Worker)
    from upload_worker import upload_task_worker
    upload_thread = threading.Thread(target=upload_task_worker, args=(providers, monitor.upload_queue,))
    upload_thread.daemon = True
    upload_thread.start()

    # 启动防抖守护线程 (Debounce Worker)
    # 注意：debounce_worker_thread 不再需要 clients，只操作 monitor 内部队列
    debounce_thread = threading.Thread(target=debounce_worker_thread, args=(monitor,))
    debounce_thread.daemon = True
    debounce_thread.start()

    # 启动优先级队列消费线程 (Priority Queue Worker)
    pq_thread = threading.Thread(target=priority_queue_worker, args=(providers, monitor))
    pq_thread.daemon = True
    pq_thread.start()

    try:
        logging.info(f"开始事件驱动监控目录: {path} (平台: {platform.system()})")
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    import sys
    from providers import Pan123Provider, Pan115Provider

    load_dotenv()
    target_path = os.getenv('MONITOR_DIR')
    print(target_path)
    if not target_path:
        logging.error("错误: 未在 .env 文件中配置 MONITOR_DIR")
        sys.exit(1)
    if not os.path.isdir(target_path):
        logging.error(f"错误: 目录 '{target_path}' 不存在")
        sys.exit(1)

    manager = CloudClientManager()
    client_123 = manager.get_client('123')
    client_115 = manager.get_client('115')

    providers = {
        "123": Pan123Provider(client_123),
        "115": Pan115Provider(client_115),
    }
    start_monitoring(path=target_path, providers=providers)
