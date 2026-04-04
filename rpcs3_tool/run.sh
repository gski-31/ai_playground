#!/bin/bash
echo "Installing dependencies..."
pip install -r requirements.txt -q
echo
echo "Starting RPCS3 Game Export Tool..."
python app.py "$@"
