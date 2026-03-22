#!/usr/bin/env python3
"""
report_webhook.py - Handle /short-report and /long-report commands.

This script can operate in two modes:
1. Webhook server mode: Listen for HTTP requests from Telegram
2. Direct execution mode: Generate and send reports directly

Usage:
  # Direct execution (called by agent or cron)
  python scripts/report_webhook.py --command short --target <chat_id>

  # Webhook server mode (listen for Telegram requests)
  python scripts/report_webhook.py --server --port 18791
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

ROOT = Path.home() / ".openclaw" / "workspace" / "trading"
VENV_PY = ROOT / ".polymarket-venv" / "bin" / "python3"
REPORT_SCRIPT = ROOT / "scripts" / "trading_reports.py"

# Telegram command handlers
COMMAND_MAP = {
    "/short-report": ("short", "Short trading report"),
    "/long-report": ("long", "Detailed trading report"),
    "/report": ("both", "Full trading report"),
}


def send_to_telegram(message, chat_id):
    """Send a message to Telegram via openclaw."""
    try:
        # Split long messages into chunks
        if len(message) > 4000:
            chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
        else:
            chunks = [message]

        for chunk in chunks:
            result = subprocess.run([
                "openclaw", "message", "send",
                "--target", str(chat_id),
                "--message", chunk
            ], capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                print(f"Error sending chunk: {result.stderr}")
                return False
        return True
    except Exception as e:
        print(f"Failed to send message: {e}")
        return False


def generate_report(command):
    """Generate the report based on command type."""
    cmd = [str(VENV_PY), str(REPORT_SCRIPT), f"--{command}"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    # For long reports, the report is saved to file and not output to stdout
    if command == "long" or command == "both":
        # Read from the most recent report file
        import glob
        pattern = str(ROOT / "reports" / "*-trading-report.md")
        files = sorted(glob.glob(pattern), reverse=True)
        if files:
            return Path(files[0]).read_text()
        return "No report file found."

    # For short reports, extract from stdout
    lines = result.stdout.strip().split('\n')
    report_lines = []

    for line in lines:
        if line.startswith("=== SHORT REPORT") or line.startswith("=== LONG REPORT"):
            continue
        if line.startswith("[Length:") or line.startswith("[WARNING:"):
            continue
        report_lines.append(line)

    return "\n".join(report_lines).strip()


def handle_command(command_name, chat_id, dry_run=False):
    """Handle a Telegram command and send report."""
    # command_name could be "/short-report" or "short"
    # Map both to the report type
    if command_name in COMMAND_MAP:
        report_type, description = COMMAND_MAP[command_name]
    elif command_name in ('short', 'long', 'both'):
        # Convert "short" to "/short-report" for display
        report_type = command_name
        description = COMMAND_MAP.get(f'/{command_name}-report', (None, "Report"))[1]
        command_name = f'/{command_name}-report'
    else:
        return {"error": f"Unknown command: {command_name}"}

    report = generate_report(report_type)

    if dry_run:
        return {"status": "dry-run", "report": report, "description": description}

    success = send_to_telegram(report, chat_id)
    return {
        "status": "sent" if success else "failed",
        "command": command_name,
        "chat_id": chat_id,
        "description": description
    }


class ReportWebhookHandler(BaseHTTPRequestHandler):
    """HTTP handler for report webhook requests."""

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

    def do_POST(self):
        """Handle POST requests from Telegram."""
        if self.path == "/webhook":
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')

                # Parse Telegram update
                update = json.loads(body)

                # Extract message and command
                chat_id = None
                command = None

                if 'message' in update and 'text' in update['message']:
                    text = update['message']['text']
                    chat_id = update['message']['chat']['id']

                    # Check for our commands
                    for cmd_name, (cmd_type, desc) in COMMAND_MAP.items():
                        if text.startswith(cmd_name):
                            command = cmd_name
                            break

                if chat_id and command:
                    result = handle_command(command, chat_id)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps(result).encode())
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "ignored"}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        """Handle GET requests (health check)."""
        if self.path == "/health":
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        else:
            self.send_response(404)
            self.end_headers()


def run_server(port=18791):
    """Run the webhook server."""
    server_address = ('', port)
    httpd = HTTPServer(server_address, ReportWebhookHandler)
    print(f"Report webhook server running on port {port}")
    print(f"Commands: {list(COMMAND_MAP.keys())}")
    httpd.serve_forever()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Report webhook handler")
    parser.add_argument('--command',
                        choices=['short', 'long', 'both'],
                        help="Report type to generate (direct mode)")
    parser.add_argument('--target', help="Telegram chat ID (direct mode)")
    parser.add_argument('--server', action='store_true',
                        help="Run as webhook server")
    parser.add_argument('--port', type=int, default=18791,
                        help="Port for webhook server (default: 18791)")
    parser.add_argument('--dry-run', action='store_true',
                        help="Print to stdout instead of sending")

    args = parser.parse_args()

    if args.server:
        run_server(args.port)
    elif args.command and args.target:
        result = handle_command(args.command, args.target, args.dry_run)
        if args.dry_run:
            print("=== DRY RUN ===")
            print(result.get('report', ''))
        else:
            print(f"Command: {result.get('command', args.command)}")
            print(f"Status: {result.get('status')}")
        return 0 if result.get('status') in ('sent', 'dry-run') else 1
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
