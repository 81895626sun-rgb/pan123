# utils/telegram_notifier.py
import os
import requests
import logging
from typing import Optional, Dict, Any
from dotenv import load_dotenv
from pathlib import Path

# 加载父目录下的.env文件
env_path = Path(__file__).resolve().parent.parent / '.env'
load_dotenv(dotenv_path=env_path)

# 获取模块级日志记录器
logger = logging.getLogger(__name__)

class TelegramNotifier:
    """Telegram消息通知工具（支持文本/文件/重试机制/代理）"""
    def __init__(self, 
                 bot_token: Optional[str] = None,
                 chat_id: Optional[str] = None,
                 proxy_url: Optional[str] = None):
        """\
        初始化通知器\
        :param bot_token: Telegram Bot Token (默认从.env文件读取)\
        :param chat_id: 目标聊天ID (默认从.env文件读取)\
        :param proxy_url: 代理服务器URL (默认从.env文件读取)\
        """
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.proxy_url = proxy_url or os.getenv("TELEGRAM_PROXY_URL")
        
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}/"
        if not self.bot_token or not self.chat_id:
            missing = "BOT_TOKEN" if not self.bot_token else "CHAT_ID"
            logger.error("Telegram配置缺失: %s", missing)
            raise ValueError(f"Missing Telegram credentials: {missing}")

    def _send_request(self, 
                     endpoint: str, 
                     payload: Dict[str, Any], 
                     files: Optional[Dict[str, Any]] = None,
                     retries: int = 3) -> bool:
        """\
        发送请求到Telegram API（含自动重试）\
        :param endpoint: API端点\
        :param payload: 请求负载\
        :param files: 文件字典\
        :param retries: 重试次数\
        """
        url = self.base_url + endpoint
        proxies = {'https': self.proxy_url} if self.proxy_url else None
        
        for attempt in range(retries):
            try:
                response = requests.post(
                    url, 
                    data=payload, 
                    files=files,
                    timeout=10,
                    proxies=proxies
                )
                if response.status_code == 200:
                    logger.info("Telegram消息发送成功")
                    return True
                else:
                    error_desc = response.json().get('description', '未知错误')
                    logger.warning("API错误[%d]: %s", response.status_code, error_desc)
            except requests.exceptions.RequestException as e:
                logger.error("请求失败[尝试 %d/%d]: %s", attempt+1, retries, str(e))
        return False

    def send_message(self, 
                    text: str, 
                    parse_mode: str = "MarkdownV2",
                    disable_web_preview: bool = True) -> bool:
        """\
        发送文本消息\
        :param text: 消息内容（支持Markdown）\
        :param parse_mode: 文本解析模式（HTML/MarkdownV2）\
        :param disable_web_preview: 禁用链接预览\
        """
        if parse_mode == "MarkdownV2":
            text = self._escape_markdown(text)
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_preview
        }
        return self._send_request("sendMessage", payload)

    def send_file(self, 
                 file_path: str, 
                 caption: str = "") -> bool:
        """\
        发送文件\
        :param file_path: 本地文件路径\
        :param caption: 文件描述（可选）\
        """
        if not os.path.exists(file_path):
            logger.error("文件不存在: %s", file_path)
            return False
        with open(file_path, 'rb') as f:
            files = {'document': f}
            payload = {
                "chat_id": self.chat_id,
                "caption": caption[:1024]  # 标题长度限制
            }
            return self._send_request("sendDocument", payload, files=files)

    @staticmethod
    def _escape_markdown(text: str) -> str:
        """转义MarkdownV2特殊字符"""
        escape_chars = '_*[]()~`>#+-=|{}.!'
        return ''.join(f'\\{char}' if char in escape_chars else char for char in text)

def send_telegram_message(text: str, **kwargs) -> bool:
    """\
    发送Telegram消息的快捷函数\
    :param text: 消息内容\
    :param kwargs: 其他参数（parse_mode, disable_web_preview等）\
    """
    notifier = TelegramNotifier()
    return notifier.send_message(text, **kwargs)