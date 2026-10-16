import { NextRequest, NextResponse } from "next/server";

/**
 * Server-side API proxy (Phase 4H.1).
 *
 * Browser calls `/backend/*` only. This route forwards to the FastAPI process
 * and injects `Authorization: Bearer` from server-only `RAJOSA_API_TOKEN`.
 * Never use `NEXT_PUBLIC_*` for the API token.
 */

export const dynamic = "force-dynamic";

const HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailers",
  "transfer-encoding",
  "upgrade",
  "host",
  "content-length",
]);

function apiBase(): string {
  return (process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000").replace(
    /\/$/,
    "",
  );
}

async function proxy(
  request: NextRequest,
  pathSegments: string[],
): Promise<NextResponse> {
  const targetPath = pathSegments.map(encodeURIComponent).join("/");
  const url = `${apiBase()}/${targetPath}${request.nextUrl.search}`;

  const headers = new Headers();
  request.headers.forEach((value, key) => {
    if (HOP_BY_HOP.has(key.toLowerCase())) {
      return;
    }
    // Client must not supply the API bearer; only the server injects it.
    if (key.toLowerCase() === "authorization") {
      return;
    }
    headers.set(key, value);
  });

  const token = (process.env.RAJOSA_API_TOKEN ?? "").trim();
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const init: RequestInit = {
    method: request.method,
    headers,
    redirect: "manual",
    cache: "no-store",
  };

  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.arrayBuffer();
  }

  let upstream: Response;
  try {
    upstream = await fetch(url, init);
  } catch {
    return NextResponse.json(
      { detail: "API unavailable" },
      { status: 502 },
    );
  }

  const responseHeaders = new Headers();
  upstream.headers.forEach((value, key) => {
    if (HOP_BY_HOP.has(key.toLowerCase())) {
      return;
    }
    responseHeaders.set(key, value);
  });

  return new NextResponse(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}

type RouteContext = { params: Promise<{ path: string[] }> };

async function handle(
  request: NextRequest,
  context: RouteContext,
): Promise<NextResponse> {
  const { path } = await context.params;
  return proxy(request, path ?? []);
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
export const OPTIONS = handle;
export const HEAD = handle;
