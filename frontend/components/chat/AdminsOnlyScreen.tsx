"use client";

import Link from "next/link";

import { OrgSeal } from "@/components/OrgSeal";
import type { Me } from "@/lib/types";

/**
 * Shown when a customer opens /a/<slug>/admin. Built from the session's own /api/me only:
 * no admin endpoint is called. The server would answer 403 anyway; this just says so
 * clearly instead of showing an empty, broken chat.
 */
export function AdminsOnlyScreen({ me }: { me: Me }) {
  return (
    <main className="flex min-h-dvh items-center bg-brand-soft px-4 py-10">
      <section
        data-testid="admins-only"
        aria-labelledby="admins-only-title"
        className="mx-auto w-full max-w-xl rounded-[var(--radius-bubble)] border border-rule bg-paper p-6 sm:p-10"
      >
        <OrgSeal emoji={me.org.branding.logo_emoji} size="lg" />
        <h1 id="admins-only-title" className="mt-6 font-display text-[32px] leading-tight md:text-[40px]">
          Analytics is for admins only
        </h1>
        <p className="mt-4 text-[16px] leading-relaxed text-muted">
          You&apos;re signed in as <strong className="font-semibold text-ink">{me.user.email}</strong>, a customer account at{" "}
          {me.org.name}. The analytics chat shows business figures for {me.org.name}&apos;s staff, so it isn&apos;t available to
          customer accounts.
        </p>
        <Link
          href={`/a/${me.org.slug}/chat`}
          className="mt-8 inline-block whitespace-nowrap rounded-[var(--radius-control)] bg-brand px-5 py-3 text-center font-medium text-white"
        >
          Back to the chat
        </Link>
      </section>
    </main>
  );
}
