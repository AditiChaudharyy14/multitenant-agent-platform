import type { NextConfig } from "next";

// The browser only ever talks to this origin. /api/* is proxied to FastAPI, so the session
// cookie is first-party and the backend's CSRF check sees this origin (ALLOWED_ORIGINS).
const backend = process.env.BACKEND_URL ?? "http://localhost:8000";

// Content-Security-Policy for the pages. Scripts: same origin, plus inline because Next's
// App Router bootstraps with inline scripts; per-request nonces would need every page to be
// dynamically rendered (a prerendered page would then have its scripts blocked). Dev mode
// also needs 'unsafe-eval' for React Refresh. Everything else is locked down: no plugins, no
// framing, no foreign connections (the browser talks only to this origin; /api is proxied).
const dev = process.env.NODE_ENV !== "production";
const csp = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${dev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self'",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: csp },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()" },
];

const nextConfig: NextConfig = {
  // Gzip would buffer the SSE chat stream; the backend's responses are small anyway.
  compress: false,
  // Keeps the floating dev button out of screenshots. Console errors still surface: the
  // Playwright tests fail on any of them.
  devIndicators: false,
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
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
