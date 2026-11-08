# Official Python image pinned to the digest verified for this increment.
FROM python:3.14-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-chat.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-chat.txt
ENV COPILOT_CLI_EXTRACT_DIR=/opt/copilot-runtime COPILOT_SKIP_CLI_DOWNLOAD=1
# The SDK downloads the runtime for the build container's own architecture; link the matching
# prebuild instead of assuming x86-64. Unsupported machines fail the build with a clear message.
# GitHub release downloads sometimes time out (HTTP 504), so the download is retried with a backoff.
RUN for attempt in 1 2 3 4; do \
      COPILOT_SKIP_CLI_DOWNLOAD=0 python -m copilot download-runtime && break; \
      rm -rf /opt/copilot-runtime; \
      if [ "$attempt" = 4 ]; then echo "Copilot runtime download failed after 4 attempts" >&2; exit 1; fi; \
      echo "Copilot runtime download failed; retrying in $((attempt * 20))s" >&2; \
      sleep $((attempt * 20)); \
    done \
    && case "$(uname -m)" in \
         x86_64) platform=linux-x64 ;; \
         aarch64|arm64) platform=linux-arm64 ;; \
         *) echo "Unsupported build architecture: $(uname -m)" >&2; exit 1 ;; \
       esac \
    && test -x "/opt/copilot-runtime/prebuilds/${platform}/copilot-runtime" \
    && chmod -R a+rX /opt/copilot-runtime \
    && ln -s "/opt/copilot-runtime/prebuilds/${platform}/copilot-runtime" /usr/local/bin/preflight-copilot
ENV COPILOT_CLI_PATH=/usr/local/bin/preflight-copilot
COPY backend/ ./backend/
COPY preflight_core/ ./preflight_core/
USER 10001:10001
RUN test -x /usr/local/bin/preflight-copilot
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=6 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2).read()"
CMD ["python", "-m", "uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
