import type { NextConfig } from "next";

const nextConfig: NextConfig = {
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
