import os
from pathlib import Path
from p123client import P123Client
from p123client.client import DEFAULT_BASE_URL
from p115client import P115Client
import p115oss.upload
from p115client import check_response
import atexit
import logging
from typing import Dict, Optional, Union


# 123pan 已将 API 由 www.123pan.com/b 迁移至裸域名 123pan.com。
# p123client 写死的 DEFAULT_BASE_URL ("https://www.123pan.com/b") 已失效：
#   www.123pan.com 现为纯前端站点，/api/* 返回 SPA HTML 壳（200），/b/api/* 部分接口 404。
# 这会让 user_info 拿到 HTML 触发 orjson 解析失败、fs_list_new 404，导致上传全部失败。
# 在此覆盖 request，把等于库默认值的失效 base_url 重定向到可用 host；
# 登录走的 login.123pan.com（DEFAULT_LOGIN_BASE_URL）不受影响。
_WORKING_123_BASE_URL = "https://123pan.com"


class P123ClientFixed(P123Client):
    """修正 p123client 失效 base_url 的 123 云盘客户端。"""

    def request(
        self,
        url,
        method="GET",
        request=None,
        base_url=DEFAULT_BASE_URL,
        headers=None,
        async_=False,
        **request_kwargs,
    ):
        if isinstance(base_url, str) and base_url == DEFAULT_BASE_URL:
            base_url = _WORKING_123_BASE_URL
        return super().request(
            url,
            method=method,
            request=request,
            base_url=base_url,
            headers=headers,
            async_=async_,
            **request_kwargs,
        )


# p115oss 0.1.0.3 的 upload_init 有 bug: `appversion = payload["appversion"]` 会 KeyError
# (upload_file 构造的 payload 里没 appversion), 115 上传必崩 (被包装成 MultipartUploadAbort)。
# 修正: 调用前注入真实 appversion "36.2.28"。
# 注意: 不能用 p115oss 0.1.0.2 —— 它用假 appversion "99.99.99.99", 会被 115 WAF 拦 405。
_orig_p115oss_upload_init = p115oss.upload.upload_init


def _p115oss_upload_init_fixed(payload, *, async_=False, **request_kwargs):
    if "appversion" not in payload:
        payload = {**payload, "appversion": "36.2.28"}
    return _orig_p115oss_upload_init(payload, async_=async_, **request_kwargs)


p115oss.upload.upload_init = _p115oss_upload_init_fixed


class CloudClientManager:
    """
    云盘客户端管理器（支持123云盘和115云盘）
    功能：
    1. 自动从环境变量/文件加载凭证
    2. 维护各云服务的独立客户端实例
    3. 网络异常时自动重置连接
    4. 程序退出时自动清理
    """
    _instance = None
    _clients: Dict[str, Optional[Union[P123Client, P115Client]]] = {
        '123': None,
        '115': None
    }

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            atexit.register(cls._cleanup)
        return cls._instance

    @classmethod
    def _cleanup(cls):
        """程序退出时自动清理所有客户端"""
        for service, client in cls._clients.items():
            if client:
                try:
                    if service == '123':
                        # 123云盘没有logout方法，直接设为None
                        cls._clients[service] = None
                    else:
                        client.logout()
                    logging.info(f"{service}云盘客户端已注销")
                except Exception as e:
                    logging.error(f"{service}云盘客户端注销失败: {str(e)}")
                finally:
                    cls._clients[service] = None

    def get_client(self, service: str = '123') -> Union[P123Client, P115Client]:
        """
        获取指定云服务的客户端
        :param service: '123' 或 '115'
        :return: 对应的客户端实例
        """
        if service not in self._clients:
            raise ValueError(f"不支持的云服务类型: {service}")

        if self._clients[service] and self._test_connection(service):
            return self._clients[service]

        try:
            if service == '123':
                self._clients[service] = self._new_123_client()
            elif service == '115':
                self._clients[service] = self._new_115_client()
            
            logging.info(f"{service}云盘登录成功")
            return self._clients[service]
        except Exception as e:
            self._clients[service] = None
            logging.error(f"{service}云盘客户端初始化失败: {str(e)}")
            raise RuntimeError(f"{service}云盘客户端初始化失败: {str(e)}")

    def _test_connection(self, service: str) -> bool:
        """测试指定云服务的客户端连接是否有效"""
        if not self._clients[service]:
            return False

        try:
            if service == '123':
                return bool(self._clients[service].user_info())
            elif service == '115':
                return self._clients[service].login_status()
        except Exception as e:
            logging.error(f"{service}云盘连接测试失败: {str(e)}")
            return False

    def _new_123_client(self) -> P123Client:
        """创建新的123云盘客户端"""
        passport = os.getenv('P123_PASSPORT')
        password = os.getenv('P123_PASSWORD')
        if not passport or not password:
            raise ValueError("未设置环境变量 P123_PASSPORT 或 P123_PASSWORD")

        # 正确的初始化方式（P123ClientFixed 修正了失效的 base_url）
        client = P123ClientFixed(passport, password)
        return client

    def _new_115_client(self) -> P115Client:
        """创建新的115云盘客户端"""
        # 优先尝试从环境变量读取cookie
        cookie = os.getenv('P115_COOKIE')
        
        # 如果环境变量没有，尝试从文件读取
        if not cookie:
            cookie_path = Path('115_cookie.txt')
            if cookie_path.exists():
                cookie = cookie_path.read_text(encoding='utf-8').strip()
        
        if not cookie:
            raise ValueError("未设置115云盘cookie（环境变量P115_COOKIE或cookie文件）")

        client = P115Client(cookies=cookie)
        # 使用login_status()检查登录状态
        if not client.login_status():
            logging.error("115云盘登录状态验证失败")
            raise RuntimeError("115云盘登录失败")
        
        logging.info("115云盘登录成功")
        return client

    def reset_client(self, service: str):
        """强制重置指定云服务的客户端"""
        if service not in self._clients:
            raise ValueError(f"不支持的云服务类型: {service}")

        if self._clients[service]:
            try:
                if service == '115':  # 只有115需要logout
                    self._clients[service].logout()
            except Exception as e:
                logging.error(f"{service}云盘客户端注销异常: {str(e)}")
        self._clients[service] = None
        logging.info(f"{service}云盘客户端已重置")