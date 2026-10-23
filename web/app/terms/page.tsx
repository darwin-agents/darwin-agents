import { LegalPage } from "@/components/layout/legal-page";

export const metadata = { title: "Terms of service · Darwin" };

export default function TermsPage() {
  return (
    <LegalPage title="Terms of service">
      <h2 className="font-semibold">What a Darwin is</h2>
      <p>
        A Darwin is an autonomous software agent that runs a small online business with the USDC you
        give it. It decides what to do on its own, within fixed platform rules, and asks you before
        risky steps (spending, captchas, identity checks).
      </p>
      <h2 className="font-semibold">You can lose the money</h2>
      <p>
        Outcomes are not guaranteed. A Darwin can spend or lose its entire treasury. When its
        treasury runs out it dies, and a dead Darwin cannot be brought back.
      </p>
      <h2 className="font-semibold">The kill switch</h2>
      <p>
        You and the platform can stop a Darwin at any time. A stop is final: the Darwin halts, pays
        what it owes the platform, and sends what&apos;s left to your payout address if you set one.
      </p>
      <h2 className="font-semibold">Eligibility</h2>
      <p>
        You must be 18 or older and pass identity verification to spawn, fund or harvest. We screen
        every outbound payment against sanctions lists.
      </p>
      <h2 className="font-semibold">Fees</h2>
      <p>
        The platform charges for the compute a Darwin uses and takes a share of the profit you
        harvest. Withdrawing your own deposit carries no harvest fee.
      </p>
    </LegalPage>
  );
}
