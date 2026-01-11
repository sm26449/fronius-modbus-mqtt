#!/bin/bash
set -e

# Initialize config from defaults if mounted volume is empty
if [ -z "$(ls -A /app/config 2>/dev/null)" ]; then
    echo "Config directory is empty, initializing from defaults..."
    cp -r /app/config.default/* /app/config/
    echo "Config initialized successfully"
else
    echo "Config directory already contains files, skipping initialization"
fi

# Create InfluxDB bucket if enabled and doesn't exist
if [ "${INFLUXDB_ENABLED}" = "true" ] && [ -n "${INFLUXDB_URL}" ] && [ -n "${INFLUXDB_TOKEN}" ]; then
    echo "InfluxDB is enabled, checking bucket..."

    # Wait for InfluxDB to be available (max 30 seconds)
    MAX_RETRIES=30
    RETRY_COUNT=0

    while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
        if curl -s -o /dev/null -w "%{http_code}" "${INFLUXDB_URL}/health" | grep -q "200"; then
            echo "InfluxDB is available"
            break
        fi
        RETRY_COUNT=$((RETRY_COUNT + 1))
        echo "Waiting for InfluxDB... ($RETRY_COUNT/$MAX_RETRIES)"
        sleep 1
    done

    if [ $RETRY_COUNT -ge $MAX_RETRIES ]; then
        echo "Warning: InfluxDB not available after $MAX_RETRIES seconds, skipping bucket creation"
    else
        # Check if bucket exists (look for "buckets":[ with content, not error message)
        BUCKET_RESPONSE=$(curl -s -H "Authorization: Token ${INFLUXDB_TOKEN}" \
            "${INFLUXDB_URL}/api/v2/buckets?name=${INFLUXDB_BUCKET}&org=${INFLUXDB_ORG}")

        # Check if response contains "not found" error (handle multiline JSON)
        if echo "$BUCKET_RESPONSE" | grep -q 'not found'; then
            BUCKET_EXISTS="0"
        else
            BUCKET_EXISTS="1"
        fi

        if [ "$BUCKET_EXISTS" = "0" ]; then
            echo "Creating InfluxDB bucket: ${INFLUXDB_BUCKET}"

            # Get org ID from bucket list (we have bucket permissions, not org permissions)
            # Query all buckets and extract orgID from any existing bucket
            ALL_BUCKETS=$(curl -s -H "Authorization: Token ${INFLUXDB_TOKEN}" \
                "${INFLUXDB_URL}/api/v2/buckets" | tr -d '\t\n')
            ORG_ID=$(echo "$ALL_BUCKETS" | grep -o '"orgID": *"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')

            if [ -n "$ORG_ID" ]; then
                # Create bucket
                RESULT=$(curl -s -X POST "${INFLUXDB_URL}/api/v2/buckets" \
                    -H "Authorization: Token ${INFLUXDB_TOKEN}" \
                    -H "Content-Type: application/json" \
                    -d "{\"name\":\"${INFLUXDB_BUCKET}\",\"orgID\":\"${ORG_ID}\",\"retentionRules\":[]}")

                # Check for success - look for bucket name in response (handles spaces in JSON)
                if echo "$RESULT" | grep -q "\"name\": *\"${INFLUXDB_BUCKET}\""; then
                    echo "✓ InfluxDB bucket '${INFLUXDB_BUCKET}' created successfully"
                else
                    echo "Warning: Could not create bucket - $RESULT"
                fi
            else
                echo "Warning: Could not find org ID for '${INFLUXDB_ORG}'"
            fi
        else
            echo "InfluxDB bucket '${INFLUXDB_BUCKET}' already exists"
        fi
    fi
else
    echo "InfluxDB not enabled or not configured, skipping bucket setup"
fi

# Execute the main command
exec "$@"
