# Local web entry point for the Compose stack: build the UI, then serve it with server/local.mjs.
# Official Node image pinned to the multi-architecture digest recorded for this increment.
FROM node:24-bookworm-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY tsconfig.json vite.config.js index.html ./
COPY public/ ./public/
COPY src/ ./src/
RUN npm run build

FROM node:24-bookworm-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9
ENV NODE_ENV=production PORT=8080 HOST=0.0.0.0 STATIC_DIR=/app/dist
WORKDIR /app
COPY --from=build /app/dist ./dist
COPY package.json worker.mjs ./
COPY server/ ./server/
USER node
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=6 \
    CMD node -e "fetch('http://127.0.0.1:8080/').then(r => process.exit(r.status === 200 ? 0 : 1), () => process.exit(1))"
CMD ["node", "server/local.mjs"]
