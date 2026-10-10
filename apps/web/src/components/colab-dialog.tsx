"use client"

import { useEffect, useState } from "react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { createColabToken, getColabStatus, type ColabStatus } from "@/lib/api"

export function ColabDialog() {
  const [open, setOpen] = useState(false)
  const [status, setStatus] = useState<ColabStatus | null>(null)
  const [token, setToken] = useState("")
  const [password, setPassword] = useState("")
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
    try { setToken((await createColabToken(password)).token) }
    catch (e) { setError(e instanceof Error ? e.message : "生成密钥失败") }
    finally { setBusy(false) }
  }
  return <Dialog open={open} onOpenChange={value => {setOpen(value); if (!value) setToken("")}}>
    <DialogTrigger render={<Button variant="outline" />}>Colab 连接</DialogTrigger>
    <DialogContent className="sm:max-w-lg">
      <DialogHeader><DialogTitle>连接 Google Colab</DialogTitle></DialogHeader>
      <p>{status ? !status.enabled ? "当前为本机执行模式，请使用 Colab GUI 启动脚本。" : status.connected ? status.gpu === false ? "Colab 已连接（无 GPU）：只处理“原音 + 字幕文件”任务，配音任务会等有 GPU 的 Colab 连接。" : "Colab 已连接，可以创建任务。" : "等待 Colab 连接；新任务会排队。" : "正在读取状态…"}</p>
      {status?.tunnel_url ? <><label htmlFor="colab-url" className="text-sm">本次连接地址</label><input id="colab-url" readOnly value={status.tunnel_url} className="w-full rounded border p-2 text-xs" onFocus={e => e.target.select()} /></> : null}
      <div className="flex gap-4 text-sm underline"><a href="https://colab.research.google.com/github/Kaitzz/free-dubbing/blob/main/notebooks/Dubbing_Launcher.ipynb" target="_blank" rel="noopener noreferrer">打开启动壳模板</a><a href="/api/remote/files/notebook">下载私有启动 Notebook</a></div>
      <ol className="list-decimal space-y-2 pl-5 text-sm">
        <li>在 Colab 打开 Notebook，选择 GPU 运行时（只做“原音 + 字幕文件”任务时选 CPU 也可以）；源码会自动从 GitHub 下载，无需上传源码包。</li>
        <li>使用本机启动脚本提供的 HTTPS Tunnel 地址。</li>
        <li>使用私有启动 Notebook 中的固定 8 位密码；重启不会更换。仅在需要改密码时使用下方设置，并同步修改私有 Notebook。</li>
        <li>运行 Colab 的领取任务单元，然后回到这里上传视频。</li>
      </ol>
      <p className="text-sm text-muted-foreground">任务视频、音频和字幕会传到你的 Colab，阶段结果会传回本机。设置中保存的 YouTube Cookie 仅传给 Colab 的 YouTube 下载阶段；本机 API 密钥不传输，MiniMax 密钥在 Colab Secrets 中配置。</p>
      <label htmlFor="connection-password" className="text-sm">设置固定连接密码（8 位数字）</label>
      <input id="connection-password" type="password" inputMode="numeric" maxLength={8} value={password} onChange={e => setPassword(e.target.value)} className="rounded border p-2" autoComplete="new-password" />
      <Button onClick={() => void pair()} disabled={busy || !status?.enabled || !/^[0-9]{8}$/.test(password)}>{busy ? "正在保存…" : "保存固定密码"}</Button>
      {token ? <><label htmlFor="colab-token" className="text-sm">已保存，重启后保持不变；请同步到私有启动 Notebook</label><input id="colab-token" readOnly value={token} className="w-full rounded border p-2 font-mono text-xs" onFocus={e => e.target.select()} /></> : null}
      {error ? <p role="alert" className="text-sm text-red-600">{error}</p> : null}
    </DialogContent>
  </Dialog>
}
