// Stream downloads with descriptive filenames, including on older API processes.
export const dynamic = "force-dynamic"

function clean(value: string, limit: number) {
  const name = value.replace(/[<>:"/\\|?*\x00-\x1f\x7f]/g, "_").replace(/\s+/g, " ").trim().slice(0, limit).replace(/[ .]+$/, "")
  return name || "Untitled"
}

export async function GET(request: Request, context: { params: Promise<{ id: string; kind: string }> }) {
  const { id, kind } = await context.params
  if (!/^[\w-]{1,100}$/.test(id) || !["video", "cover"].includes(kind)) {
    return new Response("Invalid download", { status: 400 })
  }
  const base = (process.env.NEXT_SERVER_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "")
  const auth = new Headers()
  const cookie = request.headers.get("cookie")
  if (cookie) auth.set("cookie", cookie)
  const taskResponse = await fetch(`${base}/api/tasks/${id}`, { headers: auth, cache: "no-store", signal: request.signal })
  if (!taskResponse.ok) return new Response(taskResponse.body, { status: taskResponse.status })
  const task = await taskResponse.json()
  const headers = new Headers(auth)
  const range = request.headers.get("range")
  if (range) headers.set("range", range)
  const endpoint = kind === "video" ? "artifact/final-video" : "source-asset/thumbnail"
  const source = await fetch(`${base}/api/tasks/${id}/${endpoint}?download=1`, { headers, cache: "no-store", signal: request.signal })
  const responseHeaders = new Headers(source.headers)
  responseHeaders.set("cache-control", "no-store")
  if (source.ok) {
    const mime = source.headers.get("content-type")?.split(";")[0]
    const extension = kind === "video" ? "mp4" : ({ "image/png": "png", "image/webp": "webp", "image/jpeg": "jpg" }[mime || ""] || "jpg")
    const suffix = kind === "cover" ? "cover" : task.output_mode === "subtitles" ? "original" : "dubbed"
    const filename = `${clean(String(task.title || "Untitled"), 100)} [${clean(id, 40)}] - ${suffix}.${extension}`
    const encoded = encodeURIComponent(filename).replace(/['()*]/g, char => `%${char.charCodeAt(0).toString(16).toUpperCase()}`)
    responseHeaders.set("content-disposition", `attachment; filename="${id}-${suffix}.${extension}"; filename*=UTF-8''${encoded}`)
  }
  return new Response(source.body, { status: source.status, headers: responseHeaders })
}
