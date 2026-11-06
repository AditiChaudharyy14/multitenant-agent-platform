import type { Metadata, Viewport } from "next";
import { Instrument_Sans, Young_Serif } from "next/font/google";
import type { ReactNode } from "react";

import "./globals.css";

// Instrument Sans carries the interface; Young Serif is kept for the org's own voice
// (its name and the agent's greeting), so each brand reads as a person, not a dashboard.
const instrument = Instrument_Sans({ subsets: ["latin"], variable: "--font-instrument", display: "swap" });
const youngSerif = Young_Serif({ subsets: ["latin"], weight: "400", variable: "--font-young-serif", display: "swap" });

export const metadata: Metadata = {
  title: "Multi-Tenant Agent Platform",
};

export const viewport: Viewport = { width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${instrument.variable} ${youngSerif.variable}`}>
      <body className="min-h-dvh">{children}</body>
    </html>
  );
}
