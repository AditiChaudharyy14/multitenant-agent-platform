import type { ReactNode } from "react";

import { AdminGate } from "@/components/chat/AdminGate";
import { ChatGate } from "@/components/chat/ChatGate";

// Same session gate as the chat (wrong org → the "belongs to" screen), then the admin gate.
export default async function AdminLayout({ children, params }: { children: ReactNode; params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  return (
    <ChatGate slug={slug}>
      <AdminGate>{children}</AdminGate>
    </ChatGate>
  );
}
