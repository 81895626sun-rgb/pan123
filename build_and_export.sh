#!/usr/bin/env bash
# =============================================================================
# build_and_export.sh
# 拉取 pan123 最新代码 -> 构建 Docker 镜像 -> 导出为 tar 包
# 用法: bash build_and_export.sh [输出目录] [镜像标签]
# 示例: bash build_and_export.sh /opt/images v1.0
# =============================================================================

set -euo pipefail

# ---------- 可配置参数 ----------
REPO_URL="https://github.com/81895626sun-rgb/pan123.git"
REPO_DIR="${HOME}/pan123"           # 本地克隆路径
IMAGE_NAME="pan123-monitor"
IMAGE_TAG="${2:-latest}"
OUTPUT_DIR="${1:-$(pwd)}"
# --------------------------------

FULL_IMAGE="${IMAGE_NAME}:${IMAGE_TAG}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
TAR_FILE="${OUTPUT_DIR}/${IMAGE_NAME}_${IMAGE_TAG}_${TIMESTAMP}.tar"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "[ERROR] $*" >&2; exit 1; }

# 依赖检查
command -v git    >/dev/null 2>&1 || die "git 未安装"
command -v docker >/dev/null 2>&1 || die "docker 未安装"

mkdir -p "${OUTPUT_DIR}"

# ---------- Step 1: 拉取代码 ----------
log "=== Step 1: 同步代码 ==="
if [ -d "${REPO_DIR}/.git" ]; then
    log "仓库已存在，执行 git pull ..."
    git -C "${REPO_DIR}" fetch --all --prune
    git -C "${REPO_DIR}" reset --hard origin/main
    log "代码已更新到最新 commit: $(git -C "${REPO_DIR}" rev-parse --short HEAD)"
else
    log "首次克隆 ${REPO_URL} ..."
    git clone "${REPO_URL}" "${REPO_DIR}"
    log "克隆完成"
fi

# ---------- Step 2: 构建镜像 ----------
log "=== Step 2: 构建 Docker 镜像 ${FULL_IMAGE} ==="
docker build \
    --no-cache \
    --label "git.commit=$(git -C "${REPO_DIR}" rev-parse --short HEAD)" \
    --label "build.time=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    -t "${FULL_IMAGE}" \
    "${REPO_DIR}"
log "镜像构建完成"

# ---------- Step 3: 导出为 tar ----------
log "=== Step 3: 导出镜像为 tar ==="
log "目标文件: ${TAR_FILE}"
docker save -o "${TAR_FILE}" "${FULL_IMAGE}"
log "导出完成，文件大小: $(du -sh "${TAR_FILE}" | cut -f1)"

# ---------- 完成 ----------
log "=== 全部完成 ==="
log "镜像 tar 包路径: ${TAR_FILE}"
log ""
log "在目标服务器上使用以下命令导入镜像:"
log "  docker load -i ${TAR_FILE##*/}"
log "  docker run -d --name pan123-monitor --env-file .env ${FULL_IMAGE}"
