/* Root: providers + router (C1).
 *
 * Provider order: ToastProvider (global) → UiStateProvider (shared Overview
 * filters, see store/ui.tsx) → router. The Shell is the layout route that
 * carries the sidebar + polled data context; the three views nest under it:
 *   /                → Overview   (C2)
 *   /updates         → Updates    (C3)
 *   /services/:id    → Detail     (C4)
 */

import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { ToastProvider } from "./store/toast";
import { UiStateProvider } from "./store/ui";
import { Detail } from "./views/Detail";
import { Overview } from "./views/Overview";
import { Shell } from "./views/Shell";
import { Updates } from "./views/Updates";

export default function App() {
  return (
    <ToastProvider>
      <UiStateProvider>
        <BrowserRouter>
          <Routes>
            <Route element={<Shell />}>
              <Route index element={<Overview />} />
              <Route path="updates" element={<Updates />} />
              <Route path="services/:id" element={<Detail />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </UiStateProvider>
    </ToastProvider>
  );
}
