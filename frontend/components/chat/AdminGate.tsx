"use client";

import type { ReactNode } from "react";

import { AdminsOnlyScreen } from "@/components/chat/AdminsOnlyScreen";
import { useMe } from "@/components/chat/ChatGate";

/** Below ChatGate: a customer gets the "admins only" screen and nothing under it mounts, so
 * no admin endpoint is ever requested. (The server enforces this too: 403.) */
export function AdminGate({ children }: { children: ReactNode }) {
  const me = useMe();
  if (me.user.role !== "admin") return <AdminsOnlyScreen me={me} />;
  return <>{children}</>;
}
