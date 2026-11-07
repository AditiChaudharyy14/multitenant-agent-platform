"use client";

import { useParams } from "next/navigation";

import { ChatScreen } from "@/components/chat/ChatScreen";
import { useMe } from "@/components/chat/ChatGate";

// The analytics chat: the same screen as the customer chat, on the admin API.
export default function AdminChatPage() {
  const me = useMe();
  const params = useParams<{ conversation?: string[] }>();
  return <ChatScreen me={me} mode="admin" routeConversationId={params.conversation?.[0] ?? null} />;
}
