"use client";

import { useParams } from "next/navigation";

import { ChatScreen } from "@/components/chat/ChatScreen";
import { useMe } from "@/components/chat/ChatGate";

// One page for /chat (new conversation) and /chat/<id>, so starting a conversation can
// update the URL without remounting the screen mid-stream.
export default function ChatPage() {
  const me = useMe();
  const params = useParams<{ conversation?: string[] }>();
  return <ChatScreen me={me} routeConversationId={params.conversation?.[0] ?? null} />;
}
