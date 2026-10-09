import { afterEach, describe, expect, it, vi } from "vitest"

import { deleteTask } from "@/lib/api"

function json(body: unknown, status: number) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("CSRF token refresh", () => {
  it("retries a mutation once with a fresh token after the backend restarted", async () => {
    const sentTokens: (string | null)[] = []
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === "/api/auth/session") {
        return json({ authenticated: true, csrf_token: "fresh-token", expires_at: "" }, 200)
      }
      sentTokens.push(new Headers(init?.headers).get("X-CSRF-Token"))
      return sentTokens.length === 1
        ? json({ detail: "CSRF validation failed." }, 403)
        : new Response(null, { status: 204 })
    }))

    await expect(deleteTask("task-1")).resolves.toBeUndefined()
    expect(sentTokens).toEqual([null, "fresh-token"])
  })

  it("does not retry other refusals", async () => {
    const fetchMock = vi.fn(async () => json({ detail: "Local GUI access only." }, 403))
    vi.stubGlobal("fetch", fetchMock)

    await expect(deleteTask("task-1")).rejects.toThrow("Local GUI access only.")
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
