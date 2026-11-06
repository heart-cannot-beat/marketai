FROM node:22-slim AS build-base
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .

FROM build-base AS test
RUN npm test -- --runInBand

FROM build-base AS build
RUN npm run build \
    && npm prune --omit=dev

FROM node:22-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends wget \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app/package.json /app/package-lock.json ./
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
HEALTHCHECK CMD wget -q -O /dev/null http://localhost:${PORT:-9090}/test/health || exit 1
CMD ["node", "dist/server.js"]
