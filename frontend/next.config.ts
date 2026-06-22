import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* Standalone output produces a self-contained server.js for Docker
   * deployment.  NEXT_PUBLIC_API_URL defaults to "" (relative URLs),
   * meaning the frontend makes requests like /v1/sessions that are
   * proxied to the API via nginx. */
  output: "standalone",
};

export default nextConfig;
