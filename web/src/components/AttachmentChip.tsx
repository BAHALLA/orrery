import type { AttachmentRef } from "../chat/types";
import { CloseIcon, FileIcon, ImageIcon } from "./icons";

interface Props {
  attachment: AttachmentRef;
  /** "composer" on the white input, "bubble" on the brand-coloured message. */
  tone: "composer" | "bubble";
  onRemove?: () => void;
}

/** One attachment as a chip: icon, name, size. No preview — the content is
 * never kept, and the page's CSP allows no blob: images. */
export function AttachmentChip({ attachment, tone, onRemove }: Props) {
  const Icon = attachment.type.startsWith("image/") ? ImageIcon : FileIcon;
  return (
    <span
      className={`inline-flex max-w-full items-center gap-1.5 rounded-lg py-1 pr-1.5 pl-2 text-xs ${
        tone === "bubble"
          ? "bg-white/15 text-white"
          : "border border-slate-200 bg-slate-50 text-slate-700 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
      }`}
      title={`${attachment.name} · ${attachment.type} · ${attachment.size}`}
    >
      <Icon size={14} className="shrink-0 opacity-80" />
      <span className="truncate font-medium">{attachment.name}</span>
      <span className="shrink-0 opacity-70">{attachment.size}</span>
      {onRemove ? (
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Remove ${attachment.name}`}
          className="ml-0.5 rounded p-0.5 text-slate-500 hover:bg-slate-200 hover:text-slate-700 dark:hover:bg-slate-700 dark:hover:text-slate-100"
        >
          <CloseIcon size={12} />
        </button>
      ) : null}
    </span>
  );
}
