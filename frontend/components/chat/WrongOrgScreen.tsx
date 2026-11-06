"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";

import { OrgSeal } from "@/components/OrgSeal";
import { api } from "@/lib/api";
import { brandStyle } from "@/lib/brand";
import type { Me } from "@/lib/types";

/**
 * Shown when a signed-in account opens another org's chat URL. Everything here comes from
 * the user's own session (/api/me): nothing about the requested org is fetched. The
 * requested slug appears only as the text the user typed.
 */
export function WrongOrgScreen({ me, requestedSlug }: { me: Me; requestedSlug: string }) {
  const router = useRouter();
  const home = `/a/${me.org.slug}/chat`;

  async function logout() {
    await api.logout().catch(() => undefined);
    router.replace(`/a/${requestedSlug}`);
  }

  return (
    <main style={brandStyle(me.org)} className="flex min-h-dvh items-center bg-brand-soft px-4 py-10">
      <section
        data-testid="wrong-org"
        aria-labelledby="wrong-org-title"
        className="mx-auto w-full max-w-xl rounded-[var(--radius-bubble)] border border-rule bg-paper p-6 sm:p-10"
      >
        <OrgSeal emoji={me.org.branding.logo_emoji} size="lg" />
        <h1 id="wrong-org-title" className="mt-6 font-display text-[32px] leading-tight md:text-[40px]">
          This account belongs to {me.org.name}
        </h1>
        <p className="mt-4 text-[16px] leading-relaxed text-muted">
          You&apos;re signed in as <strong className="font-semibold text-ink">{me.user.email}</strong>, a {me.org.name}{" "}
          account. Each business on this platform keeps its own accounts, chats and records, so this account can&apos;t
          open <code className="whitespace-nowrap rounded bg-brand-soft px-1.5 py-0.5 text-[14px] text-ink">/a/{requestedSlug}</code>.
        </p>
        <div className="mt-8 flex flex-col gap-3 sm:flex-row">
          <Link
            href={home}
            className="whitespace-nowrap rounded-[var(--radius-control)] bg-brand px-5 py-3 text-center font-medium text-white"
          >
            Go to {me.org.name}
          </Link>
          <button
            type="button"
            onClick={logout}
            className="whitespace-nowrap rounded-[var(--radius-control)] border border-rule bg-white px-5 py-3 font-medium"
          >
            Log out
          </button>
        </div>
      </section>
    </main>
  );
}
