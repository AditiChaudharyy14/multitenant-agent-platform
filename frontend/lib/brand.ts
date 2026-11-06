import type { CSSProperties } from "react";

import type { PublicOrg } from "./types";

/** The org's colours as CSS variables; globals.css derives the tints from them. */
export function brandStyle(org: PublicOrg): CSSProperties {
  return {
    "--brand": org.branding.primary_color,
    "--brand-2": org.branding.secondary_color,
  } as CSSProperties;
}

/** "how-long-does-delivery-take" → "How long does delivery take" */
export function humanizeSection(section: string): string {
  const words = section.replace(/-\d+$/, "").replace(/-/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}
