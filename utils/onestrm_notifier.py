#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import threading
import requests
from typing import List, Optional


class OneStrmNotifier:
    """QMediaSync 同步触发通知器（实例化，接收 Config 注入）。"""

    def __init__(self, config, notifier=None):
        """
        :param config: Config 对象（从 config.py）
        :param notifier: TelegramNotifier 实例（可选，用于通知报告）
        """
        self._base_url = config.qmediasync_base_url
        self._api_key = config.qmediasync_api_key
        self._path_ids = list(config.qmediasync_path_ids)
        self._debounce_seconds = config.qmediasync_debounce_seconds
        self._notifier = notifier

        self._debounce_timer: Optional[threading.Timer] = None
        self._debounce_lock = threading.Lock()

    # ── 公开接口 ──

    def trigger_qmediasync(self, path_ids: Optional[List[int]] = None,
                           full: bool = False) -> bool:
        """防抖触发 QMediaSync 同步任务。
        多次调用会重置计时器，最后一次调用后等 debounce_seconds 秒才真正触发。
        Args:
            path_ids: 同步目录ID列表，默认使用 Config 里的 QMEDIASYNC_PATH_IDS
            full: True=全量同步，False=增量同步（默认）
        Returns:
            bool: 是否成功重置/启动防抖计时器
        """
        ids = path_ids if path_ids is not None else self._path_ids
        try:
            with self._debounce_lock:
                if self._debounce_timer is not None:
                    self._debounce_timer.cancel()
                self._debounce_timer = threading.Timer(
                    self._debounce_seconds,
                    self._do_trigger,
                    args=(ids, full)
                )
                self._debounce_timer.daemon = True
                self._debounce_timer.start()
            return True
        except RuntimeError as e:
            if self._notifier:
                self._notifier.send_message(
                    f"❌ QMediaSync 防抖计时器启动失败: {str(e)}"
                )
            return False

    # ── 内部方法 ──

    def _do_trigger(self, path_ids: List[int], full: bool) -> None:
        """防抖计时器到期后实际执行同步"""
        for pid in path_ids:
            self._trigger_sync(pid, full)

    def _trigger_sync(self, path_id: int, full: bool) -> None:
        """触发 QMediaSync 同步（增量或全量）"""
        endpoint = "full-start" if full else "start"
        url = f"{self._base_url}/api/sync/path/{endpoint}?api_key={self._api_key}"
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
            if self._notifier:
                self._notifier.send_message(
                    f"🔄 QMediaSync {sync_type}同步触发成功 | "
                    f"目录ID={path_id} | {result.get('message', '')}"
                )
        except Exception as e:
            if self._notifier:
                self._notifier.send_message(
                    f"⚠️ QMediaSync 同步触发失败 | "
                    f"目录ID={path_id} | 错误: {str(e)}"
                )