import type { NextConfig } from "next";

/**
 * Rewrites removed in Phase 4H.1 — `/backend/*` is handled by
 * `src/app/backend/[...path]/route.ts`, which injects the server-only
 * bearer token. `API_INTERNAL_URL` still selects the upstream FastAPI base.
 */
const nextConfig: NextConfig = {};

export default nextConfig;
