# ── Job Matching AI Agent — Backend API Dockerfile ─────────────────────
#
# Build:
#   docker build -t job-matching-agent .
#
# Run (production — inject env vars, NOT .env):
#   docker run -d --name job-matching-agent \
#     -e DASHSCOPE_API_KEY=sk-xxxx \
#     -e LLM_MODEL=qwen3.5-plus \
#     -e API_KEY=my-secret \
#     -e REDIS_URL=redis://redis:6379/0 \
#     -p 8000:8000 \
#     job-matching-agent
#
# Run (development):
#   docker run -d --name job-matching-agent --env-file .env -p 8000:8000 job-matching-agent
#
# Deploy: Railway / Render / Fly.io — set env vars in the dashboard.
#         NEVER commit .env with real secrets.
#
# Model switching at runtime:
#   Set LLM_MODEL=deepseek-chat + LLM_BASE_URL/LLM_API_KEY,
#   then POST /config/reload to apply without restart.
#
# ───────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

WORKDIR /app

# Install system deps (Playwright browser + basic tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (for dynamic SPA crawling)
RUN playwright install chromium --with-deps 2>/dev/null || true

# Copy application code
COPY . .

# Create data directories
RUN mkdir -p app/data/outputs

# Expose FastAPI port
EXPOSE 8000

# Run with uvicorn
CMD ["uvicorn", "app.api.server:app", "--host", "0.0.0.0", "--port", "8000"]
