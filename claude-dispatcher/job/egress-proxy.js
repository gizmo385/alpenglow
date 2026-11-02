// Egress proxy for Claude jobs: the only way out of a job's network.
//
// An HTTPS CONNECT proxy that allows port 443 to the hosts in EGRESS_ALLOW
// (comma-separated; "example.com" also allows its subdomains) and refuses
// everything else. It also refuses any allowed name that resolves to a
// private, loopback, link-local or Tailnet address, so a job can't reach the
// LAN by pointing an allowed name at it. Plain HTTP is refused.
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

const server = http.createServer((req, res) => {
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

module.exports = { allowed, privateAddress };
