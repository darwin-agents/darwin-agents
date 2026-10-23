import { LegalPage } from "@/components/layout/legal-page";

export const metadata = { title: "Privacy · Darwin" };

export default function PrivacyPage() {
  return (
    <LegalPage title="Privacy">
      <h2 className="font-semibold">What we keep</h2>
      <ul className="list-disc space-y-1 pl-5">
        <li>Your account (via our sign-in provider) and the Darwins you own.</li>
        <li>
          Your identity-verification result. The documents themselves stay with our verification
          provider.
        </li>
        <li>Wallet addresses and the on-chain transfers your Darwins make, which are public.</li>
        <li>Each Darwin&apos;s tamper-evident activity log, which you can read and export.</li>
        <li>Device push subscriptions you enable, so escalations reach you.</li>
      </ul>
      <h2 className="font-semibold">Why</h2>
      <p>
        To run your Darwins, meet anti-money-laundering and sanctions obligations, and show you
        exactly what your Darwin did and why.
      </p>
      <h2 className="font-semibold">Retention</h2>
      <p>
        A dead Darwin&apos;s data is kept for 90 days so you can inspect it, then deleted, except
        records the law requires us to keep.
      </p>
    </LegalPage>
  );
}
