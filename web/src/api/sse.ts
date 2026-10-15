/**
 * Minimal Server-Sent Events reader for a `fetch()` response body.
 *
 * `EventSource` can't be used here: it only issues GETs and can't send an
 * `Authorization` header, and the chat turn is an authenticated POST. This
 * implements the part of the SSE wire format the front door emits — `event:`
 * and `data:` fields, blank-line-terminated frames, `:` comment lines (the
 * server's keepalives) — and tolerates chunks that split a frame anywhere,
 * including inside a multi-byte character.
 */

export interface SseFrame {
  event: string;
  data: string;
}

/** Split complete frames off the front of `buffer`; return them and the remainder. */
export function parseFrames(buffer: string): { frames: SseFrame[]; rest: string } {
  const frames: SseFrame[] = [];
  // The spec allows CRLF and CR line endings as well as LF.
  const normalized = buffer.replace(/\r\n?/g, "\n");
  const blocks = normalized.split("\n\n");
  const rest = blocks.pop() ?? "";

  for (const block of blocks) {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line === "" || line.startsWith(":")) continue; // comment / keepalive
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      // One optional space after the colon is part of the syntax, not the value.
      let value = colon === -1 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "event") event = value;
      else if (field === "data") data.push(value);
    }
    if (data.length > 0) frames.push({ event, data: data.join("\n") });
  }
  return { frames, rest };
}

/** Yield each frame of an SSE response body as it arrives. */
export async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<SseFrame> {
  const reader = body.getReader();
  // stream: true keeps a multi-byte character split across chunks intact.
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const { frames, rest } = parseFrames(buffer);
      buffer = rest;
      yield* frames;
    }
    buffer += decoder.decode();
    // A final frame without its trailing blank line is still a frame.
    if (buffer.trim()) yield* parseFrames(`${buffer}\n\n`).frames;
  } finally {
    reader.releaseLock();
  }
}
