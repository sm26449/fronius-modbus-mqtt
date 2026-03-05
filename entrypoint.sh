#!/bin/bash
set -eo pipefail

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

    # Validate bucket/org names (alphanumeric, dash, underscore only)
    if ! echo "${INFLUXDB_BUCKET}" | grep -qE '^[a-zA-Z0-9_-]+$'; then
        echo "Error: INFLUXDB_BUCKET contains invalid characters: ${INFLUXDB_BUCKET}"
        echo "Only alphanumeric, dash, and underscore are allowed"
        exit 1
    fi
    if [ -n "${INFLUXDB_ORG}" ] && ! echo "${INFLUXDB_ORG}" | grep -qE '^[a-zA-Z0-9_-]+$'; then
        echo "Error: INFLUXDB_ORG contains invalid characters: ${INFLUXDB_ORG}"
        echo "Only alphanumeric, dash, and underscore are allowed"
        exit 1
    fi

    # Wait for InfluxDB to be available (max 30 seconds)
    MAX_RETRIES=30
    RETRY_COUNT=0

    while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${INFLUXDB_URL}/health" || true)
        if [ "$HTTP_CODE" = "200" ]; then
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
        # Use config file to avoid exposing token in process list
        CURL_AUTH_CONFIG=$(mktemp)
        chmod 600 "$CURL_AUTH_CONFIG"
        echo "header = \"Authorization: Token ${INFLUXDB_TOKEN}\"" > "$CURL_AUTH_CONFIG"

        # Check if bucket exists (look for "buckets":[ with content, not error message)
        BUCKET_RESPONSE=$(curl -s -K "$CURL_AUTH_CONFIG" \
            "${INFLUXDB_URL}/api/v2/buckets?name=${INFLUXDB_BUCKET}&org=${INFLUXDB_ORG}" || true)

        # Check if response contains empty buckets array or error
        BUCKET_EXISTS="1"
        if echo "$BUCKET_RESPONSE" | tr -d '\t\n ' | grep -q '"buckets":\[\]'; then
            BUCKET_EXISTS="0"
        elif echo "$BUCKET_RESPONSE" | grep -q '"code"'; then
            BUCKET_EXISTS="0"
        fi

        if [ "$BUCKET_EXISTS" = "0" ]; then
            echo "Creating InfluxDB bucket: ${INFLUXDB_BUCKET}"

            # Get org ID from bucket list (we have bucket permissions, not org permissions)
            ALL_BUCKETS=$(curl -s -K "$CURL_AUTH_CONFIG" \
                "${INFLUXDB_URL}/api/v2/buckets" || true)
            ORG_ID=$(echo "$ALL_BUCKETS" | tr -d '\t\n' | grep -o '"orgID": *"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')

            if [ -n "$ORG_ID" ]; then
                # Create bucket
                RESULT=$(curl -s -X POST "${INFLUXDB_URL}/api/v2/buckets" \
                    -K "$CURL_AUTH_CONFIG" \
                    -H "Content-Type: application/json" \
                    -d "{\"name\":\"${INFLUXDB_BUCKET}\",\"orgID\":\"${ORG_ID}\",\"retentionRules\":[]}" || true)

                # Check for success - look for bucket name in response
                if echo "$RESULT" | grep -q "\"name\": *\"${INFLUXDB_BUCKET}\""; then
                    echo "InfluxDB bucket '${INFLUXDB_BUCKET}' created successfully"
                else
                    echo "Warning: Could not create bucket - $RESULT"
                fi
            else
                echo "Warning: Could not find org ID for '${INFLUXDB_ORG}'"
            fi
        else
            echo "InfluxDB bucket '${INFLUXDB_BUCKET}' already exists"
        fi

        # Cleanup auth config
        rm -f "$CURL_AUTH_CONFIG"
    fi
else
    echo "InfluxDB not enabled or not configured, skipping bucket setup"
fi

# Fix ownership on mounted volumes (host volumes are owned by root)
chown fronius:fronius /app/data /app/logs 2>/dev/null || true

# Drop privileges and execute the main command as non-root user
exec gosu fronius "$@"
