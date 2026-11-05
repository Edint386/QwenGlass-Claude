# glassclaude 云端服务的镜像。构建上下文是仓库根目录（compose.yaml 里的 context: ..）。
FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.11.32

WORKDIR /app
# 依赖单独一层，只改代码时不重装。字节码在这里编译好：容器根文件系统只读，运行时写不了 .pyc
ENV UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY glassclaude ./glassclaude
RUN uv sync --frozen --no-dev --no-editable

USER 10001:10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8790
CMD ["/app/.venv/bin/glassclaude", "serve", "--host", "0.0.0.0"]
