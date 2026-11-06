import type { NextConfig } from "next";

// The browser only ever talks to this origin. /api/* is proxied to FastAPI, so the session
// cookie is first-party and the backend's CSRF check sees this origin (ALLOWED_ORIGINS).
const backend = process.env.BACKEND_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // Gzip would buffer the SSE chat stream; the backend's responses are small anyway.
  compress: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
  // File events are unreliable across WSL2 bind mounts, so webpack polls in dev.
  // This only applies to webpack: don't pass --turbopack to next dev.
  webpack: (config, { dev }) => {
    if (dev) {
      config.watchOptions = { ...config.watchOptions, poll: 1000, aggregateTimeout: 300 };
    }
    return config;
  },
};

export default nextConfig;
