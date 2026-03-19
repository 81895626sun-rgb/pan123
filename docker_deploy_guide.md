# Pan123-Monitor Docker 部署指南

本项目基于 Docker 和 Docker Compose 进行容器化部署，具有隔离性好、部署快捷、支持后台静默运行和挂载外部存储等优点。本指南将指导您如何一步步在 Linux 服务器或 NAS（如群晖、威联通、Unraid）上完成部署。

## 1. 部署前准备

在开始部署前，请确保您的服务器或 NAS 已经安装了以下必要组件：
- [Docker](https://docs.docker.com/engine/install/)
- [Docker Compose](https://docs.docker.com/compose/install/)

## 2. 准备项目文件与目录

请将项目代码克隆或上传至您的服务器中（例如上传至 `/opt/pan123` 目录）。
为了保证日志和数据库持久化，需要手动创建 `data` 记录文件夹，或者 Docker 首次运行也会自动为您创建。

进入项目根目录：
```bash
cd /opt/pan123 # 请替换为您的真实项目路径
mkdir -p data
```
*(注意：`data` 目录下会存放 `cid_mapping.db`, `upload_state.db` 以及相关的日志文件 `error.log` 和 `upload.log`。)*

## 3. 配置环境变量

项目需要 `MONITOR_DIR`、网盘账号/Cookie、云端路径等参数。

1. **创建配置文件**
   使用提供的模板拷贝一份配置文件：
   ```bash
   cp .env.docker .env
   ```

2. **修改配置文件 `.env`**
   使用您惯用的文本编辑器（如 `nano` 或 `vim`）修改 `.env`：
   ```bash
   vim .env
   ```
   **重点配置说明：**
   - **123云盘**：`P123_PASSPORT`（手机号）和 `P123_PASSWORD`。
   - **115网盘**：`P115_COOKIE`（在浏览器 F12 获取）。
   - **路径挂载映射**：
     由于是在 Docker 容器内运行，所有的路径必须使用 **Linux 标准路径形式（`/`）**。
     - `local_root=/mnt`：应用在容器内部识别的根目录。
     - `cloud_prefix=nas/MPLink`：对应的云端目录前缀。
     - `MONITOR_DIR=/mnt`：**容器内**监听的绝对路径。
   - **Telegram通知**（可选）：设置 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_PROXY_URL`。

## 4. 调整 docker-compose.yml 路径挂载 (重要！)

在官方提供的 `docker-compose.yml` 中，有一行配置为：
```yaml
volumes:
  - ./env:/app/.env:ro
```
如果您是将配置文件命名为了 `.env`，这里**必须**将挂载路径修正为：
```yaml
volumes:
  - ./.env:/app/.env:ro
```

**此外，配置物理机的监控目录映射：**
检查 `docker-compose.yml` 这一行：`${MONITOR_DIR}:${MONITOR_DIR}:ro`。
这表示 Docker 会将物理机上的 `$MONITOR_DIR` 目录挂载到容器内同名目录。
- 您需要在宿主机执行部署前，确保宿主机的 `.env` 文件里 `MONITOR_DIR` 指向了**物理机实际存放文件的正确目录**（比如 `/share/Movies`）。
- 容器内也会映射到 `/share/Movies`，因此 `local_root` 等变量也要与之对应。

*(如果您使用的宿主机路径和期望的容器内路径不一致，可以直接修改 `docker-compose.yml` 中的挂载语法，例如：`- /volume1/Media:/mnt:ro`，并相应地把 `.env` 中的 `MONITOR_DIR` 和 `local_root` 修改为 `/mnt`。)*

## 5. 构建与启动

在项目根目录（包含 `docker-compose.yml` 和 `Dockerfile` 的目录）执行以下命令进行构建并放入后台运行：

```bash
docker-compose up -d --build
```
*（由于我们在 Dockerfile 中使用了 `python:3.11-slim` 并安装了 `cifs-utils` 等依赖，初次构建可能需要几分钟时间下载镜像并安装 Python 包）。*

## 6. 日志查看与系统维护

- **查看实时输出的监控日志**：
  ```bash
  docker-compose logs -f
  ```
  *(注：该命令能看到是否有“启动目录监控”、“扫描间隔”等成功字样)*

- **查看文件级别的日志流**：
  因为您映射了 `./data/upload.log` 和 `./data/error.log`，您可以随时在宿主机的 `data` 目录下直接 `tail` 查看具体的上传事务记录：
  ```bash
  tail -f ./data/upload.log
  ```

- **停止与重启**：
  - 重启服务：`docker-compose restart`
  - 停止并移除容器：`docker-compose down`

## 7. 常见问题排查

1. **容器一直不断重启（Restarting）**：
   查看启动日志报错 `docker-compose logs`，通常是因为 `.env` 配置不规范或者找不到相关的 `.env` 以致于 `${MONITOR_DIR}` 环境变量缺失，导致 `monitor.py` 在检测路径时主动 `sys.exit(1)` 退出。
2. **监控没有触发上传**：
   确认您的文件是否真实挂载进了容器中。可以使用 `docker exec -it pan123-monitor sh` 进入容器内部，尝试使用 `ls $MONITOR_DIR` 看看是否能看到您外部 NAS/物理机的文件。不能的话需重查 `volumes` 映射逻辑。
