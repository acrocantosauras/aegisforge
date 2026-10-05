const { defineConfig } = require("next");

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Emit a self-contained server bundle for the production Docker image.
  output: "standalone",
  // Legacy routes folded into their canonical replacements. Kept as redirects
  // so old links don't fall through to the dynamic /requests/[id] route.
  async redirects() {
    return [
      { source: "/logs", destination: "/system", permanent: false },
      { source: "/requests/new", destination: "/execute", permanent: false },
    ];
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${
          process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"
        }/api/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
