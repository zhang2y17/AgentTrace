# AgentTrace API 镜像。
#
# 契约 T1/T8：Python 3.12-slim，Docker Compose 启动 API + PostgreSQL + Redis。
# 安全要求（ARCHITECTURE §7 / ACCEPTANCE_CHECKLIST B-06/B-07）：
# - 非 root 用户运行；
# - .dockerignore 排除 .env，密钥不进入镜像层；
# - 不含任何密钥的构建参数或 ENV 默认值。

FROM python:3.12-slim

# Python 运行时行为：
# - PYTHONDONTWRITEBYTECODE：不生成 .pyc，保持镜像干净
# - PYTHONUNBUFFERED：日志实时输出，便于 docker logs 观察结构化 JSON 日志
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 系统依赖保持最小化以减小攻击面。健康检查使用 python stdlib 的
# urllib，因此不需要 curl/wget。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖，利用层缓存：只要 requirements.txt 不变就不重装
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再拷贝应用代码与运行所需数据。
# 注意：不拷 tests/ 与 docs/（.dockerignore 已排除），镜像更小。
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY data/ ./data/
COPY pyproject.toml README.md ./

# 创建非 root 用户并移交目录所有权。
# /app/data 需要可写（SQLite 回退模式与本地缓存场景）。
RUN groupadd --system --gid 1001 agenttrace \
    && useradd --system --uid 1001 --gid agenttrace --create-home agenttrace \
    && chown -R agenttrace:agenttrace /app
USER agenttrace

EXPOSE 8000

# 默认命令：建表 + 播种样例数据 + 启动服务。
# 说明：两个脚本都是幂等的，可安全重复执行；生产场景不应每次启动都播种，
# 应改为只跑 init_db.py（见 docker-compose.yml 的注释）。
CMD ["sh", "-c", "python scripts/init_db.py --yes && python scripts/seed_data.py && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
