#!/bin/bash
# Start the Bot Tracking Dashboard

cd /home/abdaltm86/.openclaw/workspace/trading

# Kill existing dashboard if running
pkill -f "dashboard_server.py" 2>/dev/null
sleep 1

nohup python3 dashboard_server.py --host 0.0.0.0 --port 8080 >> dashboard.log 2>&1 &
DASHBOARD_PID=$!

echo "Dashboard started on port 8080 (PID: $DASHBOARD_PID)"

# Get IP address
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -z "$IP" ]; then
    IP="127.0.0.1"
fi

echo "Access from: http://${IP}:8080"
echo "Or: http://localhost:8080"
