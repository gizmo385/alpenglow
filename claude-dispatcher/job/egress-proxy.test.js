"use strict";
process.env.EGRESS_ALLOW = "api.anthropic.com,github.com";
const test = require("node:test");
const assert = require("node:assert");
const { allowed, privateAddress, isMetricsPost } = require("./egress-proxy.js");

test("allowlist matches hosts and subdomains only", () => {
  assert.ok(allowed("api.anthropic.com"));
  assert.ok(allowed("codeload.github.com"));
  assert.ok(allowed("GitHub.com."));
  assert.ok(!allowed("evilgithub.com"));
  assert.ok(!allowed("anthropic.com"));
});

test("private, loopback, link-local and Tailnet addresses are refused", () => {
  for (const ip of ["10.0.0.1", "127.0.0.1", "172.16.5.4", "192.168.1.10", "169.254.169.254", "100.101.102.103", "::1", "fd00::1", "::ffff:10.0.0.1"]) {
    assert.ok(privateAddress(ip), ip);
  }
  for (const ip of ["140.82.112.3", "160.79.104.10", "172.32.0.1", "2606:4700::1"]) {
    assert.ok(!privateAddress(ip), ip);
  }
});

test("only POST /v1/metrics counts as a metrics request", () => {
  assert.ok(isMetricsPost({ method: "POST", url: "/v1/metrics" }));
  assert.ok(isMetricsPost({ method: "POST", url: "http://claude-egress-test:3128/v1/metrics" }));
  assert.ok(!isMetricsPost({ method: "GET", url: "/v1/metrics" }));
  assert.ok(!isMetricsPost({ method: "POST", url: "/v1/logs" }));
  assert.ok(!isMetricsPost({ method: "POST", url: "http://evil.example/v1/logs" }));
});
