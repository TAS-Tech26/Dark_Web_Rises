import type { ReactNode } from "react";
import { useEffect } from "react";
import { cn } from "@/lib/utils";
import { X } from "lucide-react";

export function Modal({
  open,
  onClose,
  title,
  children,
  className,
  size = "md",
}: {
  open: boolean;
  onClose: () => void;
  title?: string;
  children: ReactNode;
  className?: string;
  size?: "sm" | "md" | "lg" | "xl";
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    if (open) document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const sizes = {
    sm: "max-w-md",
    md: "max-w-lg",
    lg: "max-w-2xl",
    xl: "max-w-4xl",
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        className="absolute inset-0 bg-background/80 backdrop-blur-sm"
        onClick={onClose}
      />

      <div
        className={cn(
          "panel-elevated relative w-full max-h-[90vh] overflow-hidden rounded-lg animate-in fade-in zoom-in-95",
          sizes[size],
          className,
        )}
      >
        <button
          onClick={onClose}
          className="absolute right-3 top-3 z-10 rounded p-1 text-muted-foreground hover:bg-surface hover:text-neon"
          aria-label="Close"
        >
          <X className="h-4 w-4" />
        </button>

        {title && (
          <div className="border-b border-border px-6 pt-6 pb-4">
            <h3 className="font-mono text-lg uppercase tracking-[0.2em] text-neon">
              {title}
            </h3>
          </div>
        )}

       <div
  className="custom-scrollbar overflow-y-auto px-6 pb-6"
  style={{ maxHeight: "calc(90vh - 80px)" }}
>
          {children}
        </div>
      </div>
    </div>
  );
}