import type { HTMLAttributes, ReactNode } from "react";

type TagVariant = "accent" | "neutral" | "outline";

type TagProps = HTMLAttributes<HTMLSpanElement> & {
  variant?: TagVariant;
  children?: ReactNode;
};

/** Nocturne chip. accent = accent-800 fill, neutral = neutral-800, outline = bordered. */
export function Tag({ variant = "neutral", className, children, ...rest }: TagProps) {
  const cls = ["tag", `tag-${variant}`, className].filter(Boolean).join(" ");
  return (
    <span className={cls} {...rest}>
      {children}
    </span>
  );
}
