# 基础镜像与软件源可通过 --build-arg 覆盖:
#   默认(国内构建): 华为云基础镜像 + 华为云 apt/pip 源 (docker compose / build_and_export.sh 直接用)
#   CI(GitHub Actions): --build-arg BASE_IMAGE=python:3.12-slim --build-arg CN_MIRROR=0 (官方源, 国际 runner 快)
ARG BASE_IMAGE=swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/library/python:3.12-slim
FROM ${BASE_IMAGE}

LABEL maintainer="1696363859@qq.com"

WORKDIR /app

# CN_MIRROR=1 (默认, 国内): apt 换华为云源; CN_MIRROR=0 (CI): 用官方 debian 源
ARG CN_MIRROR=1
RUN if [ "$CN_MIRROR" = "1" ]; then \
        sed -i 's|deb.debian.org|mirrors.huaweicloud.com|g' /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends \
        cifs-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN if [ "$CN_MIRROR" = "1" ]; then \
        pip install --no-cache-dir -i https://mirrors.huaweicloud.com/repository/pypi/simple \
            --trusted-host mirrors.huaweicloud.com -r requirements.txt; \
    else \
        pip install --no-cache-dir -r requirements.txt; \
    fi

COPY . .

ENV PYTHONUNBUFFERED=1

CMD ["python", "monitor.py"]
