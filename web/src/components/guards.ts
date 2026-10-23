import type { ToolGuard } from "../api/types";
import { toneChip, toneSolid } from "./severity";

/** Display order: the tools that need the most care first. */
export const GUARD_ORDER: readonly ToolGuard[] = ["destructive", "confirm", "dynamic", "read"];

export const guardLabel: Record<ToolGuard, string> = {
  read: "Read-only",
  confirm: "Needs approval",
  destructive: "Destructive",
  dynamic: "MCP, per tool",
};

export const guardTitle: Record<ToolGuard, string> = {
  read: "Read-only — any role",
  confirm: "Changes state — operator, after approval",
  destructive: "Destructive — admin, after approval",
  dynamic: "MCP toolset — each tool classified when discovered",
};

/** One small coloured dot per guard: enough to scan by, quiet enough to read past. */
export const guardDot: Record<ToolGuard, string> = {
  read: toneSolid.neutral,
  confirm: toneSolid.guarded,
  destructive: toneSolid.crit,
  dynamic: toneSolid.running,
};

export const guardChip: Record<ToolGuard, string> = {
  read: toneChip.neutral,
  confirm: toneChip.guarded,
  destructive: toneChip.crit,
  dynamic: toneChip.running,
};
