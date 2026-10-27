/**
 * SHA-256 of *text* as UTF-8, in hex: what the server's `digest` is.
 * `null` where the browser has no Web Crypto (a page served over plain http
 * to a non-local host), so the caller can say it could not check.
 */
export async function sha256Hex(text: string): Promise<string | null> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) return null;
  const hash = await subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(hash), (b) => b.toString(16).padStart(2, "0")).join("");
}
