# 项目运行环境
- Python 环境: 3.13.2

---

# 模块功能说明

## pan123.py
- 功能：指定一个目录，可以获取到上传所需的 `FileId`。
- `convert_to_cloud_path` 方法：将本地路径转化为网盘路径。
- `smart_upload` 方法：用于文件的上传（参考自 123pan.py）。

## monitor.py
- 功能：监控目录，并输出发生变化的文件路径。
- 逻辑处理：
  输出的为新增的最短路径的文件夹或文件。
  - **如果是文件**：直接加入上传队列。
  - **如果是文件夹**：它下面可能会有嵌套的文件夹和文件，因此需要递归处理：
    先使用 `client.fs_mkdir()` 新建文件夹，之后进入递归：遇到文件夹就新建，遇到文件就加入上传队列。
  - **注意事项**：在递归时进入下一层目录，必须要先获取 `dataInfo` 里的 `FileId`，然后才能上传到这个文件夹里。
  路径需要依次分割查找，查找到最后没有的那个文件或文件夹，如果是文件夹就新建，如果是文件就使用 `smart_upload` 方法进行上传。
  最后要检查是否所有的任务都已经检查并完成。

## upload_large_video() 方法
- 功能：实现多线程分块上传大文件。

---

# 待优化与计划
- [ ] 增加 Telegram 通知功能：完成时通知上传成功和错误信息。
- [ ] 将项目中的可配置项提取出来，方便集中配置。

---

# 更新日志

- **2025-07-30**：
  - 更新了 `check_changes` 方法。

- **2025-07-13**：
  - 更新了 p115client 的版本至 `0.0.5.12.3`。
  - 第一步：更新 `client.py`，使其可以支持两个网盘的 client 单例化。（`client.py` 测试成功，使用的是 `test_client.py`）。
  - 第二步：修改并添加 115 模块内容。将 115 模块的函数修改并添加到 `pan123` 中。
  - *记录的问题*：存在逻辑问题，有的方法需要两个 client，有的方法只需要一个 client。

---

# 检查出的 BUG

- **2025-07-29**：
  1. 漏查文件问题：目前使用的是“快照新增”来检查文件，即使使用了最短路径检查也不该漏掉文件，但是测试中发现“凡人修仙传”就这样被漏掉了，需要排查。
  2. 临时文件被上传：复制文件时，NAS 会生成一个以 `-upload-tmp` 为末尾的文件（例如 `17365189558169502330-upload-tmp`）。由于目前没有异常后缀识别机制，导致这个临时文件会被直接加入上传队列。

---

# 接口调用及返回结果示例

## 1. 登录与用户信息
**接口：**
```python
client.user_info()
```

**错误输出（账号或密码错误）：**
```python
p123.P123OSError: [Errno 5] {'code': 1, 'message': '账号或密码错误', 'data': None, 'x-traceID': '528aa568-99ee-4b1e-ad7c-d3eb20f46ed6_kong-db-59578c6fd8-2gmnq'}
```

**正常返回示例：**
```python
{'code': 0, 'message': 'ok', 'data': {'UID': REDACTED, 'Nickname': 'REDACTED', 'SpaceUsed': 20608205682175, 'SpacePermanent': 30580167147520, 'SpaceTemp': 0, 'FileCount': 55955, 'SpaceTempExpr': '2027-06-04 09:13:24', 'Mail': '', 'Passport': REDACTED, 'HeadImage': 'https://statics.123pan.com/static-by-custom/default_avatar.png', 'BindWechat': True, 'StraightLink': False, 'OpenLink': 0, 'Vip': True, 'VipExpire': '2027-06-04 09:13:24', 'SpaceBuy': True, 'VipExplain': '3年', 'SignType': 0, 'ContinuousPayment': False, 'ContinuousPaymentDate': '', 'ContinuousPaymentAmount': 0, 'ContinuousPaymentDuration': 0, 'IsBeforeBuyVipProduct': True, 'VipLevel': 1, 'GrowSpaceAddCount': 10, 'IsAuthentication': True, 'UserVipDetailInfos': [{'TimeDesc': '2027.06.04 09:13:24 到期', 'StartTime': 1717463605, 'EndTime': 1812071604, 'IsUse': True, 'Level': 1, 'VipDesc': 'VIP会员：'}], 'IsShowAdvertisement': False, 'DirectTraffic': 105729769116, 'ShareTraffic': 107349671936, 'HTTPSCount': 0, 'SafeBoxFileId': 0, 'OSSCount': 0, 'IsDeveloperEquity': False, 'IsBlockShare': False, 'BackupFileInfo': {'BackupFileId': 22375390, 'MobileTerminalBackupFileName': '我的123备份', 'DesktopTerminalBackupFileName': '我的123备份'}, 'UserVipDetail': {'VipStatus': 3, 'UserVipDetailInfos': [{'TimeDesc': '2027.06.04 09:13:24 到期', 'StartTime': 1717463605, 'EndTime': 1812071604, 'IsUse': True, 'Level': 1, 'VipDesc': 'VIP会员至：'}], 'UserVipExpiredDetailInfos': []}}}
```

## 2. 新建文件夹
**接口：**
```python
client.fs_mkdir("测试", parent_id="0")
```

**正常返回示例：**
```python
{'code': 0, 'message': 'ok', 'data': {'AccessKeyId': None, 'SecretAccessKey': None, 'SessionToken': None, 'Expiration': None, 'Key': '', 'Bucket': '', 'FileId': 0, 'Reuse': False, 'Info': {'FileId': 22894658, 'FileName': '测试', 'Type': 1, 'Size': 0, 'ContentType': '', 'S3KeyFlag': 'REDACTED-0', 'CreateAt': '2025-05-04T18:06:04.573222132+08:00', 'UpdateAt': '2025-05-04T18:06:04.573222207+08:00', 'Hidden': False, 'Etag': '', 'Status': 0, 'ParentFileId': 0, 'Category': 0, 'PunishFlag': 0, 'ParentName': '', 'DownloadUrl': '', 'AbnormalAlert': 0, 'Trashed': False, 'TrashedExpire': '', 'TrashedAt': '', 'StorageNode': 'm0', 'DirectLink': 2, 'AbsPath': '', 'PinYin': 'cs', 'PreviewType': 0, 'BusinessType': 0, 'Thumbnail': '', 'Operable': False, 'StarredStatus': 0, 'HighLight': ''}, 'UploadId': '', 'DownloadUrl': '', 'StorageNode': '', 'EndPoint': '', 'UploadFileStatus': 0, 'SliceSize': '16777216'}}
```

## 3. 文件上传
**接口：**
```python
client.upload_file()
```

**普通上传成功返回：**
```python
{'code': 0, 'message': 'ok', 'data': {'file_info': {'FileId': 24054628, 'FileName': '按学籍学校抽取班级结果.xlsx', 'Type': 0, 'Size': 184501, 'ContentType': '', 'S3KeyFlag': 'REDACTED-0', 'CreateAt': '0001-01-01T00:00:00Z', 'UpdateAt': '0001-01-01T00:00:00Z', 'Hidden': False, 'Etag': 'a2ad183ea1c0528c5dd3321c6ae708fd', 'Status': 0, 'ParentFileId': 0, 'Category': 0, 'PunishFlag': 0, 'ParentName': '', 'DownloadUrl': '', 'AbnormalAlert': 0, 'Trashed': False, 'TrashedExpire': '', 'TrashedAt': '', 'StorageNode': 'm82', 'DirectLink': 2, 'AbsPath': '', 'PinYin': '', 'PreviewType': 1, 'BusinessType': 0, 'Thumbnail': '', 'Operable': False, 'StarredStatus': 0, 'HighLight': ''}}}
```

**秒传成功返回（触发 Reuse=True）：**
```python
{'code': 0, 'message': 'ok', 'data': {'AccessKeyId': None, 'SecretAccessKey': None, 'SessionToken': None, 'Expiration': None, 'Key': '', 'Bucket': '', 'FileId': 0, 'Reuse': True, 'Info': {'FileId': 23113369, 'FileName': '汉江怪物 (2006) - 2160p.mkv', 'Type': 0, 'Size': 35022821779, 'S3KeyFlag': 'REDACTED-0', 'CreateAt': '2025-05-08T00:14:55.33995259+08:00', 'UpdateAt': '2025-05-08T00:14:55.33995269+08:00', 'Hidden': False, 'Etag': 'e220c6b1743e90db7e197e317da039e8', 'Status': 0, 'ParentFileId': 23113355, 'Category': 2, 'PunishFlag': 0, 'ParentName': '', 'AbnormalAlert': 1, 'Trashed': False, 'TrashedExpire': '', 'TrashedAt': '', 'StorageNode': 'm75', 'DirectLink': 0, 'AbsPath': '', 'PinYin': 'hjgw20062160pmkv', 'PreviewType': 0, 'BusinessType': 0, 'Thumbnail': '', 'Operable': False, 'StarredStatus': 0, 'HighLight': ''}, 'UploadId': '', 'DownloadUrl': '', 'StorageNode': '', 'EndPoint': '', 'UploadFileStatus': 0, 'SliceSize': '134217728'}}
```

**上传文件没有反应时的返回（可能因为分块或队列分配）：**
```python
{'code': 0, 'message': 'ok', 'data': {'AccessKeyId': None, 'SecretAccessKey': None, 'SessionToken': None, 'Expiration': None, 'Key': '57508cfd/REDACTED-0/57508cfdf7352c99238cbab37c2bfaa9', 'Bucket': '123-681', 'FileId': 1056577504, 'Reuse': False, 'Info': None, 'UploadId': '2~35Mv0RJB3C-elF4BjChQG5FhJGNDNuH', 'DownloadUrl': '', 'StorageNode': 'm78', 'EndPoint': 'https://m78.123624.com', 'UploadFileStatus': 0, 'SliceSize': '16777216'}}
```

**同名文件检测提示：**
```python
{'code': 5060, 'message': '检测到1个同名文件，文件名最终幻想女孩.mp4已存在，是否继续？', 'data': None}
```
