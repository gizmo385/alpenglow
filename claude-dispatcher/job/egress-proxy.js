// Egress proxy for Claude jobs: the only way out of a job's network.
//
// An HTTPS CONNECT proxy that allows port 443 to the hosts in EGRESS_ALLOW
// (comma-separated; "example.com" also allows its subdomains) and refuses
// everything else. It also refuses any allowed name that resolves to a
// private, loopback, link-local or Tailnet address, so a job can't reach the
// LAN by pointing an allowed name at it. Plain HTTP is refused.
//
// With OTLP_UPSTREAM set it also relays Claude Code's metrics: POST /v1/metrics
// goes to that one URL and nothing else does. Alloy's OTLP endpoint sits behind
// Caddy, so a plain allowlist entry would let a job pick any internal site by
// Host header; here the job only ever chooses the body.
//
// Run by the dispatcher, one per job profile, on the job daemon:
//   EGRESS_ALLOW=api.anthropic.com,github.com node egress-proxy.js

"use strict";

const dns = require("node:dns").promises;
const http = require("node:http");
const net = require("node:net");

const PORT = Number(process.env.EGRESS_PORT || 3128);
const ALLOW = (process.env.EGRESS_ALLOW || "")
  .split(",")
  .map((h) => h.trim().toLowerCase())
  .filter(Boolean);
const OTLP_UPSTREAM = (process.env.OTLP_UPSTREAM || "").replace(/\/$/, "");
const OTLP_MAX_BYTES = 1024 * 1024;

function allowed(host) {
  host = host.toLowerCase().replace(/\.$/, "");
  return ALLOW.some((a) => host === a || host.endsWith("." + a));
}

function privateAddress(ip) {
  if (net.isIPv4(ip)) {
    const [a, b] = ip.split(".").map(Number);
    return (
      a === 10 || a === 127 || a === 0 ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 168) ||
      (a === 169 && b === 254) ||
      (a === 100 && b >= 64 && b <= 127) // Tailnet / CGNAT
    );
  }
  const v6 = ip.toLowerCase();
  if (v6.startsWith("::ffff:")) return privateAddress(v6.slice(7));
  return v6 === "::1" || v6 === "::" || v6.startsWith("fc") || v6.startsWith("fd") || v6.startsWith("fe80");
}

function log(verdict, host, why) {
  console.log(JSON.stringify({ time: new Date().toISOString(), verdict, host, why }));
}

// Jobs post here directly (NO_PROXY); a client going through HTTP_PROXY would
// send an absolute URL instead.
function isMetricsPost(req) {
  try {
    return req.method === "POST" && new URL(req.url, "http://relay").pathname === "/v1/metrics";
  } catch {
    return false;
  }
}

async function relayMetrics(req, res) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > OTLP_MAX_BYTES) {
      log("deny", "/v1/metrics", "too large");
      return res.writeHead(413).end();
    }
    chunks.push(chunk);
  }
  const headers = {};
  for (const name of ["content-type", "content-encoding"]) {
    if (req.headers[name]) headers[name] = req.headers[name];
  }
  const upstream = await fetch(`${OTLP_UPSTREAM}/v1/metrics`, { method: "POST", headers, body: Buffer.concat(chunks) });
  log("allow", "/v1/metrics", `${size} bytes, upstream ${upstream.status}`);
  res.writeHead(upstream.status, { "content-type": upstream.headers.get("content-type") || "application/x-protobuf" });
  res.end(Buffer.from(await upstream.arrayBuffer()));
}

const server = http.createServer((req, res) => {
  if (OTLP_UPSTREAM && isMetricsPost(req)) {
    return relayMetrics(req, res).catch((err) => {
      log("deny", "/v1/metrics", "relay failed: " + err.message);
      if (!res.headersSent) res.writeHead(502);
      res.end();
    });
  }
  log("deny", req.headers.host || "", "plain http");
  res.writeHead(403).end("only HTTPS CONNECT is allowed\n");
});

server.on("connect", async (req, client, head) => {
  const [host, portText] = (req.url || "").split(":");
  const port = Number(portText);
  const refuse = (why) => {
    log("deny", req.url, why);
    client.end("HTTP/1.1 403 Forbidden\r\n\r\n");
  };
  if (port !== 443) return refuse("port");
  if (!allowed(host)) return refuse("not on the allowlist");
  let address;
  try {
    ({ address } = await dns.lookup(host));
  } catch (err) {
    return refuse("dns: " + err.code);
  }
  if (privateAddress(address)) return refuse("resolves to a private address " + address);
  const upstream = net.connect(port, address, () => {
    log("allow", req.url, address);
    client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
    if (head && head.length) upstream.write(head);
    upstream.pipe(client);
    client.pipe(upstream);
  });
  upstream.on("error", () => client.destroy());
  client.on("error", () => upstream.destroy());
});

if (require.main === module) {
  server.listen(PORT, () => console.log(`egress proxy on :${PORT}, allowing ${ALLOW.join(", ")}`));
}

module.exports = { allowed, privateAddress, isMetricsPost };
