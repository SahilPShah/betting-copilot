# syntax=docker/dockerfile:1

##############################################################
# Stage 1: Builder
# Installs all Python dependencies into /install so we can
# copy only compiled packages into the final image.
##############################################################
FROM python:3.12-slim AS builder

# Build deps for C extensions: psycopg2, scipy, lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        libpq-dev \
        libxml2-dev \
        libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt .

RUN pip install --upgrade pip \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt


##############################################################
# Stage 2: Runtime image
# Copies compiled packages from builder — no compilers needed.
##############################################################
FROM python:3.12-slim AS runtime

# Runtime-only system libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libxml2 \
        libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

# Non-root user for security
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

COPY . .

RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# CMD bypasses the `if __name__ == "__main__": reload=True` block in api/main.py
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--access-log"]
