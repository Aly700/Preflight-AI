# Keep the base identical to backend/Dockerfile; ingestion is a separate image.
FROM python:3.14-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home ingest
COPY requirements-ingest.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-ingest.txt
COPY scripts/render_scans.py scripts/ocr_scans.py ./scripts/
COPY assets/fonts/ ./assets/fonts/
USER 10001:10001
ENTRYPOINT ["python", "-B"]
CMD ["scripts/render_scans.py", "--help"]
