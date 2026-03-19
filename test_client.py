import logging
from dotenv import load_dotenv
from client import CloudClientManager
from p115client import check_response


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def test_cloud_clients():
    """测试123和115云盘客户端"""
    # 加载环境变量
    load_dotenv()
    
    manager = CloudClientManager()
    
    # 测试123云盘
    try:
        print("\n=== 测试123云盘 ===")
        client_123 = manager.get_client('123')
        user_info = client_123.user_info()
        print(f"123用户信息: {user_info['data']['Nickname']}")
    except Exception as e:
        logging.error(f"123云盘测试失败: {e}")
    
    # 测试115云盘
    try:
        print("\n=== 测试115云盘 ===")
        client_115 = manager.get_client('115')
        user_info = client_115.user_info()
        if check_response(user_info):
            print(f"115用户信息: {user_info['data']['user_name']}")
        else:
            logging.error("115用户信息获取失败")
    except Exception as e:
        logging.error(f"115云盘测试失败: {e}")
    
    # 测试重置功能
    try:
        print("\n=== 测试重置客户端 ===")
        manager.reset_client('123')
        manager.reset_client('115')
        print("客户端重置成功")
    except Exception as e:
        logging.error(f"重置测试失败: {e}")

if __name__ == "__main__":
    test_cloud_clients()