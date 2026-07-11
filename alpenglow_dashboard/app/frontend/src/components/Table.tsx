import type { HTMLAttributes, ReactNode, ThHTMLAttributes } from "react";

/** Nocturne .table wrapper. Compose with <THead>/<TBody>/<TR>/<TH>/<TD> or raw markup. */
export function Table({
  className,
  children,
  ...rest
}: HTMLAttributes<HTMLTableElement> & { children?: ReactNode }) {
  return (
    <table className={["table", className].filter(Boolean).join(" ")} {...rest}>
      {children}
    </table>
  );
}

export function THead({ children }: { children?: ReactNode }) {
  return <thead>{children}</thead>;
}

export function TBody({ children }: { children?: ReactNode }) {
  return <tbody>{children}</tbody>;
}

export function TR({
  children,
  ...rest
}: HTMLAttributes<HTMLTableRowElement> & { children?: ReactNode }) {
  return <tr {...rest}>{children}</tr>;
}

export function TH({
  children,
  ...rest
}: ThHTMLAttributes<HTMLTableCellElement> & { children?: ReactNode }) {
  return <th {...rest}>{children}</th>;
}

export function TD({
  children,
  ...rest
}: HTMLAttributes<HTMLTableCellElement> & { children?: ReactNode }) {
  return <td {...rest}>{children}</td>;
}
