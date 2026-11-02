import { NextResponse, type NextRequest } from "next/server";

import {
  REQUEST_ID_HEADER,
  REQUEST_METHOD_HEADER,
  REQUEST_PATH_HEADER,
} from "./src/lib/performance/shared";

export function middleware(request: NextRequest) {
  const requestHeaders = new Headers(request.headers);

  requestHeaders.set(
    REQUEST_ID_HEADER,
    request.headers.get(REQUEST_ID_HEADER) ?? crypto.randomUUID(),
  );
  requestHeaders.set(REQUEST_PATH_HEADER, request.nextUrl.pathname);
  requestHeaders.set(REQUEST_METHOD_HEADER, request.method);

  return NextResponse.next({
    request: {
      headers: requestHeaders,
    },
  });
}

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:css|gif|ico|jpg|jpeg|js|map|png|svg|webp)$).*)",
  ],
};