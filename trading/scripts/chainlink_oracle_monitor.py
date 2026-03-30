#!/usr/bin/env python3
import json, os, time
from pathlib import Path
import requests

CREDS_FILE = Path("/home/abdaltm86/.config/openclaw/secrets.env")
ENV = {}
if CREDS_FILE.exists():
    for line in CREDS_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k,v = line.split("=",1)
            ENV[k.strip()] = v.strip().strip("\"").strip("'")

CHAINLINK_MONITOR_ENABLED = os.environ.get("CHAINLINK_MONITOR_ENABLED", ENV.get("CHAINLINK_MONITOR_ENABLED", "false")).lower() == "true"
BTC_ORACLE = "0xc907E116054Ad103354f2D350FD2514433D57F6f"

def main():
    if not CHAINLINK_MONITOR_ENABLED:
        print("CHAINLINK_MONITOR_ENABLED=false")
        return
    print("Chainlink monitor scaffold active for", BTC_ORACLE)
    while True:
        try:
            print(json.dumps({"ts": int(time.time()), "oracle": BTC_ORACLE, "status": "scaffold"}))
        except Exception as e:
            print("error", e)
        time.sleep(5)

if __name__ == "__main__":
    main()
