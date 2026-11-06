"use client";

import { useRouter } from "next/navigation";
import { createContext, type ReactNode, useContext, useEffect, useState } from "react";

import { WrongOrgScreen } from "@/components/chat/WrongOrgScreen";
import { api, ApiError } from "@/lib/api";
import { brandStyle } from "@/lib/brand";
import type { Me } from "@/lib/types";

const MeContext = createContext<Me | null>(null);

export function useMe(): Me {
  const me = useContext(MeContext);
  if (!me) throw new Error("useMe outside ChatGate");
  return me;
}

/**
 * Who is signed in decides everything below. The only request made here is /api/me (the
 * session's own identity); the URL slug is compared to it, never sent as identity. If the
 * account belongs to another org, nothing for the URL's org is mounted or fetched.
 */
export function ChatGate({ slug, children }: { slug: string; children: ReactNode }) {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let live = true;
    api
      .me()
      .then((m) => live && setMe(m))
      .catch((err) => {
        if (!live) return;
        if (err instanceof ApiError && err.status === 401) router.replace(`/a/${slug}`);
        else setFailed(true);
      });
    return () => {
      live = false;
    };
  }, [slug, router]);

  if (failed) {
    return (
      <main className="mx-auto flex min-h-dvh max-w-md flex-col justify-center gap-3 px-4">
        <h1 className="text-[20px] font-semibold">Couldn&apos;t reach the server</h1>
        <p className="text-muted">Check your connection, then reload the page.</p>
        <button className="self-start rounded-[var(--radius-control)] border border-rule px-4 py-2" onClick={() => location.reload()}>
          Reload
        </button>
      </main>
    );
  }
  if (!me) {
    return (
      <main className="flex min-h-dvh items-center justify-center text-muted" aria-busy>
        Loading…
      </main>
    );
  }
  if (me.org.slug !== slug) return <WrongOrgScreen me={me} requestedSlug={slug} />;
  return (
    <MeContext.Provider value={me}>
      <div style={brandStyle(me.org)}>{children}</div>
    </MeContext.Provider>
  );
}
