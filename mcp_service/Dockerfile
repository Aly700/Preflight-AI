FROM python:3.14-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-mcp.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-mcp.txt
# BAAI/bge-small-en-v1.5, MIT. Download only at build time, from one immutable revision.
# Match the API image's four attempts with increasing 20-second backoff.
ENV PREFLIGHT_EMBEDDING_MODEL_DIR=/opt/models/bge-small-en-v1.5
RUN mkdir -p /opt/models/bge-small-en-v1.5/onnx \
    && for file in onnx/model.onnx tokenizer.json config.json; do \
      for attempt in 1 2 3 4; do \
        python -c 'import shutil, sys, urllib.request; source = urllib.request.urlopen(sys.argv[1], timeout=120); target = open(sys.argv[2], "wb"); shutil.copyfileobj(source, target); target.close(); source.close()' \
          "https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/5c38ec7c405ec4b44b94cc5a9bb96e735b38267a/$file" \
          "/opt/models/bge-small-en-v1.5/$file" && break; \
        rm -f "/opt/models/bge-small-en-v1.5/$file"; \
        if [ "$attempt" = 4 ]; then echo "Embedding model download failed after 4 attempts" >&2; exit 1; fi; \
        echo "Embedding model download failed; retrying in $((attempt * 20))s" >&2; \
        sleep $((attempt * 20)); \
      done; \
    done \
    && printf '%s\n' \
      '828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35  /opt/models/bge-small-en-v1.5/onnx/model.onnx' \
      'd241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66  /opt/models/bge-small-en-v1.5/tokenizer.json' \
      '094f8e891b932f2000c92cfc663bac4c62069f5d8af5b5278c4306aef3084750  /opt/models/bge-small-en-v1.5/config.json' \
      | sha256sum -c - \
    && chmod -R a+rX /opt/models/bge-small-en-v1.5
COPY mcp_service/ ./mcp_service/
COPY preflight_core/ ./preflight_core/
USER 10001:10001
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=6 \
    CMD python -m mcp_service.healthcheck
CMD ["python", "-m", "uvicorn", "mcp_service.server:create_http_app", "--factory", "--host", "0.0.0.0", "--port", "8001", "--no-access-log", "--log-level", "warning"]
