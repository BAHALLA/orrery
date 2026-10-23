import { memo, type ComponentPropsWithoutRef, type ReactNode } from "react";
import type { Components, Options } from "react-markdown";
import Markdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import { common, createLowlight } from "lowlight";
import { CopyButton } from "./CopyButton";

// Only the languages an SRE actually sees in these answers. `lowlight`'s
// `common` set is ~35 grammars and dominates the bundle; this keeps the
// highlighter well inside the 700 kB chunk budget set in vite.config.ts.
const lowlight = createLowlight({
  bash: common.bash,
  json: common.json,
  yaml: common.yaml,
  python: common.python,
  sql: common.sql,
  diff: common.diff,
  javascript: common.javascript,
  typescript: common.typescript,
  xml: common.xml,
});

/** Recover the raw text of a highlighted code block for the clipboard. */
function textOf(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (typeof node === "object" && "props" in node) {
    return textOf((node.props as { children?: ReactNode }).children);
  }
  return "";
}

function Pre({ children, ...props }: ComponentPropsWithoutRef<"pre">) {
  // The copy target is the block's text, not the highlighted markup — the
  // whole point is pasting a command into a shell.
  const code = textOf(children).replace(/\n$/, "");
  return (
    <div className="group relative">
      <div className="absolute top-1.5 right-1.5 opacity-0 transition group-focus-within:opacity-100 group-hover:opacity-100 pointer-coarse:opacity-100">
        <CopyButton
          value={code}
          label="Copy code block"
          className="bg-slate-800/80 text-slate-300 hover:bg-slate-700 hover:text-white"
        />
      </div>
      <pre {...props}>{children}</pre>
    </div>
  );
}

/**
 * Agent replies rendered as markdown.
 *
 * `react-markdown` builds React elements rather than setting innerHTML, so no
 * sanitizer is needed — and `rehypeHighlight` is given an explicit language set
 * so unknown languages degrade to plain text instead of guessing (auto-detect
 * on a short log line is usually wrong and always costs).
 */
// Module constants, not inline literals: react-markdown rebuilds its unified
// processor whenever these props change identity, and a streamed answer
// re-renders this component once per frame.
const REMARK_PLUGINS: Options["remarkPlugins"] = [remarkGfm];
const REHYPE_PLUGINS: Options["rehypePlugins"] = [[rehypeHighlight, { detect: false, lowlight }]];
const COMPONENTS: Components = { pre: Pre };

export const AgentMarkdown = memo(function AgentMarkdown({ text }: { text: string }) {
  return (
    // Inline code as a quiet pill. The typography plugin's default wraps it in
    // literal backticks (::before/::after), which read as unrendered markdown.
    <div className="prose prose-sm dark:prose-invert prose-pre:bg-slate-900 prose-pre:text-slate-100 prose-code:rounded prose-code:bg-slate-200/70 prose-code:px-1 prose-code:py-0.5 prose-code:font-medium prose-code:before:content-none prose-code:after:content-none dark:prose-code:bg-slate-800 prose-pre:prose-code:bg-transparent prose-pre:prose-code:p-0 max-w-none">
      <Markdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        components={COMPONENTS}
      >
        {text}
      </Markdown>
    </div>
  );
});
