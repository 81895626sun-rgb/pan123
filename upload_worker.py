from queue import  Empty
from pan123 import smart_upload, upload_file
import logging
import json
import time
from datetime import datetime
from utils.telegram_notifier import TelegramNotifier  # 新增导入
from file_checker import FileGrowthChecker, FileState  # 添加 FileState

MAX_RETRIES = 3  # 最大重试次数
FAILED_TASKS_FILE = "failed_tasks.txt"  # 失败任务记录文件
_telegram_notifier = None


def _get_telegram_notifier():
    """懒加载 Telegram 通知器，避免 import 时副作用（缺凭证会 raise）。"""
    global _telegram_notifier
    if _telegram_notifier is None:
        _telegram_notifier = TelegramNotifier()
    return _telegram_notifier

def log_failed_task(task, error_msg):
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
    notification = (
        f"⚠️ 上传任务失败\n"
        f"盘为: {task.pan_name}\n"
        f"时间: {timestamp}\n"
        f"文件: {task.local_path}\n"
        f"目录ID: {task.dir_id}\n"
        f"重试次数: {task.retries}/{MAX_RETRIES}\n"
        f"错误: {error_msg}"
    )
    _get_telegram_notifier().send_message(notification)
    
def log_successful_task(task, file_id):
    """将成功任务记录到文件并发送通知"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 发送Telegram通知
    notification = (
        f"时间: {timestamp}\n"
        f"文件: {task.local_path}\n"
        f"✅ 上传任务成功\n"
        f"盘为: {task.pan_name}\n"
    )
    _get_telegram_notifier().send_message(notification)

def handle_upload_failure(upload_queue, task, error_msg=None, on_final_failure=None):
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
        log_failed_task(task, error_msg)
        upload_queue.task_done()


def upload_task_worker(client_123, client_115, upload_queue, on_final_failure=None):
    """文件上传工作线程（完整修正版）
    on_final_failure: 可选回调 path->None，透传给 handle_upload_failure，在彻底放弃时
    让 monitor 清 dispatched_tasks（见 troubleshooting.md #7）。"""
    last_active_time = time.time()
    while True:
        try:
            empty_count = 0  # 重置计数器（每次成功获取任务后重置）
            # 1. 从队列获取任务（1秒超时避免永久阻塞）
            task = upload_queue.get(timeout=1)
            last_active_time = time.time()  # 重置活跃时间
            # 2. 检查文件状态（返回状态枚举和当前大小）
            state, current_size = FileGrowthChecker.check_growth(task.local_path)
            task.file_state = state
            task.file_size = current_size
            logging.info(f"开始处理任务: {task.local_path} 任务状态为: {task.file_state} (网盘: {task.pan_name})")  # 新增日志
            # 3. 状态机处理核心逻辑
            if state == FileState.READY:
                if task.pan_name == "123":
                    # 3.1 文件就绪状态 - 执行上传
                    try:
                        upload_result = smart_upload(
                            client=client_123,
                            file_source=task.local_path,
                            parent_id=task.dir_id
                        )

                        # 3.1.1 验证上传结果
                        if upload_result and (upload_result.get('data', {}).get('Info') or 
                                            upload_result.get('data', {}).get('file_info')):
                            file_info = upload_result['data'].get('file_info') or upload_result['data'].get('Info')
                            file_id = file_info.get('FileId') if file_info else '未知'
                            logging.info(f"上传成功: {task.local_path} -> 文件ID: {file_id}")
                            log_successful_task(task,file_id)
                            upload_queue.task_done()  # 仅在此处标记任务完成
                            # logging.info(f"此时队列中的任务数量是 ：{upload_queue.unfinished_tasks}")
                        else:
                            handle_upload_failure(upload_queue, task, f"无效的API响应: {str(upload_result)[:100]}", on_final_failure=on_final_failure)

                    except Exception as e:
                        error_msg = str(e)[:150].replace('\n', ' ') + "..." if len(str(e)) > 150 else str(e).replace('\n', ' ')
                        logging.warning(f"【上传遇阻】网盘:[123] | 文件:[{task.local_path}] | 原因:[{error_msg}]")
                        _cause = e.__cause__ or e.__context__
                        if _cause:
                            logging.error(f"【上传异常根因】网盘:[123] | 文件:[{task.local_path}] | {type(_cause).__name__}: {str(_cause)[:300]}")
                        handle_upload_failure(upload_queue, task, error_msg, on_final_failure=on_final_failure)
                elif task.pan_name == "115":
                    try:
                        upload_file(
                            client=client_115,
                            file_path=task.local_path,
                            pid=task.dir_id
                        )
                        log_successful_task(task,task.dir_id)
                    except Exception as e:
                        error_msg = str(e)[:150].replace('\n', ' ') + "..." if len(str(e)) > 150 else str(e).replace('\n', ' ')
                        logging.warning(f"【上传遇阻】网盘:[115] | 文件:[{task.local_path}] | 原因:[{error_msg}]")
                        _cause = e.__cause__ or e.__context__
                        if _cause:
                            logging.error(f"【上传异常根因】网盘:[115] | 文件:[{task.local_path}] | {type(_cause).__name__}: {str(_cause)[:300]}")
                        handle_upload_failure(upload_queue, task, error_msg, on_final_failure=on_final_failure)
            elif state == FileState.GROWING:
                # 3.2 文件被锁定或正在写入
                if task.retries < MAX_RETRIES:
                    task.retries += 1
                    upload_queue.put(task)  # 重新入队等待重试
                    wait_time = min(120, task.retries * 30)  # 指数退避上限120秒
                    logging.info(f"文件{state.name}，等待{wait_time}s后重试: {task.local_path}")
                    time.sleep(wait_time)
                else:
                    handle_upload_failure(upload_queue, task, f"超过最大重试次数（状态:{state.name}）", on_final_failure=on_final_failure)

            elif state == FileState.NOT_FOUND:
                # 3.3 文件不存在
                handle_upload_failure(upload_queue, task, "文件不存在", on_final_failure=on_final_failure)

            else: #FileState.ERROR
                # 3.4 其他错误状态
                handle_upload_failure(upload_queue, task, "文件状态检测失败", on_final_failure=on_final_failure)
        
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



# def upload_task_worker(upload_queue):
#     while True:
#         try:
#             task = upload_queue.get(timeout=1)  # 获取任务，超时时间为1秒

#             # 智能状态检查（返回状态和当前大小）
#             state, current_size = FileReadyChecker.check_file_state(task.local_path)
#             task.file_state = state
#             task.file_size = current_size
            
#             # 状态处理逻辑
#             if state == FileState.READY:
#                 try:
#                     upload_result = smart_upload(client, task.local_path, task.dir_id)
#                     upload_queue.task_done()
#                 except Exception as e:
#                     handle_upload_failure(upload_queue, task, str(e))
                    
#             elif state in (FileState.LOCKED, FileState.GROWING):
#                 if task.retries < MAX_RETRIES:
#                     task.retries += 1
#                     upload_queue.put(task)
#                     wait_time = min(120, task.retries * 30)  # 动态等待（30s,60s,90s,120s...）
#                     logging.info(f"文件{state.name}，等待{wait_time}s后重试: {task.local_path}")
#                     time.sleep(wait_time)
#                 else:
#                     handle_upload_failure(upload_queue, task, f"文件持续{state.name}")
                    
#             elif state == FileState.NOT_FOUND:
#                 handle_upload_failure(upload_queue, task, "文件不存在")
                
#             else:  # ERROR状态
#                 handle_upload_failure(upload_queue, task, "文件状态检查错误")   

#             try:
#                 # 调用smart_upload（内部已实现分层重试）
#                 upload_result = smart_upload(
#                     client,
#                     file_source=task.local_path,
#                     parent_id=task.dir_id
#                 )
                
#                 # 只有两种情况会执行到这里：
#                 # 1. 秒传/直接上传/分片上传成功（返回有效结果）
#                 # 2. 分片上传失败（已抛出异常，不会执行到这里）
                
#                 if upload_result and (upload_result.get('data', {}).get('Info') or upload_result.get('data', {}).get('file_info')):
#                     # 兼容两种数据结构                    
#                     file_info = upload_result['data'].get('file_info') or upload_result['data'].get('Info')                    
#                     file_id = file_info.get('FileId') if file_info else '未知'                    
#                     logging.info(f"上传成功: {task.local_path} -> 文件ID: {file_id}")
#                     upload_queue.task_done()
#                 else:
#                     # 理论上不会执行到这里（因为smart_upload失败会抛异常）
#                     logging.warning(f"意外情况: 上传返回无效结果: {upload_result}")
#                     handle_upload_failure(upload_queue, task, "上传返回无效结果")
                    
#             except Exception as e:
#                 # 只有分片上传失败才会进入这里
#                 logging.error(f"最终上传失败: {task.local_path} -> 错误: {str(e)}")
#                 handle_upload_failure(upload_queue, task, str(e))
                
#         except Empty:  # 队列空时短暂等待
#             continue
            
#         except Exception as e:
#             logging.error(f"upload_worker系统错误: {str(e)}", exc_info=True)
#             # 可根据需要决定是否退出循环
#             # break  # 严重错误时退出循环


