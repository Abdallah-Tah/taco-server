import express from "express";
import http from "http";
import net from "net";
import { Readable } from "stream";
import path from "path";
import { fileURLToPath } from "url";
import facesRouter from "./routes/faces.js";

const PORT = process.env.PORT || 8080;
const DASHBOARD_DIR = path.dirname(fileURLToPath(import.meta.url));
const UI_DIR = process.env.UI_DIR || DASHBOARD_DIR;
const LIVE_API = process.env.LIVE_API || "http://127.0.0.1:18791";
const app = express();

app.use(express.json());
app.use("/api/faces", facesRouter);

app.get("/api/events/stream", async (req, res) => {
  try {
    const r = await fetch(`${LIVE_API}/api/events/stream`);
    res.setHeader("Content-Type", "text/event-stream");
    res.setHeader("Cache-Control", "no-cache");
    res.setHeader("Connection", "keep-alive");
    if (!r.ok || !r.body) {
      res.status(502).end();
      return;
    }
    const upstream = Readable.fromWeb(r.body);
    req.on("close", () => upstream.destroy());
    upstream.on("error", () => res.end());
    upstream.pipe(res);
  } catch (e) {
    res.status(500).end();
  }
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

app.use(express.static(UI_DIR));

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
  const proxy = net.connect(7681, "127.0.0.1", () => {
    let r = `${req.method} ${req.url} HTTP/${req.httpVersion}\r\n`;
    for (const [k, v] of Object.entries(req.headers)) r += `${k}: ${v}\r\n`;
    r += "\r\n";
    proxy.write(r);
    proxy.write(head);
    socket.pipe(proxy).pipe(socket);
  });
});

server.listen(PORT, "0.0.0.0", () => console.log("Gateway Ready"));
