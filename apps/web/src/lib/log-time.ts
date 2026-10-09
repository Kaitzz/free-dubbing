const pacific = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit",
  hourCycle: "h23", timeZoneName: "short",
})

/** Convert only leading, timezone-qualified log timestamps; preserve log content. */
export function pacificLog(log: string): string {
  return log.replace(/^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\]/gm, (original, timestamp: string) => {
    const date = new Date(timestamp)
    if (Number.isNaN(date.getTime())) return original
    const parts = Object.fromEntries(pacific.formatToParts(date).map(({ type, value }) => [type, value]))
    return `[${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second} ${parts.timeZoneName}]`
  })
}
