# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Install system dependencies needed for some Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Install only runtime dependencies (curl for healthcheck, optional)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user for security
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application source
COPY . .

# Create storage directories and assign ownership
RUN mkdir -p storage/chunks storage/completed \
 && chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

EXPOSE 8000

# Behind a platform load balancer (Render, Azure, Fly, ...) every request comes
# from the proxy's IP. Trust its X-Forwarded-For so the per-IP rate limiter sees
# real client IPs. "*" is appropriate when the container is only reachable
# through the platform proxy; set FORWARDED_ALLOW_IPS to the proxy's address
# if the container is also exposed directly.
ENV FORWARDED_ALLOW_IPS="*"

# Healthcheck - uses curl (more reliable than Python for this)
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:${PORT:-8000}/health || exit 1

# Hosting platforms inject PORT; default to 8000 locally. `exec` keeps uvicorn
# as PID 1 so it receives shutdown signals.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]