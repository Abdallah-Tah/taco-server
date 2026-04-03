#!/bin/bash
# Export BTC and ETH logs as JSON and email them

cd /home/abdaltm86/.openclaw/workspace/trading

# Convert BTC log to JSON
python3 << 'EOF'
import json
import re
from datetime import datetime

events = []
try:
    with open('/tmp/polymarket_btc15m.log', 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # Try different patterns
            match = re.match(r'\[.*?\] \[(\w+)\] (.+)', line)
            if match:
                level, msg = match.groups()
                events.append({'level': level, 'message': msg, 'engine': 'btc15m', 'raw': line})
            else:
                # Capture raw if pattern doesn't match
                events.append({'raw': line, 'engine': 'btc15m'})
except Exception as e:
    events = [{'error': str(e), 'engine': 'btc15m'}]

with open('btc_logs.json', 'w') as f:
    json.dump(events, f, indent=2)

print(f'Exported {len(events)} BTC events')
EOF

# Convert ETH log to JSON
python3 << 'EOF'
import json
import re
from datetime import datetime

events = []
try:
    with open('/tmp/polymarket_eth15m.log', 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            match = re.match(r'\[.*?\] \[(\w+)\] (.+)', line)
            if match:
                level, msg = match.groups()
                events.append({'level': level, 'message': msg, 'engine': 'eth15m', 'raw': line})
            else:
                events.append({'raw': line, 'engine': 'eth15m'})
except Exception as e:
    events = [{'error': str(e), 'engine': 'eth15m'}]

with open('eth_logs.json', 'w') as f:
    json.dump(events, f, indent=2)

print(f'Exported {len(events)} ETH events')
EOF

# Email them
(
  echo "Subject: BTC Engine Logs JSON"
  echo ""
  cat btc_logs.json
) | msmtp abdallahtmohamed86@gmail.com

(
  echo "Subject: ETH Engine Logs JSON"
  echo ""
  cat eth_logs.json
) | msmtp abdallahtmohamed86@gmail.com

echo "Done! Logs emailed to abdallahtmohamed86@gmail.com"
