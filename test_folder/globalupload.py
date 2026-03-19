#使用任务队列，进行上传

from flask import Flask, render_template, request, redirect, url_for, jsonify
from queue import Queue
import json
from pathlib import Path
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import send_telegram_message
import requests
import logging
from logging.handlers import RotatingFileHandler
import time
from p115client import P115Client
from collections import deque
from time import perf_counter
from watchdog.utils.dirsnapshot import DirectorySnapshot, DirectorySnapshotDiff
from watchdog.events import *
import threading
from watchdog.observers import Observer
from p115client import check_response
import atexit  


app = Flask(__name__, template_folder="/app/templates")
LOG_FILE_PATH = "/data/application.log"
cookie_path = Path("/data/115-cookies.txt")
ERROR_FILE_PATH = "/data/error.log"
CONFIG_FILE_PATH = "/data/monitor_config.json"
CACHE_FILE_PATH = "/data/path_to_cid_cache.json"# 缓存文件的路径，缓存路径和cid的对应关系
FAILED_TASKS_FILE = "/data/failed_tasks.json"  # 失败任务保存的文件路径



# 设置主要日志
log_handler = RotatingFileHandler(LOG_FILE_PATH, maxBytes=2 * 1024 * 1024, backupCount=1,encoding='utf-8')
log_handler.setLevel(logging.INFO)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
#设置错误日志
error_log_handler = RotatingFileHandler(ERROR_FILE_PATH, maxBytes=2 * 1024 * 1024, backupCount=1, encoding='utf-8')
error_log_handler.setLevel(logging.ERROR)
error_log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)
logger.addHandler(error_log_handler)  # 添加错误日志处理器

# 全局变量初始化
observer = Observer()
# 全局缓存字典
path_to_cid_cache = {}
executor = None
upload_status = {}
task_queue = Queue()
config = {
    "task_names": [],
    "local_directories": [],
    "remote_cids": [],
    "upload_modes": [],
    "delay_times": [],
    "max_workers": 1,
    "ignore_extensions": [],
    "telegram_token": "",
    "telegram_chat_id": "",
    "proxy_url": ""  # 添加代理 URL 配置项
}

task_queue = Queue()  # 用于存储上传任务
# 全局锁，用于保护失败任务文件的访问
failed_tasks_lock = threading.Lock()

# 主页面和任务逻辑
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        new_task_name = request.form.get("task_names[]")
        new_local_directory = request.form.get("local_directories[]")
        new_remote_cid = request.form.get("remote_cids[]")
        new_upload_mode = request.form.get("upload_modes[]")
        new_delay_time = int(request.form.get("delay_times[]"))

        if new_task_name and new_local_directory and new_remote_cid:
            config["task_names"].append(new_task_name)
            config["local_directories"].append(new_local_directory)
            config["remote_cids"].append(new_remote_cid)
            config["upload_modes"].append(new_upload_mode)
            config["delay_times"].append(new_delay_time)

        config["max_workers"] = int(request.form.get("max_workers", 4))
        save_config()
        restart_monitoring()

        return redirect(url_for("index"))

    local_options = get_docker_mapped_directories()
    return render_template("index.html", config=config, local_options=local_options)


def init_failed_record_file():
    """
    初始化失败记录文件。
    如果文件不存在，创建一个空列表并保存到文件中。
    """
    if not os.path.exists(FAILED_TASKS_FILE):
        try:
            with open(FAILED_TASKS_FILE, "w") as f:
                json.dump([], f)  # 初始化一个空列表
            logger.info("失败记录文件已初始化。")
        except Exception as e:
            logger.error(f"初始化失败记录文件时发生错误: {e}")
def read_failed_tasks():
    """
    读取失败任务文件。
    :return: 失败任务列表
    """
    with failed_tasks_lock:  # 获取锁，确保线程安全
        try:
            # 检查文件是否存在
            if not os.path.exists(FAILED_TASKS_FILE):
                logger.info("失败任务文件不存在，返回空列表。")
                return []

            # 检查文件是否为空
            if os.path.getsize(FAILED_TASKS_FILE) == 0:
                logger.info("失败任务文件为空，返回空列表。")
                return []

            # 读取文件内容
            with open(FAILED_TASKS_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()  # 读取内容并去除空白字符
                if not content:  # 如果内容为空
                    logger.info("失败任务文件内容为空，返回空列表。")
                    return []

                # 尝试解析 JSON 内容
                try:
                    tasks = json.loads(content)
                    if isinstance(tasks, list):  # 确保解析后的内容是列表
                        logger.info("成功加载失败任务文件。")
                        return tasks
                    else:
                        logger.error("失败任务文件内容不是有效的列表格式，返回空列表。")
                        return []
                except json.JSONDecodeError as e:
                    logger.error(f"失败任务文件格式错误: {e}")
                    return []
        except Exception as e:
            logger.error(f"读取失败任务文件时发生未知错误: {e}")
            return []

def write_failed_tasks(failed_tasks):
    """
    写入失败任务文件。
    :param failed_tasks: 失败任务列表
    """
    with failed_tasks_lock:  # 获取锁
        try:
            with open(FAILED_TASKS_FILE, "w") as f:
                json.dump(failed_tasks, f)
        except Exception as e:
            logger.error(f"写入失败任务文件时发生错误: {e}")

def record_failed_upload(file_path, pid):
    """
    记录上传失败的任务。
    如果失败次数超过 3 次，任务将不会被写入失败文件中。
    :param file_path: 文件路径
    :param pid: 父目录 CID
    :return: True 表示任务已记录，False 表示任务已达到最大重试次数
    """
    try:
        # 读取失败任务
        failed_tasks = read_failed_tasks()

        # 检查是否已经记录过该任务
        task_found = False
        for task in failed_tasks:
            if task["file_path"] == file_path and task["pid"] == pid:
                task["retry_count"] += 1  # 增加重试次数
                task_found = True
                break

        # 如果没有记录过，添加新任务
        if not task_found:
            failed_tasks.append({
                "file_path": file_path,
                "pid": pid,
                "retry_count": 1,  # 初始化重试次数为 1
                "last_failed_time": time.time()  # 记录失败时间
            })

        # 检查重试次数
        for task in failed_tasks:
            if task["file_path"] == file_path and task["pid"] == pid:
                if task["retry_count"] > 3:
                    logger.warning(f"文件 {file_path} 已达到最大重试次数（3 次），删除记录")
                    # 删除该任务记录
                    failed_tasks = [t for t in failed_tasks if not (t["file_path"] == file_path and t["pid"] == pid)]
                    write_failed_tasks(failed_tasks)  # 更新失败任务文件
                    return False  # 超过最大重试次数，不写入文件
                else:
                    logger.info(f"文件 {file_path} 已记录失败，重试次数: {task['retry_count']}")
                    break

        # 写入失败任务
        write_failed_tasks(failed_tasks)
        return True  # 任务已记录

    except Exception as e:
        logger.error(f"记录失败任务时发生错误: {e}")
        return False  # 记录失败
    
def retry_failed_uploads():
    """
    定期检查失败任务并重新加入上传队列。
    清理已达到最大重试次数的任务。
    """
    while True:
        time.sleep(300)  # 每 5 分钟检查一次
        try:
            # 读取失败任务
            failed_tasks = read_failed_tasks()

            # 清理已达到最大重试次数的任务
            new_failed_tasks = [task for task in failed_tasks if task["retry_count"] < 3]

            # 遍历失败任务
            for task in new_failed_tasks:
                file_path = task["file_path"]
                pid = task["pid"]
                retry_count = task["retry_count"]

                # 重新加入上传队列
                task_queue.put((file_path, pid))
                logger.info(f"重试上传文件: {file_path} (重试次数: {retry_count})")

                

            # 写入清理后的失败任务
            write_failed_tasks(new_failed_tasks)

        except Exception as e:
            logger.error(f"重试失败任务时发生错误: {e}")



# 保存监控任务配置
def save_config():
    with open(CONFIG_FILE_PATH, "w") as f:
        json.dump(config, f)



# 加载监控任务配置
def load_config():
    global config
    try:
        with open(CONFIG_FILE_PATH, "r") as f:
            config.update(json.load(f))
    except FileNotFoundError:
        pass
    # 初始化失败记录文件
    init_failed_record_file()

def initialize_client():
    global client
    cookie = cookie_path.read_text().strip()
    client = P115Client(cookies=cookie)
    
    if client.login_status():
        logger.info(f"登录成功")
    else:
        logger.error(f"登录失败")
        send_telegram_message(f"客户端登录失败，请检查")

#发送webhook刷新库通知
def notify_file_creation():
    # 设置 URL 和参数
    base_url = "http://221.236.27.68:38003/update_db"
    device_name = "115"
    user_name = "sun401"
    event_name = "notify"

    # 构建请求的完整 URL
    url = f"{base_url}/file_notify?device_name={device_name}&user_name={user_name}&type={event_name}"

    # 构建请求的主体
    body = {
        "device_name": device_name,
        "user_name": user_name,
        "version": "1.0",
        "event_category": "file",
        "event_name": "notify",
        "event_time": int(time.time()),  # 当前时间的 Unix 时间戳
        "send_time": int(time.time()),    # 数据发送的时间
        "data": [
            {
                "action": "创建",  # 文件操作类型
                "is_dir": "true",     # 是否为目录
                "source_file": "/115/nas/MPLink/movie",  # 源文件路径
                "destination_file": ""  # 目标文件路径
            }
        ]
    }

    try:
        # 发送 POST 请求
        response = requests.post(url, headers={"Content-Type": "application/json"}, data=json.dumps(body))

        # 检查响应状态
        response.raise_for_status()  # 如果状态码不是 200，将引发 HTTPError
        
        send_telegram_message(f"Response: {response.json()}")

    except requests.exceptions.RequestException as e:
        send_telegram_message(f"webhook失败: {e}")



# 更新任务名称
@app.route("/update_task_name/<int:task_id>", methods=["POST"])
def update_task_name(task_id):
    new_name = request.json.get("new_name", "").strip()
    if 0 <= task_id < len(config["task_names"]):
        config["task_names"][task_id] = new_name
        save_config()
        return jsonify({"success": True, "new_name": new_name})
    else:
        return jsonify({"success": False, "error": "未找到任务ID"}), 404

# 初始化多个挂载目录
MOUNT_POINTS = ["/media1", "/nas"]

# 获取 Docker 中的所有挂载目录及子目录
def get_docker_mapped_directories():
    directories = []
    for mount_point in MOUNT_POINTS:
        if os.path.exists(mount_point):
            for root, dirs, files in os.walk(mount_point):
                directories.extend([os.path.join(root, d) for d in dirs])
    return directories







# 目录监控类
class FileMonitorHandler(FileSystemEventHandler):

    def __init__(self, aim_path):
        FileSystemEventHandler.__init__(self)
        self.aim_path = aim_path
        self.timer = None
        self.snapshot = DirectorySnapshot(self.aim_path)
        self.queue = deque()  # 用于存储创建的文件
        self.lock = threading.Lock()  # 线程锁
    
    def on_any_event(self, event):
        if self.timer:
            self.timer.cancel()
        
        self.timer = threading.Timer(30, self.checkSnapshot)
        self.timer.start()
    
    def checkSnapshot(self):
        snapshot = DirectorySnapshot(self.aim_path)
        diff = DirectorySnapshotDiff(self.snapshot, snapshot)
        self.snapshot = snapshot
        self.timer = None

        if diff.dirs_created:
            notify_file_creation()
            min_depth = min(dir.count("/") for dir in diff.dirs_created)
            parent_dirs = [dir for dir in diff.dirs_created if dir.count("/") == min_depth]
            logger.info(f"保留相同深度的目录: {parent_dirs}")
            self.queue.extend(parent_dirs)
            if self.queue:
                self.process_queue()
                if diff.files_created:
                    for file in diff.files_created:
                        if not any(file.startswith(parent_dir) for parent_dir in parent_dirs): #不属于任何指定的父目录
                            self.handle_file_creation(file)
                        else:
                            logger.info(f"文件 {file} 已在相同深度的目录中，跳过处理。")
        else:
            logger.info("没有待处理的目录")
            if diff.files_created:
                notify_file_creation()
                for file in diff.files_created:
                    self.handle_file_creation(file)
            else:
                logger.info("也没有待处理的目录，继续监控")
    #找到CID，上传文件
    def handle_file_creation(self,file):
        logger.info(f"处理文件创建事件，文件路径: {file}")
        result_cid, _, _ = find_cid_by_path(file)
        if result_cid is not None:
            # 将任务加入任务队列
            task_queue.put((file, result_cid))
            logger.info(f"文件 {file} 已加入上传队列，目标 CID: {result_cid}")
        else:
            logger.error(f"无法找到文件 {file} 对应的 CID，跳过上传")

        

    #处理文件队列
    def process_queue(self):
        while self.queue:
            with self.lock:
                dir_to_process = self.queue.popleft()
            self.handle_dir_creation(dir_to_process)
    #处理文件基础函数
    def handle_dir_creation(self,dir):
        result_cid,_,not_found_part = find_cid_by_path(dir)
        mkdir_return = client.fs_mkdir(not_found_part,result_cid)
        logger.info(f"成功创建文件夹{mkdir_return['file_name']}")
        self._upload_recursive(dir,mkdir_return['cid'])
    #递归上传文件和文件夹
    def _upload_recursive(self, current_folder, parent_folder_id):
        # self.parent_file_id = parent_folder_id
        # 遍历当前文件夹
        for item in os.listdir(current_folder):
            item_path = os.path.join(current_folder, item)  # 获取完整路径
            print(item_path)
            if os.path.isdir(item_path):
                # 如果是文件夹，先创建服务器上的文件夹
                print(f"创建文件夹: {item}")
                mkdir_return = client.fs_mkdir(item,parent_folder_id)  # 创建文件夹并获取其 ID
                logger.info(f"成功创建文件夹{mkdir_return['file_name']}")
                # 递归调用，处理子文件夹
                self._upload_recursive(item_path, mkdir_return['cid'])
            else:
                # 如果是文件，调用上传文件的方法
                print(f"正在上传文件: {item_path},cid={parent_folder_id}")
                # 如果是文件，将上传任务加入任务队列
                task_queue.put((item_path, parent_folder_id))
                logger.info(f"文件 {item_path} 已加入上传队列，目标 CID: {parent_folder_id}")
               
def find_cid_in_cache(path):
    """
    递归查找缓存中的 CID。
    如果当前路径没有缓存，则逐级向上查找父目录的缓存。

    :param path: 输入的路径字符串
    :return: (cid, remaining_path)，其中：
             - cid 是缓存中的 CID。
             - remaining_path 是完整路径减去找到的路径部分。
             如果未找到缓存，则返回 (None, None)。
    """
    logger.info(f"开始查找缓存中的 CID，路径: {path}")
    # 按 '/' 分割路径并过滤空值，并去掉最后一个路径
    parts = [part for idx, part in enumerate(path.split('/')) if part and idx != len(path.split('/')) - 1]
    logger.info(f"分割后的路径部分: {parts}")

    # 从第一级别路径开始，逐级向上查找缓存
    for i in range(len(parts), 0, -1):
        current_path = '/' + '/'.join(parts[:i])  # 构建当前路径
        logger.info(f"正在查找缓存路径: {current_path}")
        cached_cid = path_to_cid_cache.get(current_path, None)
        if cached_cid is not None:
            logger.info(f"从缓存中找到 CID: {cached_cid} 对应路径: {current_path}")
            # 计算剩余路径
            remaining_path = '/' + '/'.join(parts[i:]) if i < len(parts) else None
            logger.info(f"剩余路径: {remaining_path}")
            return cached_cid, remaining_path,current_path

    # 如果所有父目录都没有缓存，返回 (None, None)
    logger.info(f"路径 {path} 及其父目录均未找到缓存")
    return None, None,None



def find_cid_by_path(full_path):
    logger.info(f"正在查找ID的完整路径是{full_path}")
    """
    根据路径的各个部分查找并返回对应的 cid 和 n。

    :param client: 传入的客户端对象
    :param path: 输入的路径字符串
    :return: 最后找到的 cid 和 n
    """
    # 按 '/' 分割路径并过滤空值
    filtered_parts = [part for idx, part in enumerate(full_path.split('/')) if part and idx != len(full_path.split('/')) - 1]
    logger.info(f"分割后的路径部分: {filtered_parts}")

    if not filtered_parts:
        logger.info("路径没有有效部分，返回 None")
        return None, None, None  # 如果没有有效部分，返回 None

    # 先检查缓存
    logger.info("开始检查缓存...")
    cached_cid, remaining_path, current_path = find_cid_in_cache(full_path)

    if cached_cid is not None:
        logger.info(f"从缓存中找到 CID: {cached_cid} 对应路径: {current_path}")
        if remaining_path is None:
            # 如果剩余路径为空，说明缓存中已经找到父目录
            logger.info(f"缓存中已找到完整路径，返回 CID 和 {os.path.basename(full_path)}")
            return cached_cid, None, os.path.basename(full_path)
        else:
            # 如果剩余路径不为空，继续处理剩余路径
            logger.info(f"剩余路径: {remaining_path}")
            path = remaining_path
            filtered_parts = [part for part in path.split('/') if part]
            logger.info(f"更新后的路径部分（去掉了最后一部分）: {filtered_parts}")
            offset = cached_cid  # 使用缓存中的 CID 作为 offset
    else:
        # 如果缓存中没有找到 CID，从根目录开始查找
        offset = 0
        current_path = ""  # 当前路径

    # 如果缓存中没有，进入逐级查找逻辑
    logger.info("缓存中没有找到 CID，进入逐级查找逻辑...")
    last_cid = None
    last_n = None
    part_index = 0  # 用于跟踪当前处理的部分索引

    # 逐级查找
    while part_index < len(filtered_parts):
        current_part = filtered_parts[part_index]  # 取当前部分
        current_path = f"{current_path}/{current_part}" if current_path else f"/{current_part}"
        logger.info(f"当前处理的部分: {current_part}, 当前路径: {current_path}")

        payload={
        "cid": offset,  # 目录ID
        "limit": 28,                  # 测试只能获取到28
        "offset": 0,
        "o": "user_ptime",             # 按创建时间排序
        "asc": 0                       # 升序保证稳定性
    }

        # 调用原有的查找逻辑
        response = client.fs_files(payload)
        logger.info(f"调用 client.fs_files(offset={offset}) 返回的响应: {response}")

        # 检查响应中的 'data' 字段是否存在且不为空
        if 'data' in response and response['data']:  # 检查是否存在并且不为空
            found = False  # 标识是否找到匹配项

            # 遍历文件数据
            for item in response['data']:
                if isinstance(item, dict):
                    # 更新缓存
                    parent_path = os.path.dirname(current_path)  # 去掉最后一部分
                    item_path = os.path.join(parent_path, item['n'])  # 拼接 item['n']
                    update_cache(item_path, item['cid'])
                    logger.info(f"缓存更新: 路径={item_path}, CID={item['cid']}")

                    # 检查是否匹配当前部分
                    if item['n'] == current_part:
                        last_cid = item['cid']
                        last_n = item['n']
                        found = True
                        offset = last_cid  # 使用找到的 cid 作为下一次请求的输入
                        logger.info(f"找到匹配项: CID={last_cid}, n={last_n}")
                        break  # 找到后退出当前循环

            if found:
                part_index += 1  # 仅在找到匹配项后才移动到下一个部分
                logger.info(f"移动到下一个部分，当前索引: {part_index}")
            else:
                logger.info(f"未找到路径 {current_path} 对应的 CID")
                break  # 如果没有找到匹配项，退出循环
        else:
            logger.info("响应中不包含有效的 'data' 字段")
            # 如果响应不包含有效数据，返回 offset、current_part 和 current_part
            return offset, current_part, current_part
            
    result = '/'.join(full_path.split('/')[full_path.split('/').index(last_n) + 1:]).lstrip('/')
    logger.info(f"最后找到的字段是: CID={last_cid}, n={last_n}, 当前路径={current_path},将要新建的目录={result}")
    return last_cid, last_n, result # 返回最后找到的 cid 和 n

def read_file_in_chunks(file_path, chunk_size=1024*1024*100):  # 默认chunk_size为100MB
    # ""“生成器函数，用于逐块读取文件内容”""
    with open(file_path, 'rb') as file:
        while True:
            chunk = file.read(chunk_size)
            if not chunk:
                break
            yield chunk

    
#基础的上传文件的方法
# 原来的程序可以通过docker挂载的方式，把网盘目录结构和本地的目录结构统一了，所以我也建议使用docker的方式
# 统一结构后，本地file和远程file一模一样
def upload_file(file_path,pid):
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    # start_time = time.time()
    logger.info(f"开始上传 {file_name} (大小: {file_size}) 到 CID: {file_path}")
    # send_telegram_message(f"开始上传 {file_name} 到 CID: {file_path}")

    
    try:     
        # 检查同步调用
        client.upload_file(file=file_path, filename=file_name, pid=pid, filesize=file_size)
        # 处理返回的数据
        logger.info(f"{file_name} 上传成功")
        send_telegram_message(f"{file_name} 已经上传成功")
    except Exception as e:  # 捕获所有异常
        # 处理异常
        logger.error(f"{file_name} 上传失败: {e}")
        send_telegram_message(f"{file_name} 文件上传失败: {e}")
        # 尝试使用 upload_file_sample 进行重试
        try:
            file_iterator = read_file_in_chunks(file_path=file_path)
            client.upload_file_sample(file=file_iterator, filename=file_name, pid=pid)
                
            logger.info(f"{file_name} 通过 upload_file_sample 上传成功")
            send_telegram_message(f"{file_name} 已经通过 upload_file_sample 上传成功")
        except Exception as e:
            logger.error(f"{file_name} 通过 upload_file_sample 上传失败: {e}")
            send_telegram_message(f"{file_name} 文件通过 upload_file_sample 上传失败: {e}")
            ## 重新抛出异常，让外部的 worker 函数捕获
            raise

def worker():
    while True:
        task = task_queue.get()
        if task is None:
            break
        file_path, pid = task
        try:
            upload_file(file_path, pid)
            # 安全移除失败记录（无论是否存在）
            remove_failed_task(file_path, pid)
        except Exception as e:
            logger.error(f"上传文件 {file_path} 时发生错误: {e}")
            send_telegram_message(f"上传文件 {file_path} 时发生错误: {e}")
            # 记录失败任务
            record_failed_upload(file_path, pid)
            logger.info(f"{file_path} 已加入错误文件")
        finally:
            task_queue.task_done()

def remove_failed_task(file_path, pid):
    """安全移除失败任务（如果存在）"""
    try:
        failed_tasks = read_failed_tasks()
        # 检查任务是否存在
        exists = any(
            task["file_path"] == file_path and task["pid"] == pid
            for task in failed_tasks
        )
        if not exists:
            logger.debug(f"无需操作: {file_path} 不在失败列表中")
            return  # 直接返回，避免后续操作

        # 存在时才执行删除和写入
        new_tasks = [t for t in failed_tasks if not (t["file_path"] == file_path and t["pid"] == pid)]
        write_failed_tasks(new_tasks)
        logger.info(f"已从失败任务中移除: {file_path}")

        # ----------------- 新增代码开始 -----------------
        with task_queue.mutex:
            task_queue.queue = deque(
                [task for task in task_queue.queue if not (task[0] == file_path and task[1] == pid)]
            )
            removed_count = len(task_queue.queue) - len(task_queue.queue)
            if removed_count > 0:
                logger.info(f"从队列中清理 {removed_count} 个残留任务: {file_path}")
        # ----------------- 新增代码结束-----------------
    except Exception as e:
        logger.error(f"删除失败任务时出错: {e}")


# 更新忽略后缀和 Telegram 设置
@app.route("/update_global_settings", methods=["POST"])
def update_global_settings():
    # 更新忽略后缀名单
    exclude_extensions = request.form.get("exclude_extensions", "")
    config["ignore_extensions"] = [ext.strip().lower() for ext in exclude_extensions.split(",") if ext.strip()]

    # 更新 Telegram 令牌和聊天 ID
    telegram_token = request.form.get("telegram_token", "").strip()
    telegram_chat_id = request.form.get("telegram_chat_id", "").strip()
    proxy_url = request.form.get("proxy_url", "").strip()

    config["telegram_token"] = telegram_token
    config["telegram_chat_id"] = telegram_chat_id
    config["proxy_url"] = proxy_url

    save_config()  # 保存配置

    # 自动重启监控任务以应用最新的忽略后缀名单
    restart_monitoring()

    return redirect(url_for("index"))

# 获取日志内容
@app.route("/get_log", methods=["GET"])
def get_log():
    try:
        with open(LOG_FILE_PATH, "r") as log_file:
            content = log_file.read()
        return jsonify({"success": True, "log": content})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/delete_task/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    if 0 <= task_id < len(config["task_names"]):
        config["task_names"].pop(task_id)
        config["local_directories"].pop(task_id)
        config["remote_cids"].pop(task_id)
        config["upload_modes"].pop(task_id)
        config["delay_times"].pop(task_id)
        save_config()
        restart_monitoring()  # 重启监控以应用更改
        return jsonify({"success": True})
    else:
        return jsonify({"success": False, "error": "未找到任务ID"}), 404


def start_workers():
    for _ in range(config["max_workers"]):
        threading.Thread(target=worker, daemon=True).start()

def restart_monitoring():
    global observer, retry_thread

    # 停止现有的监控器（如果正在运行）
    if observer.is_alive():
        observer.stop()
        observer.join()
    # 创建新的 Observer 实例
    observer = Observer()
    # 注册要监控的目录
    for directory in config["local_directories"]:
        logger.info(f"监控目录: {directory}")
        event_handler = FileMonitorHandler(directory)  # 实例化事件处理器并传入目录
        observer.schedule(event_handler, directory, recursive=True)
    # 启动监控器
    observer.start()
    start_workers()
    logger.info("文件监控已重启。")

def update_cache(path, cid):
    """更新缓存并保存到文件"""
    # 检查路径是否已经存在
    if path in path_to_cid_cache:
        logger.info(f"路径 {path} 已存在于缓存中，旧的 CID: {path_to_cid_cache[path]}，新的 CID: {cid}")

    # 更新缓存
    path_to_cid_cache[path] = cid
    save_cache()  # 每次更新缓存后保存到文件
    logger.info(f"路径 {path} 已更新缓存，CID: {cid}")


def save_cache():
    """将缓存字典保存到文件中"""
    try:
        with open(CACHE_FILE_PATH, "w", encoding='utf-8') as f:
            # 设置 ensure_ascii=False，确保路径以中文字符形式存储
            json.dump(path_to_cid_cache, f, ensure_ascii=False, indent=4)
        logger.info("缓存已保存到文件。")
    except Exception as e:
        logger.error(f"保存缓存到文件时发生错误: {e}")

def load_cache():
    """从文件中加载缓存字典"""
    global path_to_cid_cache
    try:
        # 检查文件是否存在且不为空
        if os.path.exists(CACHE_FILE_PATH) and os.path.getsize(CACHE_FILE_PATH) > 0:
            with open(CACHE_FILE_PATH, "r") as f:
                path_to_cid_cache = json.load(f)
            logger.info("缓存已从文件加载。")
        else:
            logger.info("缓存文件不存在或为空，初始化空缓存。")
            path_to_cid_cache = {}
    except Exception as e:
        logger.error(f"加载缓存文件时发生错误: {e}")
        path_to_cid_cache = {}


if __name__ == "__main__":
    # 在程序启动时加载缓存
    load_cache()

    # 在程序结束时保存缓存
    atexit.register(save_cache)
    initialize_client()
    load_config()
    restart_monitoring()
    threading.Thread(target=retry_failed_uploads, daemon=True).start()
    app.run(host="0.0.0.0", port=5200)



