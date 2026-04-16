import express from "express";
import http from "http";
import { Readable } from "stream";
import path from "path";
import { fileURLToPath } from "url";
import { createProxyMiddleware } from "http-proxy-middleware";
import facesRouter from "./routes/faces.js";

const PORT = process.env.PORT || 8080;
const DASHBOARD_DIR = path.dirname(fileURLToPath(import.meta.url));
const UI_DIR = process.env.UI_DIR || path.join(DASHBOARD_DIR, "frontend", "dist");
const LIVE_API = process.env.LIVE_API || "http://127.0.0.1:18791";
const TTYD_URL = process.env.TTYD_URL || "http://127.0.0.1:7681";
const app = express();

const mobileTerminalProxy = createProxyMiddleware({
  target: TTYD_URL,
  changeOrigin: true,
  ws: true,
  pathRewrite: (path) => path.replace(/^\/(mobile-terminal|tty)/, "") || "/",
});

app.use(express.json());
app.use("/api/faces", facesRouter);

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

app.all("/api/*", async (req, res) => {
  try {
    const url = `${LIVE_API}${req.url}`;
    const r = await fetch(url);
    const data = await r.json();
    res.json(data);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

["/system", "/latest-sale", "/redeemed", "/health"].forEach(path => {
  app.get(path, async (req, res) => {
    try {
      const r = await fetch(`${LIVE_API}${path}`);
      const data = await r.json();
      res.json(data);
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });
});

app.use("/tty", mobileTerminalProxy);
app.use(express.static(UI_DIR));

app.get("*", (req, res) => {
  if (req.url.startsWith("/api") || req.url.startsWith("/system") || req.url.startsWith("/health") || req.url.startsWith("/mobile-terminal") || req.url.startsWith("/tty")) {
    return;
  }

  const indexPath = path.join(UI_DIR, "index.html");
  res.sendFile(indexPath, (err) => {
    if (err) {
      console.error("Error sending index.html:", err);
      res.status(404).send("index.html not found in " + UI_DIR);
    }
  });
});

app.get("/faces", (req, res) => res.sendFile(path.join(UI_DIR, "faces.html")));

app.get("/token", async (req, res) => {
  try {
    const r = await fetch("http://127.0.0.1:7681/token");
    const data = await r.json();
    res.json(data);
  } catch (e) {
    res.status(500).json({ error: "ttyd" });
  }
});

const server = http.createServer(app);

server.on("upgrade", (req, socket, head) => {
  if (req.url?.startsWith("/tty")) {
    req.url = req.url.replace(/^\/tty/, "") || "/";
    mobileTerminalProxy.upgrade(req, socket, head);
    return;
  }
  socket.destroy();
});

server.listen(PORT, "0.0.0.0", () => console.log("Gateway Ready"));
