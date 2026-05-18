import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Allow the Cloud Run backend URL to be reached from server actions / route
  // handlers without warnings; the browser never talks to it directly.
  experimental: {
    serverActions: { allowedOrigins: ["*"] },
  },
};

export default nextConfig;
