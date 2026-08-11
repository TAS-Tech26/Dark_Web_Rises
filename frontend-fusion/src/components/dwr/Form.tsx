import type { InputHTMLAttributes, TextareaHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

export function Field({
  label,
  hint,
  className,
  children,
}: {
  label: string;
  hint?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <label className={cn("block", className)}>
      <span className="mb-1.5 block font-mono text-[10px] uppercase tracking-[0.25em] text-muted-foreground">
        {label}
      </span>
      {children}
      {hint && <span className="mt-1 block text-[11px] text-muted-foreground">{hint}</span>}
    </label>
  );
}

export function DwrInput(props: InputHTMLAttributes<HTMLInputElement>) {
  const { className, ...rest } = props;
  return (
    <input
      {...rest}
      className={cn(
        "flex h-11 w-full rounded border border-border bg-input px-3 font-mono text-sm text-foreground placeholder:text-muted-foreground focus:border-neon focus:outline-none focus:ring-1 focus:ring-neon transition-colors",
        className,
      )}
    />
  );
}

export function DwrTextarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const { className, ...rest } = props;
  return (
    <textarea
      {...rest}
      className={cn(
        "flex min-h-24 w-full rounded border border-border bg-input px-3 py-2 font-mono text-sm text-foreground placeholder:text-muted-foreground focus:border-neon focus:outline-none focus:ring-1 focus:ring-neon transition-colors resize-y",
        className,
      )}
    />
  );
}
