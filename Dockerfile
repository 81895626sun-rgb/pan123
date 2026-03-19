FROM python:3.11-slim

LABEL maintainer="1696363859@qq.com"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    cifs-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

CMD ["python", "monitor.py"]
