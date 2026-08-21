import { cn } from "@/lib/utils";

export function ImageViewer({
  src,
  alt,
  label,
  className,
  aspect = "square",
  placeholder,
}: {
  src?: string;
  alt?: string;
  label?: string;
  className?: string;
  aspect?: "square" | "video" | "portrait";
  placeholder?: string;
}) {
  const ratio = aspect === "square" ? "aspect-square" : aspect === "video" ? "aspect-video" : "aspect-[3/4]";
  return (
    <div className={cn("relative overflow-hidden rounded-lg border border-border panel", ratio, className)}>
      {src ? (
        <img src={src} alt={alt ?? ""} className="h-full w-full object-cover" />
      ) : (
        <div className="flex h-full w-full flex-col items-center justify-center gap-3 bg-grid">
          <div className="font-mono text-xs uppercase tracking-[0.3em] text-muted-foreground">
            {placeholder ?? "// image will render here"}
          </div>
          <div className="h-px w-24 bg-neon animate-pulse-neon" />
        </div>
      )}
      {/* scanline overlay */}
      <div className="pointer-events-none absolute inset-0 bg-scanlines opacity-40" />
      {/* scan line moving */}
      <div className="pointer-events-none absolute inset-x-0 top-0 h-16 bg-gradient-to-b from-neon/20 to-transparent animate-scan" />
      {label && (
        <div className="absolute bottom-0 left-0 right-0 border-t border-border bg-background/80 px-3 py-1.5 font-mono text-[10px] uppercase tracking-widest text-neon backdrop-blur">
          {label}
        </div>
      )}
    </div>
  );
}
