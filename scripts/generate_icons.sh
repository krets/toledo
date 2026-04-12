#!/bin/bash
# Generate app icons and favicons from master icon
MASTER="resources/master_icon.png"
STATIC="static"

if [ ! -f "$MASTER" ]; then
    echo "Error: $MASTER not found."
    exit 1
fi

echo "Generating icons from $MASTER..."

# 1. PWA Icons
convert "$MASTER" -resize 192x192 "$STATIC/icon-192.png"
convert "$MASTER" -resize 512x512 "$STATIC/icon-512.png"

# 2. Apple Touch Icon
convert "$MASTER" -resize 180x180 "$STATIC/apple-touch-icon.png"

# 3. Favicon (Multi-size bundle)
convert "$MASTER" -define icon:auto-resize=16,32,48 "$STATIC/favicon.ico"

echo "Done! Icons generated in $STATIC/"
