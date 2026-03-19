#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import time
import threading
import requests
from typing import Dict, Any
from .telegram_notifier import send_telegram_message  # 使用相对导入

class OneStrmNotifier:
    """异步文件事件通知器（线程版）"""
    
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