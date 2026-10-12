#!/usr/bin/env bash
set -euo pipefail

echo "=== Pulling Latest Changes ==="
git pull origin main

echo "=== Updating Backend Dependencies ==="
./venv/bin/pip install -r backend/requirements.txt

echo "=== Rebuilding Frontend Assets ==="
cd frontend
npm ci
npm run build
cd ..

echo "=== Restarting Application Daemon ==="
sudo systemctl restart facturigui
sudo systemctl status facturigui --no-pager

echo "=== Deployment Complete ==="