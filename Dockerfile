FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create data directory for persistent storage
RUN mkdir -p /app/data

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import httpx; r = httpx.get('http://localhost:8000/api/health'); r.raise_for_status()" || exit 1

# Run server
CMD ["python", "-u", "api_server.py"]
