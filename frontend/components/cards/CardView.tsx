"use client";

import { MetricCard } from "@/components/cards/MetricCard";
import { ProposalCard } from "@/components/cards/ProposalCard";
import { BookingList, OpeningHours, OrderList, OrderStatus, Outcome, ProductList, ServiceList, StockIssue } from "@/components/cards/ReadCards";
import { SlotList } from "@/components/cards/SlotList";
import type { Card } from "@/lib/types";

/** One card from the stream or from history, picked by its `type`. */
export function CardView({ card }: { card: Card }) {
  switch (card.type) {
    case "metric":
      return <MetricCard card={card} />;
    case "order_summary":
    case "booking_summary":
      return <ProposalCard card={card} />;
    case "slot_list":
    case "slot_alternatives":
      return <SlotList card={card} />;
    case "order_status":
      return <OrderStatus card={card} />;
    case "order_list":
      return <OrderList card={card} />;
    case "booking_list":
      return <BookingList card={card} />;
    case "product_list":
      return <ProductList card={card} />;
    case "stock_issue":
      return <StockIssue card={card} />;
    case "service_list":
      return <ServiceList card={card} />;
    case "opening_hours":
      return <OpeningHours card={card} />;
    case "order_confirmed":
    case "order_updated":
    case "order_cancelled":
    case "booking_confirmed":
    case "booking_rescheduled":
    case "booking_cancelled":
      return <Outcome card={card} />;
    default:
      // A card type this UI doesn't know yet: say so quietly rather than show nothing.
      return <p data-testid="card-unknown" className="mt-3 text-[13px] text-muted">(This reply has a card this version can&apos;t show.)</p>;
  }
}
