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

// ── Cards and panels. These mirror the payloads built in backend/app/domain/*/service.py. ──

export type ProposalState = "open" | "confirmed" | "superseded" | "expired" | "rejected";

export interface ProposalInfo {
  proposal_id: string;
  status: ProposalState;
  expires_at: string; // ISO
}

export interface OrderRow {
  order_number: string;
  status: string;
  total_display: string;
  placed_at: string;
  units: number;
}

export interface BookingRow {
  booking_number: string;
  service: string;
  practitioner: string;
  date: string;
  time: string;
  when: string;
  status: string;
  price_display: string;
  cancellation_fee_display: string | null;
}

export interface MyOrders {
  orders: OrderRow[];
}
export interface MyBookings {
  upcoming: BookingRow[];
  past: BookingRow[];
}

/** What a card button sends: exactly the body the server's action models accept. */
export type CardAction = { type: string; [key: string]: unknown };
