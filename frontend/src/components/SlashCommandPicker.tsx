// Slash-command picker overlay. Shows above the chat input when the user
// types `/`, filters as they type, picks the highlighted command on Enter,
// and dismisses on Esc or when the input no longer starts with /.

import React, { useEffect, useMemo, useRef } from 'react';
import { SlashIcon } from './SlashIcons';

export interface SlashCommand {
  name: string;
  description: string;
  icon: string;
  args: { placeholder?: string } | null;
  condition?: string;
}

interface Props {
  commands: SlashCommand[];
  selectedIndex: number;
  onSelectIndex: (i: number) => void;
  onPick: (cmd: SlashCommand) => void; // Enter: pick (sends if argless, inserts otherwise)
  onInsert: (cmd: SlashCommand) => void; // Tab: insert into textarea, no send
}

export const filterCommands = (commands: SlashCommand[], query: string): SlashCommand[] => {
  // query is the raw input (e.g. "/te"). Strip leading slash and lowercase.
  const q = query.replace(/^\//, '').toLowerCase();
  if (!q) return commands;
  // Two-pass ranking: prefix matches first, then substring matches.
  const prefix: SlashCommand[] = [];
  const substring: SlashCommand[] = [];
  for (const cmd of commands) {
    const n = cmd.name.replace(/^\//, '').toLowerCase();
    if (n.startsWith(q)) prefix.push(cmd);
    else if (n.includes(q) || cmd.description.toLowerCase().includes(q)) substring.push(cmd);
  }
  return [...prefix, ...substring];
};

export const SlashCommandPicker: React.FC<Props> = ({
  commands,
  selectedIndex,
  onSelectIndex,
  onPick,
  onInsert,
}) => {
  const listRef = useRef<HTMLDivElement>(null);

  // Auto-scroll the highlighted item into view when navigation moves selection.
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLDivElement>(`[data-idx="${selectedIndex}"]`);
    if (el) el.scrollIntoView({ block: 'nearest' });
  }, [selectedIndex]);

  if (commands.length === 0) return null;

  return (
    <div
      ref={listRef}
      style={{
        position: 'absolute',
        bottom: 'calc(100% + 8px)',
        left: 0,
        right: 0,
        maxWidth: '460px',
        maxHeight: '320px',
        overflowY: 'auto',
        backgroundColor: '#202036',
        border: '1px solid #3a3a5e',
        borderRadius: '10px',
        boxShadow: '0 8px 24px rgba(0, 0, 0, 0.4)',
        padding: '4px',
        zIndex: 50,
      }}
      onMouseDown={(e) => e.preventDefault()}  // Don't blur the textarea when clicking
    >
      {commands.map((cmd, i) => {
        const selected = i === selectedIndex;
        return (
          <div
            key={cmd.name}
            data-idx={i}
            onClick={() => onPick(cmd)}
            onMouseEnter={() => onSelectIndex(i)}
            onDoubleClick={() => onInsert(cmd)}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '10px',
              padding: '8px 12px',
              borderRadius: '6px',
              cursor: 'pointer',
              backgroundColor: selected ? '#2d2d52' : 'transparent',
              transition: 'background-color 0.05s',
            }}
          >
            <span style={{ color: selected ? '#a8a8ff' : '#888', display: 'flex' }}>
              <SlashIcon name={cmd.icon} size={16} />
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 600, color: '#eee', fontSize: '14px' }}>{cmd.name}</div>
              <div
                style={{
                  fontSize: '12px',
                  color: '#999',
                  whiteSpace: 'nowrap',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                }}
              >
                {cmd.description}
                {cmd.args?.placeholder ? (
                  <span style={{ color: '#666', marginLeft: '6px' }}>
                    — {cmd.args.placeholder}
                  </span>
                ) : null}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
};

// Hook helper: given input text and an optional cursor position, decide if the
// picker should be open and what the "query" is.  We only open when the input
// starts with `/` and there's no whitespace before the cursor (so `/text foo`
// doesn't keep the picker open while typing the arg).
export function useSlashPickerState(
  text: string,
  commands: SlashCommand[]
): { open: boolean; filtered: SlashCommand[]; query: string } {
  return useMemo(() => {
    if (!text.startsWith('/') || text.includes(' ') || text.includes('\n')) {
      return { open: false, filtered: [], query: '' };
    }
    return {
      open: true,
      filtered: filterCommands(commands, text),
      query: text,
    };
  }, [text, commands]);
}
