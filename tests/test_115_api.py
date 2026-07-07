"""
115 网盘 API 连通性验证（手动冒烟测试）。

用法:
    python tests/test_115_api.py

从项目根目录的 .env 读取 P115_COOKIE，验证:
  - login_status : client.py _new_115_client 用的登录校验
  - fs_files     : 上传路径 find_cid_by_parts 依赖的目录列表接口

本脚本不硬编码任何凭证。失败时退出码非 0。
注意: 115 用 cookie 鉴权，cookie 过期会失败——那是凭证过期，不是客户端版本问题。
"""
import logging
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认 GBK，打印 emoji 会崩；强制 UTF-8（Linux 本就是 UTF-8，无影响）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from client import CloudClientManager  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(root, ".env"))

    if not os.getenv("P115_COOKIE"):
        print("❌ 未在 .env 中配置 P115_COOKIE")
        return 1

    failures = 0
    client = CloudClientManager().get_client("115")

    # 1) login_status —— _new_115_client 用的登录校验（get_client 内部已校验过一次）
    try:
        ok = client.login_status()
        assert ok, "login_status 返回 False"
        print("✅ login_status  True")
    except Exception as e:
        failures += 1
        print(f"❌ login_status  失败: {type(e).__name__}: {str(e)[:200]}")

    # 2) fs_files —— 上传路径 find_cid_by_parts 依赖的接口（列根目录 cid=0）
    try:
        resp = client.fs_files({"cid": 0, "limit": 3, "offset": 0, "o": "user_ptime", "asc": 0})
        state = resp.get("state")
        errNo = resp.get("errNo")
        assert state is True and errNo == 0, f"异常响应: {str(resp)[:200]}"
        print(f"✅ fs_files      state={state} errNo={errNo}")
    except Exception as e:
        failures += 1
        print(f"❌ fs_files      失败: {type(e).__name__}: {str(e)[:200]}")

    print("\n全部通过" if failures == 0 else f"\n{failures} 项失败")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
