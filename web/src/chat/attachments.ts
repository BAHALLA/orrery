import type { AttachmentsInfo } from "../api/types";
import type { AttachmentRef } from "./types";

/**
 * Attachments on the client (AEP-030).
 *
 * The server never stores a file: the conversation keeps one placeholder line
 * per attachment, and `splitAttachments` turns those lines back into chips when
 * a transcript is reloaded. `checkFiles` is a courtesy, so an obviously wrong
 * file is refused before it is uploaded; the server checks every file again,
 * by content.
 */

const PLACEHOLDER = /^\[attachment [0-9a-f]{8}: (.+?) · (.+?) · (.+?) — [^\]]*\]$/;

/** Types the server will refuse anyway: say so before uploading them. */
const REFUSED = /^(application\/(pdf|zip|x-|vnd\.|octet-stream$)|audio\/|video\/)/;
const REFUSED_EXT = /\.(pdf|zip|gz|tgz|7z|rar|docx?|xlsx?|pptx?|exe|dmg|mp[34]|mov|avi)$/i;

/** Extensions offered in the file picker. Others can still be dropped or pasted. */
export const PICKER_ACCEPT =
  ".txt,.log,.out,.err,.json,.yaml,.yml,.toml,.ini,.conf,.cfg,.env.example,.csv,.tsv,.md,.xml,.sql,.sh,.py,.tf";

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  // One decimal while it matters: a 1.5 KB limit must not read as "1 KB".
  if (n < 10 * 1024) return `${(n / 1024).toFixed(1).replace(/\.0$/, "")} KB`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** A user message's text, with its placeholder lines lifted out as chips. */
export function splitAttachments(text: string): { text: string; attachments: AttachmentRef[] } {
  const attachments: AttachmentRef[] = [];
  const kept: string[] = [];
  for (const line of text.split("\n")) {
    const m = PLACEHOLDER.exec(line);
    if (m) attachments.push({ name: m[1], type: m[2], size: m[3] });
    else kept.push(line);
  }
  return attachments.length ? { text: kept.join("\n").trim(), attachments } : { text, attachments };
}

export function refsFor(files: File[]): AttachmentRef[] {
  return files.map((f) => ({ name: f.name, type: f.type || "file", size: formatBytes(f.size) }));
}

/** Add *incoming* to *current* within the server's limits; explain each refusal. */
export function checkFiles(
  current: File[],
  incoming: File[],
  info: AttachmentsInfo,
): { files: File[]; errors: string[] } {
  const files = [...current];
  const errors: string[] = [];
  let total = files.reduce((sum, f) => sum + f.size, 0);
  for (const file of incoming) {
    const isImage = file.type.startsWith("image/");
    if (files.some((f) => f.name === file.name && f.size === file.size)) continue; // dropped twice
    if (files.length >= info.max_files) {
      errors.push(`At most ${info.max_files} files per message.`);
      break;
    }
    if (file.size === 0) {
      errors.push(`${file.name} is empty.`);
    } else if (file.size > info.max_file_bytes) {
      errors.push(`${file.name} is over the ${formatBytes(info.max_file_bytes)} limit.`);
    } else if (total + file.size > info.max_total_bytes) {
      errors.push(`${file.name} would take the message over ${formatBytes(info.max_total_bytes)}.`);
    } else if (isImage && !info.image_types.includes(file.type)) {
      errors.push(
        info.images
          ? `${file.name}: use PNG, JPEG, GIF or WebP.`
          : `${file.name}: this deployment doesn't accept images.`,
      );
    } else if (!isImage && (REFUSED.test(file.type) || REFUSED_EXT.test(file.name))) {
      errors.push(`${file.name}: only images and text files are supported.`);
    } else {
      files.push(file);
      total += file.size;
    }
  }
  return { files, errors };
}
