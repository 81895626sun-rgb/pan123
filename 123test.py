import os
from dotenv import load_dotenv
from p123client import P123Client

load_dotenv()
passport = os.getenv("P123_PASSPORT")
password = os.getenv("P123_PASSWORD")
if not passport or not password:
    raise SystemExit("未在 .env 配置 P123_PASSPORT / P123_PASSWORD")

client = P123Client(passport, password)
payload = {
                "parentFileId": 7948523,  # 关键点
                "driveId": "0",             # 必需参数
                "limit": "100",
                "Page": "2",
                "orderBy": "file_id",
                "orderDirection": "desc",
                "event": "homeListFile",
                "inDirectSpace": "false",
                "trashed": "false"
            }
response = client.fs_list_new(payload)
data = response.get('data', {})
print(data)
