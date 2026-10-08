"use client"

import Link from "next/link"
import { ArrowLeft } from "lucide-react"

import { Button } from "@/components/ui/button"
import { ColabDialog } from "@/components/colab-dialog"
import { SettingsDialog } from "@/components/settings-dialog"
import { useI18n } from "@/lib/i18n"

export function AppHeader({ backHref }: { backHref?: string }) {
  const { t } = useI18n()
  return (
    <header className="flex flex-col gap-4 border-b border-[#00aeec]/25 pb-5 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex items-center gap-3">
        {backHref ? (
          <Button
            variant="ghost"
            size="icon-sm"
            nativeButton={false}
            render={<Link href={backHref} aria-label={t.common.back} />}
          >
            <ArrowLeft className="size-4" />
          </Button>
        ) : null}
        <Link href="/" className="flex items-center">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src="/youdub-logo.svg"
            alt="YouDub"
            className="h-9 w-auto sm:h-11"
          />
        </Link>
      </div>
      <div className="flex items-center gap-2">
        <ColabDialog />
        <SettingsDialog />
      </div>
    </header>
  )
}
