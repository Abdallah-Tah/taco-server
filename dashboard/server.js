import express from "express";
import http from "http";
import { Readable } from "stream";
import path from "path";
import { fileURLToPath } from "url";
import { createProxyMiddleware } from "http-proxy-middleware";
import httpProxy from "http-proxy";
import facesRouter from "./routes/faces.js";

const PORT = process.env.PORT || 8080;
const DASHBOARD_DIR = path.dirname(fileURLToPath(import.meta.url));
const UI_DIR = process.env.UI_DIR || path.join(DASHBOARD_DIR, "frontend", "dist");
const LIVE_API = process.env.LIVE_API || "http://127.0.0.1:18791";
const TTYD_URL = process.env.TTYD_URL || "http://127.0.0.1:7681";
const NOORHAFIZ_URL = process.env.NOORHAFIZ_URL || "http://127.0.0.1:3000";
const NOORHAFIZ_API_URL = process.env.NOORHAFIZ_API_URL || "http://127.0.0.1:3001";
const NOORHAFIZ_STATIC_DIR = process.env.NOORHAFIZ_STATIC_DIR || path.join(path.dirname(path.dirname(DASHBOARD_DIR)), "noorhafiz", "app", "dist");
const app = express();

function proxyTtyWebSocket(req, socket, head) {
  const upstream = http.request({
    protocol: "http:",
    host: "127.0.0.1",
    port: 7681,
    method: "GET",
    path: req.url || "/",
    headers: {
      ...req.headers,
      host: "127.0.0.1:7681",
      connection: "Upgrade",
      upgrade: "websocket",
      "x-forwarded-host": req.headers.host || "",
      "x-forwarded-proto": "https",
    },
  });

  upstream.on("upgrade", (upstreamRes, upstreamSocket, upstreamHead) => {
    const statusLine = `HTTP/1.1 ${upstreamRes.statusCode} ${upstreamRes.statusMessage}\r\n`;
    const headers = Object.entries(upstreamRes.headers)
      .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : v}`)
      .join("\r\n");
    socket.write(`${statusLine}${headers}\r\n\r\n`);
    if (head?.length) upstreamSocket.write(head);
    if (upstreamHead?.length) socket.write(upstreamHead);
    upstreamSocket.pipe(socket);
    socket.pipe(upstreamSocket);
  });

  upstream.on("response", (res) => {
    socket.write(`HTTP/1.1 ${res.statusCode || 502} ${res.statusMessage || "Bad Gateway"}\r\n\r\n`);
    socket.destroy();
  });

  upstream.on("error", (err) => {
    console.error("TTY WS tunnel error:", err?.message || err);
    try { socket.destroy(); } catch {}
  });

  upstream.end();
}

const mobileTerminalProxy = createProxyMiddleware({
  target: TTYD_URL,
  changeOrigin: true,
  ws: true,
  pathRewrite: (path) => path.replace(/^\/(mobile-terminal|tty)/, "") || "/",
});

// NoorHafiz API proxy
const noorhafizApiProxy = createProxyMiddleware({
  target: NOORHAFIZ_API_URL,
  changeOrigin: true,
  ws: true,
  pathRewrite: (path) => path.replace(/^\/api/, "") || "/",
});

app.use("/tacotutor", (req, res) => {
  res.redirect(302, "/");
});
app.use("/tacotutor-live", (req, res) => {
  res.redirect(302, "/");
});

app.use(["/tty", "/mobile-terminal"], (req, res, next) => {
  res.setHeader("Cache-Control", "no-store, no-cache, must-revalidate, proxy-revalidate");
  res.setHeader("Pragma", "no-cache");
  res.setHeader("Expires", "0");
  next();
});

app.use("/mobile-terminal", (req, res) => {
  const suffix = req.originalUrl.replace(/^\/mobile-terminal/, "") || "";
  res.redirect(302, `/tty${suffix}`);
});

app.use("/tty", mobileTerminalProxy);

// Gateway-owned endpoints
app.get("/api/events/stream", (req, res) => {
  const proxyReq = http.get(`${LIVE_API}/api/events/stream`, (proxyRes) => {
    res.setHeader("Content-Type", "text/event-stream");
    res.setHeader("Cache-Control", "no-cache");
    res.setHeader("Connection", "keep-alive");
    proxyRes.pipe(res);
    proxyRes.on("error", () => res.end());
  });

  proxyReq.on("error", () => res.status(502).end());
  req.on("close", () => proxyReq.destroy());
});

["/system", "/latest-sale", "/redeemed", "/health"].forEach(p => {
  app.get(p, async (req, res) => {
    try {
      const r = await fetch(`${LIVE_API}${p}`);
      const data = await r.json();
      res.json(data);
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });
});

app.get("/token", async (req, res) => {
  try {
    const r = await fetch("http://127.0.0.1:7681/token");
    const data = await r.json();
    res.json(data);
  } catch (e) {
    res.status(500).json({ error: "ttyd" });
  }
});

app.get("/faces", (req, res) => res.sendFile(path.join(UI_DIR, "faces.html")));
app.use("/api/faces", express.json(), facesRouter);

// NoorHafiz API routes
app.use("/nh/api", (req, res, next) => {
  res.setHeader("Cache-Control", "no-store, no-cache, must-revalidate, proxy-revalidate");
  next();
}, noorhafizApiProxy);

// Everything else → NoorHafiz built frontend. Use built static instead of Vite dev
// on the public tunnel so there is no HMR/WebSocket noise on iPhone.
app.use("/assets", express.static(path.join(NOORHAFIZ_STATIC_DIR, "assets"), {
  maxAge: "1y",
  immutable: true,
}));

app.use("/", (req, res, next) => {
  if (req.path.startsWith("/api/") || req.path.startsWith("/nh/api/")) return next();
  res.setHeader("Cache-Control", "no-store, no-cache, must-revalidate, proxy-revalidate");
  res.setHeader("Pragma", "no-cache");
  res.setHeader("Expires", "0");
  res.sendFile(path.join(NOORHAFIZ_STATIC_DIR, "index.html"));
});

const server = http.createServer(app);

server.on("upgrade", (req, socket, head) => {
  if (req.url?.startsWith("/tty")) {
    req.url = req.url.replace(/^\/tty/, "") || "/";
    proxyTtyWebSocket(req, socket, head);
    return;
  }
  // NoorHafiz public UI is served as static production files; no frontend WS needed.
  try { socket.destroy(); } catch {}
});

server.listen(PORT, "::", () => console.log("Gateway Ready → NoorHafiz"));
