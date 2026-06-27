FROM python:3.11-slim

WORKDIR /app

# Base tools needed before playwright install-deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements_server.txt .
RUN pip install --no-cache-dir -r requirements_server.txt

# Install Chromium system deps then the browser binary
RUN playwright install-deps chromium \
    && playwright install chromium

COPY . .

# Runtime data written here — mount as volumes
RUN mkdir -p /app/logs /app/history

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
