FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app

# Create data directory for SQLite and debug files
RUN mkdir -p /data

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py db.py scraper.py notifier.py main.py ./
COPY templates/ templates/
COPY static/ static/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Environment defaults
ENV DB_PATH=/data/price_alerts.db
ENV DATA_DIR=/data
ENV PORT=5001
ENV FLASK_DEBUG=0

EXPOSE 5001

ENTRYPOINT ["./entrypoint.sh"]
