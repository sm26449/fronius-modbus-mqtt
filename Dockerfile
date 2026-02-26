FROM python:3.11-slim

LABEL maintainer="Stefan M <sm26449@diysolar.ro>"
LABEL version="1.4.0"
LABEL description="Fronius Modbus TCP to MQTT/InfluxDB Bridge"

WORKDIR /app

# Install system dependencies (curl for InfluxDB bucket creation, gosu for privilege drop)
RUN apt-get update && apt-get install -y --no-install-recommends curl gosu \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY fronius_modbus_mqtt.py .
COPY healthcheck.py .
COPY fronius/ ./fronius/

# Copy config to default location (will be copied to /app/config on first run if empty)
COPY config/ ./config.default/

# Create non-root user
RUN groupadd -r fronius && useradd -r -g fronius fronius

# Create directories for runtime and set ownership
RUN mkdir -p /app/config /app/data /app/logs \
    && chown -R fronius:fronius /app

# Copy and setup entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Healthcheck
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python /app/healthcheck.py || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "fronius_modbus_mqtt.py"]
