import type { ReactNode, SVGProps } from "react";
import type { NodeKind } from "./types";

/** The Studio's glyphs, in the console's icon style (24×24, 1.75 stroke), kept in its chunk. */
type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function Icon({ size = 16, children, ...props }: IconProps & { children: ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      {children}
    </svg>
  );
}

export const PlayIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M7 5v14l11-7z" />
  </Icon>
);

export const UploadIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 16V4M7 9l5-5 5 5M5 20h14" />
  </Icon>
);

export const DownloadIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 4v12M7 11l5 5 5-5M5 20h14" />
  </Icon>
);

export const GridIcon = (p: IconProps) => (
  <Icon {...p}>
    <rect x="4" y="4" width="6" height="6" rx="1.5" />
    <rect x="14" y="4" width="6" height="6" rx="1.5" />
    <rect x="4" y="14" width="6" height="6" rx="1.5" />
    <rect x="14" y="14" width="6" height="6" rx="1.5" />
  </Icon>
);

export const LayoutIcon = (p: IconProps) => (
  <Icon {...p}>
    <rect x="3" y="9" width="5" height="6" rx="1.5" />
    <rect x="16" y="4" width="5" height="6" rx="1.5" />
    <rect x="16" y="14" width="5" height="6" rx="1.5" />
    <path d="M8 12h4M12 7v10M12 7h4M12 17h4" />
  </Icon>
);

const kindPaths: Record<NodeKind, ReactNode> = {
  llm_agent: (
    <>
      <rect x="5" y="7" width="14" height="11" rx="3" />
      <path d="M12 7V4M9.5 12h.01M14.5 12h.01M9.5 15h5" />
    </>
  ),
  remote_a2a: (
    <>
      <circle cx="6" cy="12" r="2.5" />
      <circle cx="18" cy="12" r="2.5" />
      <path d="M8.5 10.5c2-1.5 5-1.5 7 0M8.5 13.5c2 1.5 5 1.5 7 0" />
    </>
  ),
  router: <path d="M4 12h6l4-6h6M10 12l4 6h6M17 3l3 3-3 3M17 15l3 3-3 3" />,
  parallel: <path d="M4 12h4M8 12l4-6h8M8 12h12M8 12l4 6h8" />,
  join: <path d="M4 6h8l4 6h4M4 12h16M4 18h8l4-6" />,
  supervisor: (
    <>
      <circle cx="12" cy="6" r="2.5" />
      <path d="M12 8.5V12M6 16v-2h12v2M6 16v2M12 12v6M18 16v2" />
    </>
  ),
  human_approval: (
    <>
      <circle cx="12" cy="8" r="3.5" />
      <path d="M5 20c1-4 4-6 7-6s6 2 7 6" />
    </>
  ),
};

export function KindIcon({ kind, ...p }: IconProps & { kind: NodeKind }) {
  return <Icon {...p}>{kindPaths[kind]}</Icon>;
}

export const SaveIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M5 4h11l3 3v13H5z" />
    <path d="M8 4v5h7V4M8 20v-6h8v6" />
  </Icon>
);

export const FolderIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
  </Icon>
);
