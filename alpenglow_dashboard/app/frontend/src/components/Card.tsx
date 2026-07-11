import type { HTMLAttributes, ReactNode } from "react";

type CardProps = HTMLAttributes<HTMLDivElement> & {
  /** Adds the .12s hover lift (transform + shadow) for clickable cards. */
  hover?: boolean;
  /** Elevation edge; sm is the default surface treatment. */
  elevation?: "sm" | "md" | "lg";
  children?: ReactNode;
};

/** Nocturne surface card: --color-surface, 8px radius, edge shadow. */
export function Card({
  hover = false,
  elevation = "sm",
  className,
  children,
  ...rest
}: CardProps) {
  const cls = ["card", `elev-${elevation}`, hover && "card-hover", className]
    .filter(Boolean)
    .join(" ");
  return (
    <div className={cls} {...rest}>
      {children}
    </div>
  );
}
