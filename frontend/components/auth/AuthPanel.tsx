"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useId, useState } from "react";

import { api, ApiError } from "@/lib/api";
import type { Me } from "@/lib/types";

type Mode = "login" | "signup";

export function AuthPanel({ slug, orgName, agentName }: { slug: string; orgName: string; agentName: string }) {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [signedInElsewhere, setSignedInElsewhere] = useState<Me | null>(null);
  const ids = useId();
  const chatUrl = `/a/${slug}/chat`;

  // Already signed in to this org: go straight to the chat.
  useEffect(() => {
    api
      .me()
      .then((me) => (me.org.slug === slug ? router.replace(chatUrl) : setSignedInElsewhere(me)))
      .catch(() => undefined);
  }, [slug, chatUrl, router]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "login") await api.login(slug, email, password);
      else await api.signup(slug, email, password, name);
      router.replace(chatUrl);
    } catch (err) {
      setError(messageFor(err, mode));
      setBusy(false);
    }
  }

  const tab = (m: Mode, label: string) => (
    <button
      type="button"
      role="tab"
      aria-selected={mode === m}
      id={`${ids}-${m}`}
      onClick={() => {
        setMode(m);
        setError(null);
      }}
      className={`flex-1 rounded-[var(--radius-control)] px-3 py-2 text-sm font-medium transition-colors ${
        mode === m ? "bg-paper text-ink shadow-sm" : "text-muted hover:text-ink"
      }`}
    >
      {label}
    </button>
  );

  const field = "w-full rounded-[var(--radius-control)] border border-rule bg-white px-3 py-2.5 text-[16px] text-ink";

  return (
    <section aria-label="Log in or sign up" className="rounded-[var(--radius-bubble)] border border-rule bg-paper p-5 sm:p-7">
      <h2 className="text-[20px] font-semibold">Talk to {agentName}</h2>
      <p className="mt-1 text-sm text-muted">Your {orgName} account keeps your chats, orders and bookings together.</p>

      {signedInElsewhere && (
        <p className="mt-4 rounded-[var(--radius-control)] bg-brand-tint px-3 py-2 text-sm">
          You&apos;re signed in to {signedInElsewhere.org.name}. Log in below to use a {orgName} account instead.
        </p>
      )}

      <div role="tablist" aria-label="Account" className="mt-5 flex gap-1 rounded-[8px] bg-brand-soft p-1">
        {tab("login", "Log in")}
        {tab("signup", "Sign up")}
      </div>

      <form onSubmit={submit} aria-labelledby={`${ids}-${mode}`} className="mt-5 flex flex-col gap-4" noValidate={false}>
        {mode === "signup" && (
          <label className="flex flex-col gap-1.5 text-sm font-medium">
            Your name
            <input className={field} value={name} onChange={(e) => setName(e.target.value)} required maxLength={80} autoComplete="name" />
          </label>
        )}
        <label className="flex flex-col gap-1.5 text-sm font-medium">
          Email
          <input className={field} type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email" />
        </label>
        <label className="flex flex-col gap-1.5 text-sm font-medium">
          Password
          <input
            className={field}
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            minLength={mode === "signup" ? 10 : undefined}
            autoComplete={mode === "signup" ? "new-password" : "current-password"}
          />
          {mode === "signup" && <span className="font-normal text-muted">At least 10 characters.</span>}
        </label>

        {error && (
          <p role="alert" className="text-sm text-danger">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy}
          className="rounded-[var(--radius-control)] bg-brand px-4 py-2.5 font-medium text-white disabled:opacity-60"
        >
          {busy ? "One moment…" : mode === "login" ? "Log in" : "Create account"}
        </button>
      </form>
    </section>
  );
}

function messageFor(err: unknown, mode: Mode): string {
  if (!(err instanceof ApiError)) return "Couldn't reach the server. Check your connection and try again.";
  if (err.status === 429) return `Too many attempts. Try again in ${err.retryAfter ?? 60}s.`;
  // 401 is one message for a wrong password, an unknown email or another org's account.
  if (err.status === 401) return "Invalid email or password.";
  if (err.status === 409) return "That email already has an account here. Log in instead.";
  if (err.status === 422) return mode === "signup" ? "Check your details: a valid email and a password of at least 10 characters." : "Enter your email and password.";
  return "Something went wrong. Please try again.";
}
