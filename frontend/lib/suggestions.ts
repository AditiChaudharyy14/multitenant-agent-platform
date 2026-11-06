import type { Vertical } from "./types";

// Starter questions for the empty chat. Keyed on the org's vertical, never its slug, so a
// third org gets sensible suggestions from config alone.
export const SUGGESTIONS: Record<Vertical, string[]> = {
  store: ["How long does delivery take?", "Which green teas do you have in stock?", "Where is my latest order?"],
  booking: [
    "Do I need to book in advance?",
    "What times are free for a massage tomorrow afternoon?",
    "What is your cancellation policy?",
  ],
};
