/* Toast system (handoff §Toast).
 *
 * Bottom-right surface cards with --shadow-lg + a fill check-circle glyph,
 * toastIn animation, ~2.4s auto-dismiss. Exposed as the `useToast()` hook:
 *
 *   const toast = useToast();
 *   toast("Glances restarting…");
 *
 * Wrap the app in <ToastProvider>; it renders the fixed toast host itself.
 */

import { CheckCircleIcon } from "@phosphor-icons/react";
import { createContext, useCallback, useContext, useRef, useState } from "react";
import type { ReactNode } from "react";

const DISMISS_MS = 2400;

type ToastItem = { id: number; message: string };
type ToastFn = (message: string) => void;

const ToastContext = createContext<ToastFn | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(0);

  const toast = useCallback<ToastFn>((message) => {
    const id = nextId.current++;
    setToasts((prev) => [...prev, { id, message }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, DISMISS_MS);
  }, []);

  return (
    <ToastContext.Provider value={toast}>
      {children}
      <div className="toast-host" aria-live="polite" aria-atomic="true">
        {toasts.map((t) => (
          <div key={t.id} className="toast" role="status">
            <CheckCircleIcon size={16} weight="fill" color="var(--color-ok)" />
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

/** Fire a toast. Must be used under <ToastProvider>. */
export function useToast(): ToastFn {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within <ToastProvider>");
  return ctx;
}
