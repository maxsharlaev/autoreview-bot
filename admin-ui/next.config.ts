import type { NextConfig } from "next";

function basePathFromEnv(): string {
  const raw = (process.env.ADMIN_UI_BASE_PATH || "/control").trim() || "/control";
  return raw.endsWith("/") && raw !== "/" ? raw.slice(0, -1) : raw;
}

const nextConfig: NextConfig = {
  output: "export",
  basePath: basePathFromEnv(),
  trailingSlash: true,
  images: { unoptimized: true },
};

export default nextConfig;
