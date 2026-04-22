# ─── Base image ──────────────────────────────────────────
FROM python:3.12-slim

# ─── Working directory ───────────────────────────────────
WORKDIR /app

# ─── System deps for lxml/pdfplumber ─────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libxml2-dev \
    libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

# ─── Python deps (cached layer) ──────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ─── App code ────────────────────────────────────────────
COPY api.py .
COPY functions/ ./functions/
COPY templates/ ./templates/

# Allow `import config` from functions/ (same as Firebase Functions runtime)
ENV PYTHONPATH=/app/functions:/app

# ─── Cloud Run injects PORT env var ──────────────────────
ENV PORT=8080
EXPOSE 8080

# ─── Use exec form so SIGTERM is forwarded ───────────────
CMD exec uvicorn api:app --host 0.0.0.0 --port ${PORT}
