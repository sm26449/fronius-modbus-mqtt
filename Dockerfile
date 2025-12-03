FROM python:3.11-slim

LABEL maintainer="Stefan M <sm26449@diysolar.ro>"
LABEL version="1.2.0"
LABEL description="Fronius Modbus TCP to MQTT/InfluxDB Bridge"

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY fronius_modbus_mqtt.py .
COPY healthcheck.py .
COPY fronius/ ./fronius/
COPY config/ ./config/

# Create data directory for cache
RUN mkdir -p /app/data /app/logs

# Healthcheck
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python /app/healthcheck.py || exit 1

# Default command
CMD ["python", "fronius_modbus_mqtt.py"]
