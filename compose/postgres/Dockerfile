# The same PostgreSQL 17 image as before (Debian trixie, glibc 2.41), so an existing data
# volume keeps its collation version; pgvector comes from the PostgreSQL project's signed
# Debian repository that the image already trusts, pinned to one package version.
FROM postgres:17@sha256:ff87138949ad9a46f32ff23bd6852b52ce024a13dffe9b4087be98292cdd3b4f
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-17-pgvector=0.8.6-1.pgdg13+2 \
    && rm -rf /var/lib/apt/lists/*
