// Inline Lucide-style icon registry for the slash-command picker.
// Adding the lucide-react package would pull in a full icon set; instead
// each gamesystem references icons by name and we resolve them here.
// Path data taken from lucide.dev (ISC license).

import React from 'react';

interface IconProps {
  size?: number;
  color?: string;
  strokeWidth?: number;
}

const I: React.FC<IconProps & { children: React.ReactNode }> = ({
  size = 16,
  color = 'currentColor',
  strokeWidth = 2,
  children,
}) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke={color}
    strokeWidth={strokeWidth}
    strokeLinecap="round"
    strokeLinejoin="round"
    style={{ flexShrink: 0 }}
  >
    {children}
  </svg>
);

export const SlashIcon: React.FC<{ name: string; size?: number; color?: string }> = ({
  name,
  size,
  color,
}) => {
  const props = { size, color };
  switch (name) {
    case 'MessageSquare':
      return (
        <I {...props}>
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
        </I>
      );
    case 'Phone':
      return (
        <I {...props}>
          <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z" />
        </I>
      );
    case 'Users':
      return (
        <I {...props}>
          <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
          <circle cx="9" cy="7" r="4" />
          <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
          <path d="M16 3.13a4 4 0 0 1 0 7.75" />
        </I>
      );
    case 'Video':
      return (
        <I {...props}>
          <polygon points="23 7 16 12 23 17 23 7" />
          <rect x="1" y="5" width="15" height="14" rx="2" ry="2" />
        </I>
      );
    case 'Check':
      return (
        <I {...props}>
          <polyline points="20 6 9 17 4 12" />
        </I>
      );
    case 'X':
      return (
        <I {...props}>
          <line x1="18" y1="6" x2="6" y2="18" />
          <line x1="6" y1="6" x2="18" y2="18" />
        </I>
      );
    case 'CheckCircle':
      return (
        <I {...props}>
          <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
          <polyline points="22 4 12 14.01 9 11.01" />
        </I>
      );
    case 'Sparkles':
      return (
        <I {...props}>
          <path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3z" />
          <path d="M5 3v4" />
          <path d="M3 5h4" />
          <path d="M19 17v4" />
          <path d="M17 19h4" />
        </I>
      );
    case 'ClipboardEdit':
      return (
        <I {...props}>
          <path d="M8 2h8a2 2 0 0 1 2 2v2H6V4a2 2 0 0 1 2-2z" />
          <path d="M14 2v4H10V2" />
          <path d="M21 12V6a2 2 0 0 0-2-2h-1" />
          <path d="M5 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h7" />
          <path d="M18.4 12.6a2.1 2.1 0 1 1 3 3L17 20l-4 1 1-4z" />
        </I>
      );
    case 'Layers':
      return (
        <I {...props}>
          <polygon points="12 2 2 7 12 12 22 7 12 2" />
          <polyline points="2 17 12 22 22 17" />
          <polyline points="2 12 12 17 22 12" />
        </I>
      );
    case 'Forward':
      return (
        <I {...props}>
          <polygon points="13 19 22 12 13 5 13 19" />
          <polygon points="2 19 11 12 2 5 2 19" />
        </I>
      );
    case 'FolderTree':
      return (
        <I {...props}>
          <path d="M13 10h7a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1h-2.5a1 1 0 0 1-.8-.4l-.9-1.2A1 1 0 0 0 15 3h-2a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1z" />
          <path d="M13 21h7a1 1 0 0 0 1-1v-3a1 1 0 0 0-1-1h-2.88a1 1 0 0 1-.9-.55l-.44-.9a1 1 0 0 0-.9-.55H13a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1z" />
          <path d="M3 3v2c0 1.1.9 2 2 2h3" />
          <path d="M3 3v13c0 1.1.9 2 2 2h3" />
        </I>
      );
    case 'BookOpen':
      return (
        <I {...props}>
          <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" />
          <path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
        </I>
      );
    default:
      // Fallback: a simple slash glyph
      return (
        <I {...props}>
          <line x1="19" y1="5" x2="5" y2="19" />
        </I>
      );
  }
};
