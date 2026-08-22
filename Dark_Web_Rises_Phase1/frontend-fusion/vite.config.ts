// @lovable.dev/vite-tanstack-config already includes the following — do NOT add them manually
// or the app will break with duplicate plugins:
//   - TanStack devtools (dev-only, first), tanstackStart, viteReact, tailwindcss, tsConfigPaths,
//     nitro (build-only using cloudflare as a default target), VITE_* env injection, @ path alias,
//     React/TanStack dedupe, error logger plugins, and sandbox detection (port/host/strictPort).
// You can pass additional config via defineConfig({ vite: { ... }, etc... }) if needed.
import { defineConfig } from "@lovable.dev/vite-tanstack-config";

// Hostnames this dev server will answer to, comma separated.
//
// Vite rejects requests whose Host header it does not recognise -- the
// response is "Blocked request. This host is not allowed." -- which is the
// first thing you hit after pointing a DNS name at the box, and it reads like
// a proxy or routing fault rather than a frontend setting.
//
// Fed by docker-compose.yml from PUBLIC_HOST, so setting that one variable
// covers this too. Set VITE_ALLOWED_HOSTS explicitly to list several names
// (e.g. the bare domain and a www or staging alias).
//
// Left empty -- local development -- Vite keeps its default of localhost only.
const allowedHosts = (process.env.VITE_ALLOWED_HOSTS ?? "")
  .split(",")
  .map((host) => host.trim())
  .filter(Boolean);

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
      // Listen on all interfaces. Inside a container the default of localhost
      // means the published port reaches nothing.
      host: true,
      ...(allowedHosts.length > 0 ? { allowedHosts } : {}),
    },
  },
});
