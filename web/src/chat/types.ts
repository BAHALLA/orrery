import type { Trace } from "./trace";

/** A single message rendered in the conversation. */
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  at: number;
  /**
   * What the agent did to produce this reply (tool calls, delegations to
   * specialists), as streamed live. Absent for messages reloaded from the
   * session store, which keeps the transcript but not the progress stream.
   */
  trace?: Trace;
  /** True while the reply is still being written. */
  streaming?: boolean;
  /** The turn was abandoned client-side before its answer arrived. */
  interrupted?: boolean;
  /** Wall time from send to final answer, once finished. */
  durationMs?: number;
}

/** Non-terminal, per-message error surfaced inline in the transcript. */
export interface ChatError {
  message: string;
  /** True when re-authenticating would likely fix it. */
  isAuth: boolean;
  /** True when sending the same message again could plausibly work. */
  canRetry: boolean;
  /** Seconds to wait before retrying, when the server said so (429). */
  retryAfter: number | null;
}
