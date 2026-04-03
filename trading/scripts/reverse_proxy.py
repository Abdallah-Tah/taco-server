#!/usr/bin/env python3
"""Reverse proxy to serve OpenClaw dashboard + trading API on same port."""
import http.server
import socketserver
import urllib.request
import urllib.error
import threading
import json

OPENCLAW_PORT = 18790
WEBHOOK_PORT = 18791
PROXY_PORT = 18792

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # Route report/webhook endpoints to the webhook service.
        if self.path.startswith('/api/'):
            self._proxy_to(WEBHOOK_PORT)
        else:
            self._proxy_to(OPENCLAW_PORT)
    
    def do_POST(self):
        if self.path.startswith('/api/') or self.path == '/webhook':
            self._proxy_to(WEBHOOK_PORT, method='POST')
        else:
            self._proxy_to(OPENCLAW_PORT, method='POST')
    
    def _proxy_to(self, port, method='GET'):
        try:
            url = f"http://127.0.0.1:{port}{self.path}"
            
            if method == 'POST':
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length) if content_length > 0 else None
                req = urllib.request.Request(url, data=body, method='POST')
                for header in self.headers:
                    if header.lower() not in ('content-length', 'host'):
                        req.add_header(header, self.headers[header])
            else:
                req = urllib.request.Request(url, method='GET')
            
            with urllib.request.urlopen(req, timeout=30) as response:
                self.send_response(response.status)
                for header, value in response.getheaders():
                    if header.lower() not in ('transfer-encoding', 'content-length'):
                        self.send_header(header, value)
                self.end_headers()
                self.wfile.write(response.read())
                
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(f"Proxy error: {e}".encode())
    
    def log_message(self, format, *args):
        pass  # Suppress logging

def run():
    with socketserver.ThreadingTCPServer(('0.0.0.0', PROXY_PORT), ProxyHandler) as httpd:
        print(f"Reverse proxy running on port {PROXY_PORT}")
        print(f"  /api/*  -> port {WEBHOOK_PORT}")
        print(f"  /*      -> port {OPENCLAW_PORT}")
        httpd.serve_forever()

if __name__ == '__main__':
    run()
