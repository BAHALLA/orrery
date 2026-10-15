import { describe, expect, it } from "vitest";
import { parseFrames, readSse } from "./sse";

function streamOf(chunks: (string | Uint8Array)[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(typeof c === "string" ? encoder.encode(c) : c);
      controller.close();
    },
  });
}

async function collect(body: ReadableStream<Uint8Array>) {
  const out = [];
  for await (const frame of readSse(body)) out.push(frame);
  return out;
}

describe("parseFrames", () => {
  it("splits complete frames and keeps the remainder", () => {
    const { frames, rest } = parseFrames('event: a\ndata: {"x":1}\n\nevent: b\ndata: {"y"');
    expect(frames).toEqual([{ event: "a", data: '{"x":1}' }]);
    expect(rest).toBe('event: b\ndata: {"y"');
  });

  it("skips comment lines (the server's keepalives)", () => {
    expect(parseFrames(": keepalive\n\nevent: a\ndata: 1\n\n").frames).toEqual([
      { event: "a", data: "1" },
    ]);
  });

  it("joins multi-line data and accepts CRLF endings", () => {
    expect(parseFrames("data: one\r\ndata: two\r\n\r\n").frames).toEqual([
      { event: "message", data: "one\ntwo" },
    ]);
  });

  it("strips exactly one space after the colon", () => {
    expect(parseFrames("data:  padded\n\n").frames[0].data).toBe(" padded");
  });
});

describe("readSse", () => {
  it("reassembles frames split across chunks", async () => {
    const frames = await collect(streamOf(["event: del", 'ta\ndata: {"te', 'xt":"hi"}\n', "\n"]));
    expect(frames).toEqual([{ event: "delta", data: '{"text":"hi"}' }]);
  });

  it("keeps a multi-byte character split between chunks intact", async () => {
    const bytes = new TextEncoder().encode("event: delta\ndata: café\n\n");
    const cut = bytes.indexOf(0xc3) + 1; // inside the two-byte "é"
    const frames = await collect(streamOf([bytes.slice(0, cut), bytes.slice(cut)]));
    expect(frames[0].data).toBe("café");
  });

  it("yields a final frame that lacks its trailing blank line", async () => {
    expect(await collect(streamOf(["event: done\ndata: {}"]))).toEqual([
      { event: "done", data: "{}" },
    ]);
  });
});
