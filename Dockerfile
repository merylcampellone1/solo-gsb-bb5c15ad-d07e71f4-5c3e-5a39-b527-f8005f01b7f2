FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/names.db \
    PORT=8000

WORKDIR /srv/app

# 先装依赖，利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 非 root 运行；/data 存放 SQLite 数据库（可挂卷持久化）
RUN useradd --system --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data
USER appuser

EXPOSE 8000

# 启动命令：先执行版本化数据库迁移（幂等，多实例并发启动安全；
# 迁移被改写 / 版本缺口时以非零码退出，API 不会启动），成功后才启动 API
CMD ["sh", "-c", "python -m app.database migrate && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
