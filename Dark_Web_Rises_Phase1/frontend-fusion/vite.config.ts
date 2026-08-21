// @lovable.dev/vite-tanstack-config already includes the following — do NOT add them manually
// or the app will break with duplicate plugins:
//   - TanStack devtools (dev-only, first), tanstackStart, viteReact, tailwindcss, tsConfigPaths,
//     nitro (build-only using cloudflare as a default target), VITE_* env injection, @ path alias,
//     React/TanStack dedupe, error logger plugins, and sandbox detection (port/host/strictPort).
// You can pass additional config via defineConfig({ vite: { ... }, etc... }) if needed.
import { defineConfig } from "@lovable.dev/vite-tanstack-config";

export default defineConfig({
  tanstackStart: {
    // Redirect TanStack Start's bundled server entry to src/server.ts (our SSR error wrapper).
    // nitro/vite builds from this
    server: { entry: "server" },
  },
  vite: {
    server: {
      // The shared config defaults to 8080, which CTFd already occupies in
      // docker-compose.yml. Without strictPort Vite would quietly pick 8081
      // instead, and the origin would no longer match the backend's
      // CORS_ALLOWED_ORIGINS -- which surfaces as a websocket that refuses to
      // connect rather than an obvious port error.
      //
      // Pinned so the origin is predictable. If you change it, change
      // CORS_ALLOWED_ORIGINS in ../.env to match.
      port: 5173,
      strictPort: true,
    },
  },
});
