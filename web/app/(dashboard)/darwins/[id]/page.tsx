"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { use } from "react";
import {
  Activity,
  Clock,
  CreditCard,
  Database,
  FileText,
  GitBranch,
  Lock,
  Receipt,
  Server,
  Settings,
  ShieldCheck,
  Skull,
} from "lucide-react";
import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { DarwinHeader } from "@/components/darwin/darwin-header";
import { TreasuryStrip } from "@/components/darwin/treasury-strip";
import { EscalationList } from "@/components/darwin/escalation-list";
import { EventTable } from "@/components/timeline/event-table";

export default function DarwinPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);

  const { data: status } = useQuery({
    queryKey: queryKeys.darwin(id),
    queryFn: () => api.getDarwin(id),
    refetchInterval: 10_000,
  });
  const { data: billing } = useQuery({
    queryKey: queryKeys.billing(id),
    queryFn: () => api.billing(id),
    refetchInterval: 30_000,
  });
  // Capabilities gate the deployment-mode-only tabs (forensics here, organism
  // inside the Data page). On error they stay hidden — fail safe.
  const { data: caps } = useQuery({
    queryKey: queryKeys.capabilities(),
    queryFn: () => api.capabilities(),
    staleTime: 5 * 60_000,
    retry: false,
  });

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-6">
      <DarwinHeader darwinId={id} />

      <TreasuryStrip status={status} billing={billing} />

      <div className="flex flex-wrap gap-2 text-xs">
        <SubNav href={`/darwins/${id}/anatomy`} icon={Activity} label="Anatomy" />
        <SubNav href={`/darwins/${id}/timeline`} icon={Clock} label="Timeline" />
        <SubNav href={`/darwins/${id}/lineage`} icon={GitBranch} label="Lineage" />
        <SubNav href={`/darwins/${id}/billing`} icon={Receipt} label="Billing & ledger" />
        <SubNav href={`/darwins/${id}/summary`} icon={FileText} label="Tax summary" />
        <SubNav href={`/darwins/${id}/preapprovals`} icon={ShieldCheck} label="Pre-approvals" />
        <SubNav href={`/darwins/${id}/payments`} icon={CreditCard} label="Payments" />
        <SubNav href={`/darwins/${id}/vault`} icon={Lock} label="Vault" />
        <SubNav href={`/darwins/${id}/resources`} icon={Server} label="Resources" />
        <SubNav href={`/darwins/${id}/data`} icon={Database} label="Data" />
        {caps?.forensics_viewer && (
          <SubNav href={`/darwins/${id}/forensics`} icon={Skull} label="Forensics" />
        )}
        <SubNav href={`/darwins/${id}/settings`} icon={Settings} label="Settings & controls" />
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <EventTable darwinId={id} />
        <div className="space-y-4">
          <EscalationList darwinId={id} />
        </div>
      </div>
    </div>
  );
}

function SubNav({
  href,
  icon: Icon,
  label,
}: {
  href: string;
  icon: React.ComponentType<{ className?: string }>;
  label: string;
}) {
  return (
    <Link
      href={{ pathname: href }}
      className="inline-flex items-center gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-1.5 text-[var(--color-muted-foreground)] hover:bg-[var(--color-muted)] hover:text-[var(--color-foreground)]"
    >
      <Icon className="size-3.5" />
      {label}
    </Link>
  );
}
