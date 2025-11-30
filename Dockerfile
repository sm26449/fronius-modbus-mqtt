FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY fronius_modbus_mqtt.py .
COPY fronius/ ./fronius/
COPY config/ ./config/

# Create data directory for cache
RUN mkdir -p /app/data

# Default command
CMD ["python", "fronius_modbus_mqtt.py"]
