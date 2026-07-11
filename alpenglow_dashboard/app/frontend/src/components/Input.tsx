import { forwardRef } from "react";
import type { InputHTMLAttributes, ReactNode } from "react";

type InputProps = InputHTMLAttributes<HTMLInputElement> & {
  /** Optional inset leading icon (e.g. a magnifier). */
  icon?: ReactNode;
  wrapperClassName?: string;
};

/** Nocturne text input, with an optional inset leading icon. */
export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { icon, className, wrapperClassName, style, ...rest },
  ref,
) {
  const input = (
    <input
      ref={ref}
      className={["input", className].filter(Boolean).join(" ")}
      style={icon ? { paddingLeft: 30, ...style } : style}
      {...rest}
    />
  );
  if (!icon) return input;
  return (
    <div
      className={wrapperClassName}
      style={{ position: "relative", display: "inline-flex", alignItems: "center" }}
    >
      <span
        style={{
          position: "absolute",
          left: 10,
          top: "50%",
          transform: "translateY(-50%)",
          display: "inline-flex",
          color: "var(--color-neutral-500)",
          pointerEvents: "none",
        }}
      >
        {icon}
      </span>
      {input}
    </div>
  );
});
