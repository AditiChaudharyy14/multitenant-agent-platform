import type { Metadata } from "next";
import { cookies } from "next/headers";
import { notFound, redirect } from "next/navigation";

import { AuthPanel } from "@/components/auth/AuthPanel";
import { OrgSeal } from "@/components/OrgSeal";
import { fetchPublicOrg, fetchSessionMe } from "@/lib/api";
import { brandStyle } from "@/lib/brand";

type Params = { params: Promise<{ slug: string }>; searchParams?: Promise<{ reason?: string }> };

export async function generateMetadata({ params }: Params): Promise<Metadata> {
  const org = await fetchPublicOrg((await params).slug);
  return { title: org ? `${org.name}: chat with ${org.branding.agent_name}` : "Not found" };
}

// The slug only picks the branding and which org to log in or sign up to. It never
// establishes identity: that comes from the session the backend sets.
export default async function OrgLanding({ params, searchParams }: Params) {
  const { slug } = await params;
  // Set by the chat when the session ended mid-conversation (a 401 from the API).
  const expired = (await searchParams)?.reason === "expired";
  const org = await fetchPublicOrg(slug);
  if (!org) notFound();
  // Checked here rather than in the browser, so an anonymous visit makes no failing request.
  const me = await fetchSessionMe((await cookies()).get("sid")?.value);
  if (me?.org.slug === org.slug) redirect(`/a/${org.slug}/chat`); // already signed in here

  return (
    <main style={brandStyle(org)} className="min-h-dvh bg-brand-soft">
      <div className="mx-auto grid min-h-dvh max-w-6xl gap-10 px-4 py-10 sm:px-8 md:grid-cols-[1.15fr_1fr] md:items-center md:gap-16 md:py-16">
        <section className="flex flex-col gap-8">
          <div className="flex items-center gap-4">
            <OrgSeal emoji={org.branding.logo_emoji} size="lg" />
            <h1 className="font-display text-[32px] leading-tight text-brand md:text-[48px]">{org.name}</h1>
          </div>

          {/* The greeting, set as the first thing the agent has already said. */}
          <figure className="max-w-xl">
            <blockquote className="relative rounded-[var(--radius-bubble)] rounded-tl-none bg-paper px-6 py-5 font-display text-[20px] leading-snug text-ink shadow-[0_1px_0_var(--rule)] md:text-[28px]">
              {org.branding.greeting}
            </blockquote>
            <figcaption className="mt-3 text-sm text-muted">
              {org.branding.agent_name}, {org.vertical === "store" ? "your shop assistant" : "your booking assistant"}
            </figcaption>
          </figure>
        </section>

        {expired && (
          <p role="status" data-testid="session-expired" className="rounded-[var(--radius-control)] bg-paper px-4 py-3 text-sm text-ink">
            Your session expired. Please log in again.
          </p>
        )}
        <AuthPanel
          slug={org.slug}
          orgName={org.name}
          agentName={org.branding.agent_name}
          signedInElsewhere={me ? me.org.name : null}
        />
      </div>
    </main>
  );
}
