FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app

# Install system dependencies
# xvfb: virtual display for non-headless Chromium (needed for CAPTCHA rendering)
RUN apt-get update && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Cloud Run requires the app to listen on $PORT (default 8080)
ENV PORT=8080

EXPOSE 8080

CMD ["sh", "-c", "python web_monitor_menu.py --port ${PORT}"]
