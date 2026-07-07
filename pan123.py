from p123client import P123Client
import hashlib
import posixpath
import logging
import time
import os
import re
import requests
from typing import Optional, Dict,  Set, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from requests.exceptions import RequestException
import random
from typing import Tuple
from cid_db_115 import upsert_mapping ,get_cid
import sqlite3


def normalize_drive_letter(path):
    """规范化Windows路径中的盘符，保持其他部分不变"""
    if os.name == 'nt':
        drive, tail = os.path.splitdrive(path)
        if drive:
            return drive.upper() + tail
    return path
# 115和123都是nas文件夹下，这个函数可以通用
def convert_to_cloud_path(local_root, cloud_prefix, local_full_path):
    """
    将本地路径转换为网盘路径

    参数:
        local_root: 本地根目录(将被替换的部分)
        cloud_prefix: 网盘前缀路径
        local_full_path: 要转换的本地完整路径（必须是绝对路径）

    返回:
        转换后的网盘路径(使用POSIX格式)

    异常:
        ValueError: 如果路径无效或不在local_root下
    """
    # 检查输入是否为绝对路径
    if not os.path.isabs(local_full_path):
        raise ValueError("local_full_path 必须是绝对路径")

    # 规范化路径（解析符号链接并统一大小写）
    norm_local_root = os.path.realpath(os.path.normpath(local_root))
    norm_local_full = os.path.realpath(os.path.normpath(local_full_path))

    # Windows下处理盘符的大小写，统一为大写
    norm_local_root = normalize_drive_letter(path=norm_local_root)
    norm_local_full = normalize_drive_letter(path=norm_local_full)


    # 去除末尾分隔符
    norm_local_root = norm_local_root.rstrip(os.sep)

    # 验证路径包含关系
    try:
        common_path = os.path.commonpath([norm_local_root, norm_local_full])
    except ValueError:
        raise ValueError(f"路径 '{local_full_path}' 不在根目录 '{local_root}' 下")

    if common_path != norm_local_root:
        raise ValueError(f"路径 '{local_full_path}' 不在根目录 '{local_root}' 下")

    # 获取相对路径并转换为POSIX格式
    relative = os.path.relpath(norm_local_full, norm_local_root)
    relative_posix = relative.replace(os.sep, '/')

    # 组合网盘路径
    return posixpath.join(cloud_prefix, relative_posix)

#115专用函数find_cid_by_paths
def find_cid_by_parts(client, parts: list) -> Tuple[int, list]:
    """
    根据路径的各个部分查找并返回对应的 current_id 和剩余 parts，支持分页查询和失败重试
    
    Args:
        client: 客户端对象
        parts: 目录分割后的列表 (e.g. ["folder1", "folder2"])
    
    Returns:
        tuple: (current_id, remaining_parts)
            current_id: 最后成功找到的目录ID (0表示根目录)
            remaining_parts: 剩余未找到的部分列表 (空列表表示全部找到)
    """
    if not parts:
        return 0, []  # 根目录，无需继续查找
    
    # 1. 先尝试从数据库查询完整路径
    full_path = "/" + "/".join(parts)  # 拼接完整路径
    try:
        db_cid = get_cid(full_path)
        if db_cid and db_cid != 0:
            logging.info(f"从数据库找到缓存记录: {full_path} -> {db_cid}")
            return int(db_cid), []  # 数据库中找到完整记录
    except sqlite3.Error as e:
        logging.warning(f"数据库查询失败（继续API查询）: {e}")


    current_id = 0  # 从根目录开始
    remaining_parts = parts.copy()  # 避免修改原始列表
    max_retries = 3  # +++ 固定最大重试次数为 3
    
    while remaining_parts:
        current_part = remaining_parts[0]
        found = False
        offset = 0  # 分页起始索引
        
        # 分页查询，直到找到目标或遍历完所有数据
        while True:
            payload = {
                "cid": current_id,
                "limit": 28,
                "offset": offset,
                "o": "user_ptime",
                "asc": 0
            }
            
            # +++ 带重试的请求逻辑
            retry_count = 0
            response = None
            
            while retry_count < max_retries:
                try:
                    response = client.fs_files(payload)

                    time.sleep(random.uniform(1, 3))  # 随机延迟 1~3 秒
                    if isinstance(response, dict) and response.get("state") is True and response.get("errNo") == 0:  # 请求成功则退出重试循环
                        break
                    else:
                        logging.error(f"失败返回的response为{response}")
                        error_msg = response.get("error", "Unknown error")
                        errNo = response.get("errNo", "Unknown")
                        logging.error(f"请求失败（{retry_count + 1}/{max_retries}），错误码：{errNo}，错误信息：{error_msg}")
                        raise RequestException(f"API Error: {error_msg}")  # 触发重试

                except RequestException as e:
                    logging.error(f"请求异常（{retry_count + 1}/{max_retries}），错误：{e}")
                    time.sleep(random.uniform(1, 3))  # 随机延迟 1~3 秒
                    retry_count += 1
            
            # 如果仍然失败或无数据，终止当前层查询
            # 最终检查是否成功
            if retry_count >= max_retries and (response is None or not response.get("state") or response.get("errNo") != 0):
                logging.error(f"重试 {max_retries} 次后仍失败，最终错误码：{response.get('errNo', 'Unknown')}")
            else:
                logging.info("请求成功！")
            
            # 遍历查询结果
            for item in response['data']:
                if isinstance(item, dict) and item['n'] == current_part:
                    current_id = item['cid']
                    remaining_parts.pop(0)  # 移除已找到的部分
                    found = True
                    # 3. 将找到的部分路径存入数据库
                    partial_path = "/" + "/".join(parts[:len(parts)-len(remaining_parts)])
                    try:
                        upsert_mapping(partial_path, current_id)
                        logging.info(f"新增数据库记录: {partial_path} -> {current_id}")
                    except sqlite3.Error as e:
                        logging.warning(f"缓存写入失败: {e}")
                    break

            
            if found or len(response['data']) < 28:
                break  # 找到目标或已查完所有数据
            
            offset += 28  # 继续查询下一页
            time.sleep(0.5)  # 避免请求过快
        
        if not found:
            break  # 当前部分未找到，停止查找
    
    return current_id, remaining_parts


#123盘专用的
def find_directory_path(client, parts):
    current_id = "0"
    
    for i, part in enumerate(parts):
        found = False
        page = 1  # 页码从1开始
        
        while True:
            # 构建payload字典
            payload = {
                "parentFileId": current_id,
                "driveId": "0",
                "limit": "100",
                "Page": str(page),  # 添加页码参数
                "orderBy": "file_id",
                "orderDirection": "desc",
                "event": "homeListFile",
                "inDirectSpace": "false",
                "trashed": "false"
            }
            
            try:
                response = client.fs_list_new(payload)
                data = response.get('data', {})
                
                # 在当前页数据中查找目标目录
                for item in data.get('InfoList', []):
                    if str(item.get('Type')) == '1' and item.get('FileName') == part:
                        current_id = str(item['FileId'])
                        found = True
                        break
                        
                # 判断是否终止循环
                if found or data.get('Next') == '-1':
                    break
                    
                # 还有下一页，页码加1
                page += 1
                
            except Exception as e:
                logging.error(f"查询失败: {e}")
                break
                
        if not found:
            return current_id, parts[i:]
            
    return current_id, []


def calculate_md5(file_path: str) -> str:
    """带进度显示的MD5计算"""
    file_size = os.path.getsize(file_path)
    # print(f"计算MD5({file_size/1024/1024:.2f}MB)...")
    
    hash_md5 = hashlib.md5()
    last_print = 0
    with open(file_path, "rb") as f:
        for i, chunk in enumerate(iter(lambda: f.read(1024*1024), b"")):  # 1MB块
            hash_md5.update(chunk)
            if time.time() - last_print > 1:  # 每秒更新进度
                percent = f.tell() / file_size * 100
                # print(f"MD5进度: {percent:.1f}%", end='\r')
                last_print = time.time()
    
    # print("\nMD5计算完成")
    return hash_md5.hexdigest()

def upload_chunk(args: Tuple[int, bytes, str, int]) -> Tuple[int, Optional[str]]:
    """123盘优化内存的分块上传"""
    part_num, chunk, current_url, max_retries = args
    chunk_size = len(chunk)
    try:
        # 立即释放内存
        headers = {'Content-Length': str(chunk_size),'Content-Type': 'application/octet-stream'}
        response = requests.put(
            current_url,
            data=chunk,
            headers=headers
        )
        
        del chunk  # 主动释放内存
        
        if response.status_code not in [200, 201]:
            raise ValueError(f"状态码异常: {response.status_code}")
        
        etag = response.headers.get("ETag", "").strip('"')
        if not etag:
            raise ValueError("ETag缺失")
        
        logging.info(f"[成功] 分块 {part_num} (大小: {chunk_size/1024/1024:.1f}MB) etag的值为{etag}")
        return part_num, etag
    except Exception as e:
        logging.warning(f"[分块失败] 序号 {part_num} 遇到网络波动或异常，稍后重试。详情: {str(e)[:100]}")
        return part_num, None

def process_upload_batch(
    client: P123Client,
    file_path: str,
    upload_data: Dict,
    parts_to_upload: Set[int],
    max_workers: int = 3,
    slice_size: int = 134217728
) -> Set[int]:
    """严格控制内存的批量上传"""
    failed_parts = set()
    uploaded_parts = []
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        
        for part_num in sorted(parts_to_upload):
            try:
                # 获取上传URL
                prepare_parts = client.upload_prepare({
                    "bucket": upload_data["Bucket"],
                    "key": upload_data["Key"],
                    "storageNode": upload_data["StorageNode"],
                    "uploadId": upload_data["UploadId"],
                    "partNumberStart": part_num,
                    "partNumberEnd": part_num + 1
                })
                # print("此时获取的为第"+str(part_num)+"部分的url")
                url = prepare_parts['data']['presignedUrls'].get(str(part_num))
                if not url:
                    raise ValueError("URL缺失")
                
                # 分块读取（严格控制内存）
                with open(file_path, "rb") as f:
                    f.seek((part_num - 1) * slice_size)
                    chunk = f.read(slice_size)
                
                # 提交任务（限制队列长度）
                futures.append(executor.submit(
                    upload_chunk, 
                    (part_num, chunk, url, 3)
                ))
                
                # 控制内存占用
                if len(futures) >= max_workers * 2:
                    for future in as_completed(futures):
                        part_num, etag = future.result()
                        if etag:
                            uploaded_parts.append({"PartNumber": part_num, "ETag": etag})
                        else:
                            failed_parts.add(part_num)
                    futures = []
                    
            except Exception as e:
                logging.warning(f"[分块准备失败] 序号 {part_num} 连接失败，稍后重试。详情: {str(e)[:100]}")
                failed_parts.add(part_num)
        
        # 处理剩余任务
        for future in as_completed(futures):
            part_num, etag = future.result()
            if etag:
                uploaded_parts.append({"PartNumber": part_num, "ETag": etag})
            else:
                failed_parts.add(part_num)
    
    return failed_parts

def upload_large_video(
    client: P123Client,
    file_path: str,
    parent_id: int = 0,
    enable_resume: bool = True,
    max_workers: int = 3,
    chunk_retry_rounds: int = 3,
    file_name: str = None,
    file_size: int = None,
    file_md5: str = None
) -> Dict:
    """优化后的分块上传主函数"""
    try:
        # 初始化检查
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")
        # print(f"\n开始上传: {file_name} ({file_size/1024/1024:.2f}MB)")
        
        # 创建上传任务
        upload_request = client.upload_request({
            "fileName": file_name,
            "etag": file_md5,
            "size": file_size,
            "duplicate": 2,
            "parentFileId": parent_id,
            "type": 0
        })

        if upload_request["data"].get("Reuse"):
            logging.info("[秒传] 文件已存在")
            return upload_request
            
        upload_data = upload_request["data"]
        slice_size = int(upload_data.get("SliceSize", 134217728))  # 128MB
        part_count = (file_size + slice_size - 1) // slice_size
        logging.info(f"[分块] 总数: {part_count} 单块: {slice_size/1024/1024:.1f}MB")
        
        # 断点续传检查
        uploaded_parts = []
        if enable_resume:
            list_response = client.upload_list({
                "bucket": upload_data["Bucket"],
                "key": upload_data["Key"],
                "storageNode": upload_data["StorageNode"],
                "uploadId": upload_data["UploadId"]
            })
            uploaded_parts = list_response.get("data", {}).get("Parts") or []  # 确保最终是列表
            logging.info(f"[续传] 发现 {len(uploaded_parts)} 个已上传分块")
        
        # 准备上传
        uploaded_parts = uploaded_parts or []  # 如果 None，则赋值为空列表
        uploaded_part_numbers = {int(p["PartNumber"]) for p in uploaded_parts}  # 字符串转整数
        pending_parts = set(range(1, part_count + 1)) - uploaded_part_numbers
        # 分批上传
        failed_parts = process_upload_batch(
            client=client,
            file_path=file_path,
            upload_data=upload_data,
            parts_to_upload=pending_parts,
            max_workers=max_workers,
            slice_size=slice_size
        )
        
        # 重试逻辑
        for retry in range(chunk_retry_rounds):
            if not failed_parts:
                break
                
            logging.info(f"\n[重试] 第 {retry+1} 轮 ({len(failed_parts)}个分块)")
            new_failures = process_upload_batch(
                client=client,
                file_path=file_path,
                upload_data=upload_data,
                parts_to_upload=failed_parts,
                max_workers=max_workers,
                slice_size=slice_size
            )
            failed_parts = new_failures
        
        if failed_parts:
            raise RuntimeError(f"上传失败，剩余分块: {sorted(failed_parts)}")
        
        # 完成上传
        logging.info("\n[完成] 提交最终确认...")
        uploaded_parts.sort(key=lambda x: x["PartNumber"])
        return client.upload_complete({
            **upload_data,
            "isMultipart": part_count > 1,
            "parts": uploaded_parts
        })
        
    except Exception as e:
        # 这个错最后会被外层捕获，这里记录个简短的即可，不要抛庞大的堆栈
        logging.warning(f"123大文件上传中继受阻，剩余分块将交由外层重试。")
        raise


def smart_upload(client, file_source, parent_id, file_name=None, file_size=None, file_md5=None,
                duplicate=2, **request_kwargs):
    """智能三阶段上传"""
    
    try:
        msg = f"开始上传文件: 源路径={file_source}, 父目录ID: {parent_id}"
        # print(msg)
        logging.info(msg)
        # 处理文件路径输入
        file_obj = None
        if isinstance(file_source, (str, os.PathLike)):
            original_file_path = file_source
            if not file_name:
                file_name = os.path.basename(file_source)
                msg = f"自动获取文件名: 源路径={file_source} -> 目标文件名={file_name}"
                # print(msg)
                logging.info(msg)
            if not file_size:
                file_size = os.path.getsize(file_source)
                msg = f"文件大小: {file_size} bytes"
                # print(msg)
                logging.info(msg)
            file_obj = open(file_source, 'rb')
            if not file_md5:
                msg = "开始计算文件MD5..."
                # print(msg)
                logging.info(msg)
                file_md5 = calculate_md5(file_source)
                msg = f"MD5计算完成: {file_md5}"
                # print(msg)
                logging.info(msg)
            file_source = file_obj

        # 第一阶段：尝试秒传
        msg = "尝试秒传..."
        # print(msg)
        logging.info(msg)
        fast_result = client.upload_file_fast(
            file=file_source,
            parent_id=parent_id,
            file_name=file_name,
            file_md5=file_md5,
            file_size=file_size,
            duplicate=duplicate,
            **request_kwargs
        )
        if fast_result and fast_result.get('code') == 0:
            # print(fast_result)
            # 检查Info是否为None
            if fast_result.get('data', {}).get('Info') is None:
                msg = "秒传失败: Info为None"
                # print(msg)
                logging.warning(msg)
            else:
                msg = "秒传成功！"
                # print(msg)
                logging.info(msg)
                return fast_result
        else:
            msg = f"秒传失败: {fast_result.get('message', '未知错误')}"
            # print(msg)
            logging.warning(msg)

        # 根据文件大小决定是否跳过直接上传阶段
        if file_size > 50*1024*1024:  # 大于50MB直接进入分片上传
            msg = "大文件检测(>50MB)，跳过直接上传阶段..."
            print(msg)
            logging.info(msg)
        else:
            # 尝试直接上传
            msg = "秒传失败，尝试直接上传..."
            # print(msg)
            logging.info(msg)
            direct_result = client.upload_file(
                file=file_source,
                parent_id=parent_id,
                file_name=file_name,
                file_md5=file_md5,
                file_size=file_size,
                duplicate=duplicate,
                **request_kwargs
            )
            if direct_result and direct_result.get('code') == 0:
                msg = "直接上传成功！"
                # print(msg)
                logging.info(msg)
                return direct_result
            else:
                msg = f"直接上传失败: {direct_result.get('message', '未知错误')}"
                # print(msg)
                logging.error(msg)

        # 分片上传阶段
        msg = "直接上传失败，开始分片上传..."
        # print(msg)
        logging.info(msg)
        chunked_result = upload_large_video(
            client=client,
            file_path=original_file_path,
            parent_id=parent_id,
            max_workers=3,
            file_name=file_name,
            file_size=file_size,
            file_md5=file_md5
        )
        if chunked_result:
            msg = f"分片上传结果: {chunked_result}"
            # print(msg)
            logging.info(msg)
        return chunked_result

    except Exception as e:
        # 交给最高层 upload_worker 打印精简格式
        raise e
    finally:
        if hasattr(file_source, 'close'):
            file_source.close()
            msg = "文件句柄已关闭"
            # print(msg)
            logging.info(msg)

#115的分块读取
def read_file_in_chunks(file_path, chunk_size=1024*1024*100):  # 默认chunk_size为100MB
    # ""“生成器函数，用于逐块读取文件内容”""
    with open(file_path, 'rb') as file:
        while True:
            chunk = file.read(chunk_size)
            if not chunk:
                break
            yield chunk

    


# ---------- 115上传文件主入口 ----------
def upload_file(client, file_path: str, pid: int | str = 0) -> None:
    """
    智能上传入口
    :param client: P115Client 实例
    :param file_path: 本地文件绝对路径
    :param pid: 目标目录 ID（默认根目录）
    :raises: RuntimeError 上传失败时抛出
    """
    CHUNK_THRESHOLD = 128 * 1024 * 1024  # 128 MiB
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    logging.info(f"开始上传 {file_name} 大小={file_size >> 20} MiB  目标PID={pid}")

    if file_size <= CHUNK_THRESHOLD:
        _upload_small_file(client, file_path, file_name, pid)
    else:
        _upload_large_file(client, file_path, pid)

    logging.info(f"{file_name} 上传成功")


# ---------- 小文件分支 ----------
def _upload_small_file(client, file_path: str, file_name: str, pid: int | str) -> Tuple[str, str]:
    """≤128 MiB：使用 upload_file_sample 整包/秒传"""

    resp = client.upload_file(
        file=file_path,
        filename=file_name,
        pid=pid
    )

    if not resp.get("state"):
        raise RuntimeError(f"upload_file 失败: {resp}")

    # 统一解析
    if resp.get("reuse"):  # 秒传
        pick_code = resp["pickcode"]
        file_name = resp["data"]["filename"]
    else:  # 非秒传
        data = resp["data"]
        pick_code = data["pick_code"]
        file_name = data["file_name"]

    logging.info(f"小文件上传完成 → pick_code={pick_code}  文件名={file_name}")
    return pick_code, file_name



# ---------- 115大文件上传子逻辑 ----------
def _upload_large_file(client, file_path: str, pid: int | str):
    """128 MiB+ 文件上传，秒传/分块自动切换，无 file_id 需求"""
    # p115client 0.0.9.3.x 移除了 upload_file 的 progress 参数（进度回调改用 reporthook，
    # 签名 Callable[[int], Any]，与旧的 progress_cb(info dict) 不兼容）。
    # 此处不再传进度回调；如需恢复进度日志，可改用 reporthook=<callable>。
    resp = client.upload_file(
        file_path,
        pid=pid,
        partsize=128 * 1024 * 1024,
        async_=False,
    )

    if not resp.get("state"):
        raise RuntimeError(f"115 返回错误: {resp}")

    # 秒传
    if resp.get("reuse"):
        pick_code = resp["pickcode"]
        file_name = resp["data"]["filename"]
        mode = "秒传"
    # 分块
    else:
        data = resp["data"]
        pick_code = data["pick_code"]
        file_name = data["file_name"]
        mode = "分块上传"

    logging.info(f"{mode}完成 → pick_code={pick_code}  文件名={file_name}")
    return pick_code, file_name

