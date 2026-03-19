from p123client import P123Client
client = P123Client(passport="REDACTED", password="REDACTED")
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