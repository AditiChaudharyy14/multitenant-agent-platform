// Shapes returned by the FastAPI backend (see backend/app/api and backend/app/auth).

export type Vertical = "store" | "booking";

export interface Branding {
  primary_color: string;
  secondary_color: string;
  logo_emoji: string;
  agent_name: string;
  greeting: string;
}

export interface PublicOrg {
  slug: string;
  name: string;
  vertical: Vertical;
  branding: Branding;
}

export interface User {
  id: string;
  email: string;
  display_name: string;
  role: "customer" | "admin";
}

export interface Me {
  user: User;
  org: PublicOrg;
}

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface Citation {
  id: string; // doc_id#section
  title: string; // document title
}

export interface Card {
  type: string;
  [key: string]: unknown;
}

export type MessageStatus = "complete" | "stopped" | "error" | "streaming";

export interface StoredMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: Exclude<MessageStatus, "streaming">;
  citations: Citation[] | null;
  cards: Card[] | null;
  created_at: string;
}

export interface KbSource {
  ref: string;
  doc_id: string;
  doc_title: string;
  section: string;
  content: string;
}
