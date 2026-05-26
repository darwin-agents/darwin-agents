"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusBadge } from "@/components/darwin/status-badge";
import { AlertBadges } from "@/components/darwin/alert-badges";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

/**
 * M0-A design token reference. Verifies status semantics, density, and
 * monospaced ids render correctly. Lives under (dashboard) so it picks up
 * the dev-auth gate; remove or move under /_dev/ before any prod ship.
 */
export default function TokensPage() {
  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Design tokens</h1>
        <p className="mt-1 text-sm text-[var(--color-muted-foreground)]">
          Status semantics, alert grades, density, and typography.
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle>Status badges (Darwin lifecycle)</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-3">
          <StatusBadge status="alive" />
          <StatusBadge status="paused" />
          <StatusBadge status="quarantined" />
          <StatusBadge status="dead" />
          <StatusBadge status="killed" />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Graded alerts</CardTitle>
        </CardHeader>
        <CardContent>
          <AlertBadges
            alerts={[
              { level: 1, code: "warn", summary: "Cost > 0.5 USDC/h" },
              { level: 2, code: "throttle", summary: "LLM tier rate-limited" },
              { level: 3, code: "pause", summary: "Repeated rejection" },
              { level: 4, code: "quarantine", summary: "Egress locked" },
            ]}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Buttons</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-3">
          <Button>Default</Button>
          <Button variant="outline">Outline</Button>
          <Button variant="ghost">Ghost</Button>
          <Button variant="destructive">Destructive</Button>
          <Button variant="link">Link</Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Badges</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-3">
          <Badge>Default</Badge>
          <Badge variant="outline">Outline</Badge>
          <Badge variant="accent">Accent</Badge>
          <Badge variant="destructive">Destructive</Badge>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Typography</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <p className="text-2xl font-semibold">Heading 24/600</p>
          <p className="text-lg font-semibold">Heading 18/600</p>
          <p className="text-sm">Body 14 — primary copy</p>
          <p className="text-xs text-[var(--color-muted-foreground)]">
            Body 12 muted — secondary copy
          </p>
          <p className="mono text-sm">
            darwin_a3f9b4c1e2 · event_01HQ8XZ2J · gen 2
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
