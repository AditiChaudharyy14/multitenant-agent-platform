import type { ReactNode } from "react";

import { ChatGate } from "@/components/chat/ChatGate";

export default async function ChatLayout({ children, params }: { children: ReactNode; params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  return <ChatGate slug={slug}>{children}</ChatGate>;
}
