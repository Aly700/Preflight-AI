FROM python:3.14-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-mcp.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-mcp.txt
COPY mcp_service/ ./mcp_service/
COPY preflight_core/ ./preflight_core/
USER 10001:10001
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=6 \
    CMD python -m mcp_service.healthcheck
CMD ["python", "-m", "uvicorn", "mcp_service.server:create_http_app", "--factory", "--host", "0.0.0.0", "--port", "8001", "--no-access-log", "--log-level", "warning"]
