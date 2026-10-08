"use client"

import { useEffect, useState } from "react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { createColabToken, getColabStatus, type ColabStatus } from "@/lib/api"

export function ColabDialog() {
  const [open, setOpen] = useState(false)
  const [status, setStatus] = useState<ColabStatus | null>(null)
  const [token, setToken] = useState("")
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (!open) return
    let active = true
    async function refresh() {
      try { const value = await getColabStatus(); if (active) setStatus(value) }
      catch (e) { if (active) setError(e instanceof Error ? e.message : "连接状态读取失败") }
    }
    void refresh()
    const timer = setInterval(() => void refresh(), 5000)
    return () => { active = false; clearInterval(timer) }
  }, [open])
  async function pair() {
    setBusy(true); setError("")
    try { setToken((await createColabToken()).token) }
    catch (e) { setError(e instanceof Error ? e.message : "生成密钥失败") }
    finally { setBusy(false) }
  }
  return <Dialog open={open} onOpenChange={value => {setOpen(value); if (!value) setToken("")}}>
    <DialogTrigger render={<Button variant="outline" />}>Colab 连接</DialogTrigger>
    <DialogContent className="sm:max-w-lg">
      <DialogHeader><DialogTitle>连接 Google Colab</DialogTitle></DialogHeader>
      <p>{status ? !status.enabled ? "当前为本机执行模式，请使用 Colab GUI 启动脚本。" : status.connected ? "Colab 已连接，可以创建任务。" : "等待 Colab 连接；新任务会排队。" : "正在读取状态…"}</p>
      {status?.tunnel_url ? <><label htmlFor="colab-url" className="text-sm">本次连接地址</label><input id="colab-url" readOnly value={status.tunnel_url} className="w-full rounded border p-2 text-xs" onFocus={e => e.target.select()} /></> : null}
      <div className="flex gap-4 text-sm underline"><a href="/api/remote/files/notebook">下载 Colab Notebook</a><a href="/api/remote/files/bundle">下载源码包</a></div>
      <ol className="list-decimal space-y-2 pl-5 text-sm">
        <li>使用本机启动脚本提供的 HTTPS Tunnel 地址。</li>
        <li>在下方生成专用连接密钥，填入 Colab worker notebook。</li>
        <li>运行 Colab 的领取任务单元，然后回到这里上传视频。</li>
      </ol>
      <p className="text-sm text-muted-foreground">任务视频、音频和字幕会传到你的 Colab，阶段结果会传回本机。Cookie 和本机 API 密钥不传输；MiniMax 密钥在 Colab Secrets 中配置。需要 Cookie 的视频请先下载，再上传本地文件。</p>
      <Button onClick={() => void pair()} disabled={busy || !status?.enabled}>{busy ? "正在生成…" : status?.paired ? "生成新连接密钥（旧密钥失效）" : "生成连接密钥"}</Button>
      {token ? <><label htmlFor="colab-token" className="text-sm">仅本次显示，请复制到 Colab</label><input id="colab-token" readOnly value={token} className="w-full rounded border p-2 font-mono text-xs" onFocus={e => e.target.select()} /></> : null}
      {error ? <p role="alert" className="text-sm text-red-600">{error}</p> : null}
    </DialogContent>
  </Dialog>
}
