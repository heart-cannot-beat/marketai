FROM mcr.microsoft.com/playwright:v1.62.1-noble

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/marketai-venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN python3 -m venv /opt/marketai-venv \
    && pip install --no-cache-dir -r requirements.txt

COPY web/package.json web/package-lock.json ./web/
WORKDIR /app/web
RUN npm ci

COPY . /app

# Prisma generation is offline but still needs a syntactically valid URL.
ENV DATABASE_URL=postgresql://build:build@localhost:5432/build
RUN npx prisma generate

CMD ["npx", "playwright", "test", "--config=e2e/golden-path/playwright.config.golden.ts"]
