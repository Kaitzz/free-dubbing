"use client"

import { ReactNode, useEffect, useState } from "react"
import { AUTH_UNAUTHORIZED_EVENT, getAuthSession } from "@/lib/api"
import { Button } from "@/components/ui/button"

// Initialize the local CSRF token without a password or a login screen.
export function AuthProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false)
  const [error, setError] = useState("")
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let active = true
    getAuthSession().then(() => { if (active) { setError(""); setReady(true) } })
      .catch((e) => { if (active) setError(e instanceof Error ? e.message : "无法连接本机服务") })
    const refresh = () => { setReady(false); setAttempt(n => n + 1) }
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, refresh)
    return () => { active = false; window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, refresh) }
  }, [attempt])
  if (ready) return children
  return <main className="flex min-h-screen flex-col items-center justify-center gap-4">
    <p>{error || "正在连接本机服务…"}</p>
    {error ? <Button onClick={() => { setError(""); setAttempt(n => n + 1) }}>重新连接</Button> : null}
  </main>
}
