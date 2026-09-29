"""
123 云盘 API 连通性验证（手动冒烟测试）。

用法:
    python tests/test_123_api.py

从项目根目录的 .env 读取 P123_PASSPORT / P123_PASSWORD，验证修正后的客户端
能正常访问两个关键接口:
  - user_info   : client.py _test_connection 用的接口，修正前返回 HTML 触发 orjson 解析失败
  - file/list/new : 上传路径 find_directory_path 依赖的接口；修正前 404(域迁移)；
                    9/7 前后起 400 "driveId is required"，需 client.request 直发驼峰

本脚本不硬编码任何凭证。失败时退出码非 0。
"""
import logging
import os
import sys

from dotenv import load_dotenv

# Windows 控制台默认 GBK，打印 emoji 会崩；强制 UTF-8（Linux 本就是 UTF-8，无影响）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 让脚本无论从哪里执行都能导入项目根目录下的模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from client import CloudClientManager  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def main() -> int:
    # 优先加载 tests/ 上一级（项目根）的 .env
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(root, ".env"))

    if not os.getenv("P123_PASSPORT") or not os.getenv("P123_PASSWORD"):
        print("❌ 未在 .env 中配置 P123_PASSPORT / P123_PASSWORD")
        return 1

    failures = 0
    client = CloudClientManager().get_client("123")

    # 1) user_info —— 修正前: 200 + text/html(SPA 壳) -> orjson 解析失败
    try:
        info = client.user_info()
        nickname = info.get("data", {}).get("Nickname")
        assert isinstance(info, dict) and info.get("code") == 0 and nickname, f"异常响应: {str(info)[:200]}"
        print(f"✅ user_info     code={info.get('code')} Nickname={nickname}")
    except Exception as e:
        failures += 1
        print(f"❌ user_info     失败: {type(e).__name__}: {str(e)[:200]}")

    # 2) file/list/new
    #    修正历史: 修正前 404(域迁移)；9/7 前后起 400 "driveId is required"
    #    (上游把参数名改成严格驼峰，p123client.fs_list_new 内部
    #    dict_key_to_lower_merge 把驼峰打成小写，导致服务端不认)
    #    验证修复: 绕过 fs_list_new，直接 client.request 透传驼峰参数
    payload = {
        "parentFileId": "0", "driveId": "0", "limit": "3", "Page": "1",
        "orderBy": "file_id", "orderDirection": "desc", "event": "homeListFile",
        "inDirectSpace": "false", "trashed": "false",
    }
    try:
        listing = client.request("file/list/new", params=payload, base_url="https://123pan.com")
        total = (listing.get("data") or {}).get("Total")
        assert isinstance(listing, dict) and listing.get("code") == 0, f"异常响应: {str(listing)[:200]}"
        print(f"✅ file/list/new code={listing.get('code')} Total={total}")
    except Exception as e:
        failures += 1
        print(f"❌ file/list/new 失败: {type(e).__name__}: {str(e)[:200]}")

    print("\n全部通过" if failures == 0 else f"\n{failures} 项失败")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
