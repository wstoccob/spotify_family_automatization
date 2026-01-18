#!/bin/bash

# Stop the current bot safely
echo "🛑 Stopping current bot..."
docker-compose down

# Pull the latest code
echo "⬇️ Pulling latest code..."
git pull

# Rebuild and start
echo "🚀 Rebuilding and starting..."
docker-compose up -d --build

# Show logs
echo "📋 Showing logs (Ctrl+C to exit)..."
docker-compose logs -f
