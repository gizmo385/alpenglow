/* C1 fidelity smoke test (not shipped): server-renders the Sidebar with mock
   data and asserts the chrome the 01-overview.png screenshot shows — brand
   wordmark, Overview/Updates nav, the Updates count badge, all five category
   rows with counts, and the host/branch footer. Run via scripts/ssr-smoke.mjs. */

import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import type { OverviewMeta, ServiceSummary } from "../src/api/types";
import { DataContext } from "../src/store/data";
import type { DataContextValue } from "../src/store/data";
import { ToastProvider } from "../src/store/toast";
import { UiStateProvider } from "../src/store/ui";
import { Detail } from "../src/views/Detail";
import { Sidebar } from "../src/views/Sidebar";

function svc(id: string, category: string, extra: Partial<ServiceSummary> = {}): ServiceSummary {
  return {
    id,
    name: id,
    category,
    icon: "cube",
    description: "",
    image: `${id}:1.0`,
    currentVersion: "1.0",
    latestVersion: null,
    releasedAt: null,
    changelogUrl: null,
    status: "up",
    uptimeSeconds: 100,
    url: null,
    tier: "tailnet",
    containers: [],
    sso: { state: "none", source: "" },
    tags: [],
    restartPolicy: "unless-stopped",
    ports: "80",
    memLimit: null,
    ...extra,
  };
}

// Mirror the screenshot counts: Media 4, Productivity 6, Home 3, Infra 8, Monitoring 5; 11 updates.
const services: ServiceSummary[] = [
  ...Array.from({ length: 4 }, (_, i) => svc(`media${i}`, "Media")),
  ...Array.from({ length: 6 }, (_, i) => svc(`prod${i}`, "Productivity")),
  ...Array.from({ length: 3 }, (_, i) => svc(`home${i}`, "Home")),
  ...Array.from({ length: 8 }, (_, i) => svc(`infra${i}`, "Infrastructure")),
  ...Array.from({ length: 5 }, (_, i) => svc(`mon${i}`, "Monitoring")),
];
// Flag 11 of them as having an update for the badge.
services.slice(0, 11).forEach((s) => (s.latestVersion = "1.1"));

const meta: OverviewMeta = { host: "nimbus", branch: "main" };

const html = renderToStaticMarkup(
  <MemoryRouter>
    <UiStateProvider>
      <Sidebar services={services} meta={meta} />
    </UiStateProvider>
  </MemoryRouter>,
);

// ── C4: Service Detail chrome (03-detail-overview.png) ──────────────────────
// Render the detail view for a service that has an update, over the shared
// DataContext (its summary drives status/version/tags) + a ToastProvider, at the
// /services/:id route so useParams resolves. Effects don't run under
// renderToStaticMarkup, so this exercises the initial header/action-bar/tabs +
// the Overview tab facts/tag-editor that render from the polled summary.
const detailSvc: ServiceSummary = svc("immich", "Media", {
  name: "Immich",
  icon: "images",
  image: "ghcr.io/immich-app/immich-server:v1.118.2",
  currentVersion: "v1.118.2",
  latestVersion: "v1.119.0",
  url: "https://immich.acbc.house",
  tier: "tailnet",
  sso: { state: "oidc", source: "OIDC_ISSUER_URL in .env" },
  tags: ["GPU", "User data"],
  ports: "2283",
  restartPolicy: "unless-stopped",
  description: "Self-hosted photo & video backup.",
});

const dataValue: DataContextValue = {
  services: [detailSvc],
  servicesError: null,
  servicesLoading: false,
  refreshServices: () => {},
  overview: null,
  overviewError: null,
  overviewLoading: false,
  refreshOverview: () => {},
};

const detailHtml = renderToStaticMarkup(
  <MemoryRouter initialEntries={["/services/immich"]}>
    <DataContext.Provider value={dataValue}>
      <ToastProvider>
        <Routes>
          <Route path="/services/:id" element={<Detail />} />
        </Routes>
      </ToastProvider>
    </DataContext.Provider>
  </MemoryRouter>,
);

const checks: [string, boolean][] = [
  ["brand wordmark", html.includes("Alpenglow") && html.includes("home")],
  ["Overview nav", html.includes("Overview")],
  ["Updates nav", html.includes("Updates")],
  ["updates badge = 11", /class="[^"]*badge[^"]*"[^>]*>11</.test(html)],
  ["Media count 4", /Media<\/span><span[^>]*>4</.test(html)],
  ["Productivity count 6", /Productivity<\/span><span[^>]*>6</.test(html)],
  ["Home count 3", /Home<\/span><span[^>]*>3</.test(html)],
  ["Infrastructure count 8", /Infrastructure<\/span><span[^>]*>8</.test(html)],
  ["Monitoring count 5", /Monitoring<\/span><span[^>]*>5</.test(html)],
  ["footer host", html.includes("nimbus")],
  ["footer branch", html.includes("alpenglow @ main")],
  // C4 detail checks
  ["detail: back button", detailHtml.includes("Overview")],
  ["detail: service name", detailHtml.includes("Immich")],
  ["detail: mono image line", detailHtml.includes("immich-server:v1.118.2")],
  ["detail: status tag Running", detailHtml.includes("Running")],
  ["detail: Open button (has url)", detailHtml.includes("immich.acbc.house")],
  [
    "detail: Pull & recreate → latest",
    detailHtml.includes("Pull &amp; recreate") && detailHtml.includes("v1.119.0"),
  ],
  ["detail: Restart action", detailHtml.includes("Restart")],
  ["detail: Stop action", detailHtml.includes("Stop")],
  ["detail: Copy update cmd", detailHtml.includes("Copy update cmd")],
  ["detail: all four tabs", ["Overview", "Resources", "Logs", "Compose"].every((t) => detailHtml.includes(t))],
  ["detail: fact Access tier", detailHtml.includes("Access tier")],
  ["detail: fact Single sign-on", detailHtml.includes("Single sign-on")],
  ["detail: SSO source sub-line", detailHtml.includes("OIDC_ISSUER_URL in .env")],
  ["detail: applied tag chip GPU", detailHtml.includes("GPU")],
  ["detail: preset suggestion Critical", detailHtml.includes("Critical")],
];

let ok = true;
for (const [label, pass] of checks) {
  console.log(`${pass ? "PASS" : "FAIL"}  ${label}`);
  if (!pass) ok = false;
}
if (!ok) {
  console.error("\n--- rendered html ---\n" + html);
  process.exit(1);
}
console.log("\nAll sidebar chrome checks passed.");
