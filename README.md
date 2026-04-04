# TacoClaw Server

Self-hosted dashboard, API gateway, and terminal bridge for the TacoClaw trading system.

Live deployment: `https://tacoclaw.v-carte.pro`

## Overview

This repository contains the public-facing stack that sits in front of the trading engines running on the Raspberry Pi. It is responsible for:

- serving the TacoClaw dashboard UI
- proxying dashboard API requests to the live backend
- bridging Server-Sent Events for real-time trade and skip activity
- exposing the browser terminal bridge backed by `ttyd`

The production backend currently used by the dashboard is the OpenClaw report service on `127.0.0.1:18791`, which reads live wallet state and trading history from the local trading workspace.

## Architecture

```text
trading engines + journal.db + wallet state
                 |
                 v
  report_webhook.py on :18791
  live REST + SSE backend
                 |
                 v
 dashboard/server.js on :8080
 static UI + API proxy + SSE bridge + ttyd upgrade proxy
                 |
                 v
     Cloudflare / public hostname
       tacoclaw.v-carte.pro
```

## Repository Layout

### `dashboard/`

Node/Express gateway for the public site.

Files:
- `server.js` reverse proxy and SSE bridge
- `index.html` dashboard UI
- `package.json` runtime dependencies

Responsibilities:
- serve the dashboard HTML locally from this repo
- proxy `/api/*` to the live backend on `127.0.0.1:18791`
- stream `/api/events/stream` to browsers without refresh
- proxy terminal websocket upgrades to local `ttyd`

### `api/`

Compatibility Flask shim for older local services.

It does not calculate trading state itself anymore. It simply proxies legacy API paths and SSE traffic to the live backend on `127.0.0.1:18791`, so older service units can continue working without keeping a second stale implementation in this repo.

### `terminal/`

Utility scripts for the mobile shell and remote terminal workflow.

## Live Dashboard

The dashboard includes:

- portfolio value, win rate, streak, and uptime cards
- open positions from the live trading state
- a real-time event feed driven by SSE
- animated engine activity with skip obstacles and event reactions
- browser terminal access through `ttyd`

## Runtime Dependencies

- Node.js 18+
- access to the live backend on `127.0.0.1:18791`
- optional `ttyd` on `127.0.0.1:7681`

## Local Run

```bash
git clone https://github.com/Abdallah-Tah/taco-server.git
cd taco-server/dashboard
npm install
node server.js
```

The dashboard will listen on `http://localhost:8080`.

## Environment

### `dashboard/server.js`

- `PORT`
  default: `8080`
- `LIVE_API`
  default: `http://127.0.0.1:18791`
- `UI_DIR`
  optional override for the static dashboard directory

## Notes

- This repo is intentionally separated from the broader OpenClaw workspace.
- Trading databases and runtime state are not tracked here.
- The live backend data source is expected to remain outside this repo.
