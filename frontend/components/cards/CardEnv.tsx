"use client";

import { createContext, useContext } from "react";

import type { CardAction, ProposalInfo } from "@/lib/types";

/** What a card needs from the chat screen: whether a turn is running, a way to press a
 * button (which is just another message), and the live status of each proposal. */
export interface CardEnv {
  busy: boolean;
  send: (action: CardAction, label: string) => void;
  proposals: Record<string, ProposalInfo>;
}

export const CardEnvContext = createContext<CardEnv>({ busy: true, send: () => undefined, proposals: {} });
export const useCardEnv = () => useContext(CardEnvContext);
