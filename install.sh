#!/usr/bin/env bash
set -euo pipefail

echo "=== Installing OS Dependencies ==="
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip poppler-utils curl git

# Install Node.js LTS if not present
if ! command -v node &> /dev/null; then
    echo "=== Installing Node.js LTS ==="
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi

echo "=== Initializing Python Virtual Environment ==="
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r backend/requirements.txt

echo "=== Building Vue Production Bundle ==="
cd frontend
npm ci
npm run build
cd ..

echo "=== Setting Permissions & Exports Directory ==="
mkdir -p backend/exports/archive
chmod -R 775 backend/exports

echo "=== Installing Systemd Service ==="
if [ -f "facturigui.service" ]; then
    sudo cp facturigui.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable facturigui
    sudo systemctl restart facturigui
    echo "FacturiGUI is running at http://$(hostname -I | awk '{print $1}'):8000"
fi