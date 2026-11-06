// The org's emoji in a brand-coloured seal: the one mark every screen shares.
export function OrgSeal({ emoji, size = "md" }: { emoji: string; size?: "sm" | "md" | "lg" }) {
  const dims = { sm: "h-8 w-8 text-base", md: "h-11 w-11 text-xl", lg: "h-16 w-16 text-3xl md:h-20 md:w-20 md:text-4xl" }[size];
  return (
    <span
      aria-hidden
      className={`${dims} inline-flex shrink-0 items-center justify-center rounded-full bg-brand ring-4 ring-brand-tint`}
    >
      <span className="leading-none">{emoji}</span>
    </span>
  );
}
