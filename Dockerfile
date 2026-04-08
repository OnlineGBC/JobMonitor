FROM mcr.microsoft.com/playwright/python:v1.51.0-noble

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium for Playwright
RUN playwright install chromium

# Copy application code
COPY . .

# Cloud Run requires the app to listen on $PORT (default 8080)
ENV PORT=8080

EXPOSE 8080

CMD ["sh", "-c", "python web_monitor_menu.py --port ${PORT}"]
