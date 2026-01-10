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

# Execute the main command
exec "$@"
