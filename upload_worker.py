from queue import  Empty
import logging
import json
import time
from datetime import datetime
from file_checker import FileGrowthChecker, FileState  # 添加 FileState

MAX_RETRIES = 3  # 最大重试次数
FAILED_TASKS_FILE = "failed_tasks.txt"  # 失败任务记录文件


def log_failed_task(task, error_msg, notifier=None):
    """将失败任务记录到文件"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    task_data = {
        "timestamp": timestamp,
        "local_path": task.local_path,
        "dir_id": task.dir_id,
        "retries": task.retries,
        "error": error_msg
    }
    with open(FAILED_TASKS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(task_data, ensure_ascii=False) + "\n")

    # 发送Telegram通知
    if notifier is not None:
        notification = (
            f"⚠️ 上传任务失败\n"
            f"盘为: {task.pan_name}\n"
            f"时间: {timestamp}\n"
            f"文件: {task.local_path}\n"
            f"目录ID: {task.dir_id}\n"
            f"重试次数: {task.retries}/{MAX_RETRIES}\n"
            f"错误: {error_msg}"
        )
        notifier.send_message(notification)

def log_successful_task(task, file_id, notifier=None):
    """将成功任务记录到文件并发送通知"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 发送Telegram通知
    if notifier is not None:
        notification = (
            f"时间: {timestamp}\n"
            f"文件: {task.local_path}\n"
            f"✅ 上传任务成功\n"
            f"盘为: {task.pan_name}\n"
        )
        notifier.send_message(notification)

def handle_upload_failure(upload_queue, task, error_msg=None, on_final_failure=None, notifier=None):
    """处理上传失败的情况，包括重试或放弃。
    on_final_failure: 可选回调 path->None，仅在「彻底放弃」分支调用，让 monitor 清掉
    dispatched_tasks，使失败文件可被重拷重新检测重传（见 troubleshooting.md #7）。"""
    if task.retries < MAX_RETRIES:
        task.retries += 1
        upload_queue.put(task)
        logging.info(f"♻️ 【任务排队重试】({task.retries}/{MAX_RETRIES}) | 网盘:[{task.pan_name}] | 文件:[{task.local_path}]")
    else:
        error_brief = str(error_msg)[:150].replace('\n', ' ') if error_msg else "未知错误"
        logging.error(f"❌ 【彻底放弃上传】网盘:[{task.pan_name}] | 失败文件:[{task.local_path}] | 最终错误:[{error_brief}]")
        if on_final_failure is not None:
            on_final_failure(task.local_path)
        log_failed_task(task, error_msg, notifier=notifier)
        upload_queue.task_done()


def upload_task_worker(providers, upload_queue, on_final_failure=None, notifier=None):
    """文件上传工作线程（Provider 版）
    providers: dict[str, CloudProvider]，通过 pan_name 查找对应网盘执行上传。
    on_final_failure: 可选回调 path->None，透传给 handle_upload_failure，在彻底放弃时
    让 monitor 清 dispatched_tasks（见 troubleshooting.md #7）。
    notifier: TelegramNotifier 实例（可选），用于发送上传成功/失败通知。"""
    last_active_time = time.time()
    while True:
        try:
            empty_count = 0  # 重置计数器（每次成功获取任务后重置）
            # 1. 从队列获取任务（1秒超时避免永久阻塞）
            task = upload_queue.get(timeout=1)
            last_active_time = time.time()  # 重置活跃时间

            # 1.5 GROWING 退避未到期：放回队尾让出线程（单线程穿插处理其他就绪文件），
            # 不放 sleep 阻塞。注意：必须在 check_growth 之前检查，避免白跑文件检测。
            if task.available_after and time.time() < task.available_after:
                upload_queue.put(task)
                upload_queue.task_done()
                time.sleep(1)
                continue

            # 2. 检查文件状态（返回状态枚举和当前大小）
            state, current_size = FileGrowthChecker.check_growth(task.local_path)
            task.file_state = state
            task.file_size = current_size
            logging.info(f"开始处理任务: {task.local_path} 任务状态为: {task.file_state} (网盘: {task.pan_name})")  # 新增日志
            # 3. 状态机处理核心逻辑
            if state == FileState.READY:
                provider = providers.get(task.pan_name)
                if provider is None:
                    logging.error(f"[上传] 未知网盘 {task.pan_name}，跳过: {task.local_path}")
                    upload_queue.task_done()
                    continue

                try:
                    file_id = provider.upload(task.local_path, task.dir_id)
                    # 123 返回真实 FileId；115 返回 None，回退到 dir_id（与原逻辑一致）
                    log_successful_task(task, file_id or task.dir_id, notifier=notifier)
                    upload_queue.task_done()
                except Exception as e:
                    error_msg = str(e)[:150].replace('\n', ' ') + "..." if len(str(e)) > 150 else str(e).replace('\n', ' ')
                    logging.warning(f"【上传遇阻】网盘:[{task.pan_name}] | 文件:[{task.local_path}] | 原因:[{error_msg}]")
                    _cause = e.__cause__ or e.__context__
                    if _cause:
                        logging.error(f"【上传异常根因】网盘:[{task.pan_name}] | 文件:[{task.local_path}] | {type(_cause).__name__}: {str(_cause)[:300]}")
                    handle_upload_failure(upload_queue, task, error_msg, on_final_failure=on_final_failure, notifier=notifier)
            elif state == FileState.GROWING:
                # 3.2 文件被锁定或正在写入：设退避时间戳后放回队尾，
                # 不 sleep 阻塞单线程（否则队列里其他就绪文件会干等 30-120s）
                if task.retries < MAX_RETRIES:
                    task.retries += 1
                    wait_time = min(120, task.retries * 30)  # 指数退避上限120秒
                    task.available_after = time.time() + wait_time
                    upload_queue.put(task)
                    upload_queue.task_done()  # 保持 unfinished_tasks 平衡（原代码漏了这行，导致计数泄漏）
                    logging.info(f"文件{state.name}，{wait_time}s后重试: {task.local_path}")
                else:
                    handle_upload_failure(upload_queue, task, f"超过最大重试次数（状态:{state.name}）", on_final_failure=on_final_failure, notifier=notifier)

            elif state == FileState.NOT_FOUND:
                # 3.3 文件不存在
                handle_upload_failure(upload_queue, task, "文件不存在", on_final_failure=on_final_failure, notifier=notifier)

            else: #FileState.ERROR
                # 3.4 其他错误状态
                handle_upload_failure(upload_queue, task, "文件状态检测失败", on_final_failure=on_final_failure, notifier=notifier)

        except Empty:
            # 4. 队列空时短暂等待（避免CPU空转）
            # 新增队列空完成检测
            if upload_queue.unfinished_tasks == 0:
                idle_time = time.time() - last_active_time
                if idle_time > 2:  # 持续空闲2秒才确认完成
                    logging.info("✅ 所有上传任务已完成（包括初始空队列）")
                    time.sleep(30)  # 避免频繁提示
            continue

        except Exception as e:
            # 5. 系统级异常处理
            logging.error(f"工作线程异常: {str(e)}", exc_info=True)
            # 注意：此处不退出循环以保持线程存活