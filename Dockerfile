# ─── Stage 1: Build frontend ─────────────────────────────
FROM node:20-slim AS frontend-builder

WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./
# Use relative URL — frontend served from same origin as API
ENV VITE_API_URL=/api
RUN npm run build

# ─── Stage 2: Python backend + static frontend ───────────
FROM python:3.12-slim

WORKDIR /app

# System deps for lxml/pdfplumber
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libxml2-dev \
    libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

# Python deps (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY api.py .
COPY functions/ ./functions/
COPY templates/ ./templates/

# Frontend built assets from stage 1
COPY --from=frontend-builder /frontend/dist ./frontend_dist

# Allow `import config` from functions/ (same as Firebase Functions runtime)
ENV PYTHONPATH=/app/functions:/app

# Cloud Run injects PORT env var
ENV PORT=8080
EXPOSE 8080

# Use exec form so SIGTERM is forwarded
CMD exec uvicorn api:app --host 0.0.0.0 --port ${PORT}
