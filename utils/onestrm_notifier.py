#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import time
import threading
import requests
from typing import Dict, Any
from .telegram_notifier import send_telegram_message  # 使用相对导入

QMEDIASYNC_BASE_URL = "http://REDACTED:20683"
QMEDIASYNC_API_KEY = "REDACTED"
# sync_path id: 1=movie, 2=teleplay
QMEDIASYNC_PATH_IDS = [1, 2]
# 防抖延迟：最后一个文件就绪后等待多少秒再触发同步
QMEDIASYNC_DEBOUNCE_SECONDS = 120


class OneStrmNotifier:
    """异步文件事件通知器（线程版）"""

    _debounce_timer: threading.Timer = None
    _debounce_lock = threading.Lock()

    @staticmethod
    def _trigger_qmediasync_sync(path_id: int, full: bool = False) -> None:
        """触发 QMediaSync 同步（增量或全量）"""
        endpoint = "full-start" if full else "start"
        url = f"{QMEDIASYNC_BASE_URL}/api/sync/path/{endpoint}?api_key={QMEDIASYNC_API_KEY}"
        try:
            response = requests.post(
                url=url,
                headers={"Content-Type": "application/json"},
                data=json.dumps({"id": path_id}),
                timeout=10
            )
            response.raise_for_status()
            result = response.json()
            sync_type = "全量" if full else "增量"
            send_telegram_message(f"🔄 QMediaSync {sync_type}同步触发成功 | 目录ID={path_id} | {result.get('message', '')}")
        except Exception as e:
            send_telegram_message(f"⚠️ QMediaSync 同步触发失败 | 目录ID={path_id} | 错误: {str(e)}")

    @classmethod
    def _do_trigger(cls, path_ids: list, full: bool) -> None:
        """防抖计时器到期后实际执行同步"""
        for pid in path_ids:
            cls._trigger_qmediasync_sync(pid, full)

    @classmethod
    def trigger_qmediasync(cls, path_ids: list = None, full: bool = False) -> bool:
        """防抖触发 QMediaSync 同步任务。
        多次调用会重置计时器，最后一次调用后等 QMEDIASYNC_DEBOUNCE_SECONDS 秒才真正触发，
        避免批量上传时重复触发多次同步。
        Args:
            path_ids: 同步目录ID列表，默认使用 QMEDIASYNC_PATH_IDS
            full: True=全量同步，False=增量同步（默认）
        Returns:
            bool: 是否成功重置/启动防抖计时器
        """
        ids = path_ids if path_ids is not None else QMEDIASYNC_PATH_IDS
        try:
            with cls._debounce_lock:
                if cls._debounce_timer is not None:
                    cls._debounce_timer.cancel()
                cls._debounce_timer = threading.Timer(
                    QMEDIASYNC_DEBOUNCE_SECONDS,
                    cls._do_trigger,
                    args=(ids, full)
                )
                cls._debounce_timer.daemon = True
                cls._debounce_timer.start()
            return True
        except RuntimeError as e:
            send_telegram_message(f"❌ QMediaSync 防抖计时器启动失败: {str(e)}")
            return False

    @staticmethod
    def _notify_sync() -> None:
        """同步执行的通知核心逻辑"""
        config = {
            "base_url": "http://REDACTED:38003/update_db",
            "device_name": "115",
            "user_name": "sun401",
            "event_type": "notify"
        }
        
        try:
            response = requests.post(
                url=f"{config['base_url']}/file_notify?"
                    f"device_name={config['device_name']}"
                    f"&user_name={config['user_name']}"
                    f"&type={config['event_type']}",
                headers={"Content-Type": "application/json"},
                data=json.dumps({
                    "device_name": config["device_name"],
                    "user_name": config["user_name"],
                    "version": "1.0",
                    "event_category": "file",
                    "event_name": "notify",
                    "event_time": int(time.time()),
                    "send_time": int(time.time()),
                    "data": [{
                        "action": "创建",
                        "is_dir": "true",
                        "source_file": "/115/nas/MPLink/movie",
                        "destination_file": ""
                    }]
                }),
                timeout=10
            )
            response.raise_for_status()
            send_telegram_message(f"📁 文件操作通知成功 | 响应: {response.json()}")
        except Exception as e:
            send_telegram_message(f"⚠️ 文件通知失败 | 错误: {str(e)}")

    @classmethod
    def notify_file_creation(cls) -> bool:
        """触发异步文件创建通知
        Returns: 
            bool: 是否成功启动线程
        """
        try:
            threading.Thread(
                target=cls._notify_sync,
                name="OneStrmNotifier_Thread",
                daemon=True
            ).start()
            return True
        except RuntimeError as e:
            send_telegram_message(f"❌ 线程启动失败: {str(e)}")
            return False