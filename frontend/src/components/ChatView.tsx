import React, { useRef, useCallback, useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import { styles } from '../styles';
import { ChatMessage, ModelInfo, convertMathDelimiters, formatTimestamp } from '../types';
import { SlashCommandPicker, SlashCommand, useSlashPickerState } from './SlashCommandPicker';
import { FlakinessBandsModal } from './Modals';

/** Format the in-fiction time/date/location HUD line for an assistant
 * message.  Reads from msg.pipeline_state_after.hud_state — the
 * deterministic backend-tracked snapshot at the moment that message
 * was generated.
 *
 * Mode messages (sex_mode / hack_mode / combat_mode / net_combat_mode /
 * ship_combat_mode) are sealed pipelines that DON'T write back to the
 * main pipeline_state, so they have no hud_state of their own.  For
 * those, we walk backward through `messages` to the most recent
 * assistant message that DOES have a hud_state and inherit it — that's
 * the scene-start time, semantically "this is happening some time
 * during the scene that started at X."
 *
 * Format: "2045-06-12 · 21:43 · Delphi's Apartment, Watson"
 *   - HHMM (4-digit) → HH:MM for readability
 *   - YYYY-MM-DD passed through
 *   - Location passed through
 *   - Middle dots as separators
 */
/** Render a side-agent usage dict in the project-standard "I:X C:Y W:Z O:Q R:R T:N"
 * format (mirrors the main provider's format_token_string).  Mirrors the cache
 * refresh-overlap heuristic: if cache_read and cache_creation are within 10% of
 * each other (a 1h-cache TTL refresh event), we count the larger as T contribution
 * once instead of double-counting.
 *
 * Side-agent usage dicts have an `input_tokens` field that's already the SUM of
 * non-cached + cache_read + cache_creation (per how the side-agent files build it).
 */
function formatUsageString(usage: any): string {
  if (!usage || typeof usage !== 'object') return '';
  const totalInput = usage.input_tokens || 0;
  const cacheRead = usage.cache_read_tokens || 0;
  const cacheWrite = usage.cache_creation_tokens || 0;
  const output = usage.output_tokens || 0;
  const reasoning = usage.reasoning_tokens || 0;
  const nonCached = Math.max(0, totalInput - cacheRead - cacheWrite);
  const r = cacheRead;
  const w = cacheWrite;
  const cachedPortion =
    r > 0 && w > 0 && Math.abs(r - w) < 0.1 * Math.max(r, w)
      ? Math.max(r, w) // refresh-overlap
      : r + w;
  const total = nonCached + cachedPortion + output + reasoning;
  return `I:${nonCached} C:${r} W:${w} O:${output} R:${reasoning} T:${total}`;
}

function _extractHudFromMsg(msg: any): any | null {
  if (!msg || typeof msg !== 'object') return null;
  const psa = msg.pipeline_state_after;
  if (!psa || typeof psa !== 'object') return null;
  const hud = psa.hud_state;
  if (!hud || typeof hud !== 'object') return null;
  return hud;
}

function _formatHudLine(hud: any): string | null {
  if (!hud) return null;
  const date = typeof hud.date === 'string' ? hud.date.trim() : '';
  const rawTime = typeof hud.time === 'string' ? hud.time.trim() : '';
  const location = typeof hud.location === 'string' ? hud.location.trim() : '';
  let time = rawTime;
  if (rawTime && /^\d{3,4}$/.test(rawTime)) {
    const padded = rawTime.padStart(4, '0');
    time = padded.slice(0, 2) + ':' + padded.slice(2);
  }
  const parts = [date, time, location].filter(p => !!p);
  if (parts.length === 0) return null;
  return parts.join(' · ');
}

function getMessageHudLine(msg: any, allMessages: any[], index: number): string | null {
  if (!msg || msg.role !== 'assistant') return null;
  // First try this message's own snapshot.
  let hud = _extractHudFromMsg(msg);
  if (hud) return _formatHudLine(hud);
  // Mode message — inherit from the most recent prior assistant message
  // that has a hud_state (the normal-mode message that opened the scene).
  for (let j = index - 1; j >= 0; j--) {
    const prior = allMessages[j];
    if (!prior || prior.role !== 'assistant') continue;
    hud = _extractHudFromMsg(prior);
    if (hud) return _formatHudLine(hud);
  }
  return null;
}

interface ChatViewProps {
  isMobile: boolean;
  username: string;
  currentChat: string;
  currentProject: string | null;
  viewerCount: number;
  projectGameSystem: string | null;
  availableGameSystems: {id: string, name: string, slash_commands?: SlashCommand[]}[];
  handleProjectGameSystemChange: (v: string) => void;
  availableModels: ModelInfo[];
  selectedModel: string;
  handleModelChange: (v: string) => void;
  anthropicSync: boolean;
  handleAnthropicSyncToggle: () => void;
  handleReloadChat: () => void;
  messagesContainerRef: React.RefObject<HTMLDivElement | null>;
  messagesEndRef: React.RefObject<HTMLDivElement | null>;
  isLoadingMoreMessages: boolean;
  messages: ChatMessage[];
  allMessages: ChatMessage[];
  totalMessages: number;
  contextStartIndex: number;
  editingMessageIndex: number | null;
  editingMessageContent: string;
  setEditingMessageContent: (v: string) => void;
  startEditMessage: (i: number) => void;
  saveEditedMessage: () => void;
  cancelEditMessage: () => void;
  expandedReasoning: Set<number>;
  setExpandedReasoning: React.Dispatch<React.SetStateAction<Set<number>>>;
  getSiblings: (msgs: ChatMessage[], messageId: string) => ChatMessage[];
  switchBranch: (targetId: string) => void;
  deleteMessagePair: (messageIndex: number) => void;
  isLoading: Set<string>;
  pipelineStage: Map<string, {stage: string, status: string}>;
  stagedFiles: {filename: string, content: string}[];
  removeStagedFile: (i: number) => void;
  showAttachMenu: boolean;
  setShowAttachMenu: (v: boolean) => void;
  attachMenuRef: React.RefObject<HTMLDivElement | null>;
  chatFileInputRef: React.RefObject<HTMLInputElement | null>;
  handleChatFileSelect: (e: React.ChangeEvent<HTMLInputElement>) => void;
  isDraggingFile: boolean;
  handleDragOver: (e: React.DragEvent) => void;
  handleDragLeave: (e: React.DragEvent) => void;
  handleDrop: (e: React.DragEvent) => void;
  handleResizeStart: (e: React.MouseEvent) => void;
  newMessage: string;
  setNewMessage: (v: string) => void;
  textareaHeight: number;
  sendMessage: (overrideText?: string) => void;
  updatesText: string;
  onNotesClick: () => void;
  stateNotifications: any[];
  bookmarkingMessageIndex: number | null;
  bookmarkText: string;
  setBookmarkText: (v: string) => void;
  startBookmark: (i: number) => void;
  saveBookmark: () => void;
  cancelBookmark: () => void;
  deleteBookmark: (i: number) => void;
  bookmarkTooltip: {index: number, x: number, y: number} | null;
  setBookmarkTooltip: (v: {index: number, x: number, y: number} | null) => void;
  onSelectArtifact?: (docId: string) => void;
  onDownloadArtifact?: (docId: string) => void;
  onToggleMessageStaged?: (messageId: string, staged: boolean) => void;
  onUnstageAll?: () => void;
  chatGameSystem?: string | null;
  pipelineState?: any;
}

export default function ChatView({
  isMobile,
  username,
  currentChat,
  currentProject,
  viewerCount,
  projectGameSystem,
  availableGameSystems,
  handleProjectGameSystemChange,
  availableModels,
  selectedModel,
  handleModelChange,
  anthropicSync,
  handleAnthropicSyncToggle,
  handleReloadChat,
  messagesContainerRef,
  messagesEndRef,
  isLoadingMoreMessages,
  messages,
  allMessages,
  totalMessages,
  contextStartIndex,
  editingMessageIndex,
  editingMessageContent,
  setEditingMessageContent,
  startEditMessage,
  saveEditedMessage,
  cancelEditMessage,
  expandedReasoning,
  setExpandedReasoning,
  getSiblings,
  switchBranch,
  deleteMessagePair,
  isLoading,
  pipelineStage,
  stagedFiles,
  removeStagedFile,
  showAttachMenu,
  setShowAttachMenu,
  attachMenuRef,
  chatFileInputRef,
  handleChatFileSelect,
  isDraggingFile,
  handleDragOver,
  handleDragLeave,
  handleDrop,
  handleResizeStart,
  newMessage,
  setNewMessage,
  textareaHeight,
  sendMessage,
  updatesText,
  onNotesClick,
  stateNotifications,
  bookmarkingMessageIndex,
  bookmarkText,
  setBookmarkText,
  startBookmark,
  saveBookmark,
  cancelBookmark,
  deleteBookmark,
  bookmarkTooltip,
  setBookmarkTooltip,
  onSelectArtifact,
  onDownloadArtifact,
  onToggleMessageStaged,
  onUnstageAll,
  chatGameSystem,
  pipelineState,
}: ChatViewProps) {
  const tooltipHideTimeout = useRef<NodeJS.Timeout | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // Flakiness-bands review modal — opened from a "Review follow-through" button
  // on the interview-finalize assistant message. Bands are auto-committed
  // server-side already; this is the review-and-adjust surface.
  const [bandsModalOpen, setBandsModalOpen] = useState(false);
  const [bandsModalProposal, setBandsModalProposal] = useState<any>(null);

  // Slash command picker — pulls the per-gamesystem command list from
  // availableGameSystems (backend SSOT) and shows a filtered list when the
  // input starts with "/".
  const slashCommandsForGameSystem: SlashCommand[] = useMemo(() => {
    const id = chatGameSystem || null;
    if (!id) return [];
    const gs = availableGameSystems.find(g => g.id === id);
    return gs?.slash_commands || [];
  }, [chatGameSystem, availableGameSystems]);

  const slashPicker = useSlashPickerState(newMessage, slashCommandsForGameSystem);
  const [slashIdx, setSlashIdx] = useState(0);

  // Reset selection to top whenever the filtered list changes.
  React.useEffect(() => {
    setSlashIdx(0);
  }, [slashPicker.filtered.length, slashPicker.query]);

  const handleSlashPick = useCallback((cmd: SlashCommand) => {
    if (cmd.args) {
      // Argful: insert "/cmd " into the textarea, dismiss picker by adding space.
      // The space causes useSlashPickerState to close (it bails on whitespace).
      setNewMessage(`${cmd.name} `);
      // Refocus and move cursor to end so user can start typing the arg.
      requestAnimationFrame(() => {
        const ta = textareaRef.current;
        if (ta) {
          ta.focus();
          ta.setSelectionRange(ta.value.length, ta.value.length);
        }
      });
    } else {
      // Argless: send immediately. Pass override text directly to avoid the
      // React render race — setNewMessage doesn't flush before sendMessage's
      // closure reads the input state.
      setNewMessage('');
      sendMessage(cmd.name);
    }
  }, [setNewMessage, sendMessage]);

  const handleSlashInsert = useCallback((cmd: SlashCommand) => {
    // Tab: insert into textarea regardless of args. Adds trailing space when
    // the command takes args; otherwise just the bare command.
    setNewMessage(cmd.args ? `${cmd.name} ` : cmd.name);
    requestAnimationFrame(() => {
      const ta = textareaRef.current;
      if (ta) {
        ta.focus();
        ta.setSelectionRange(ta.value.length, ta.value.length);
      }
    });
  }, [setNewMessage]);

  const scheduleTooltipHide = useCallback(() => {
    if (tooltipHideTimeout.current) clearTimeout(tooltipHideTimeout.current);
    tooltipHideTimeout.current = setTimeout(() => setBookmarkTooltip(null), 150);
  }, [setBookmarkTooltip]);

  const cancelTooltipHide = useCallback(() => {
    if (tooltipHideTimeout.current) {
      clearTimeout(tooltipHideTimeout.current);
      tooltipHideTimeout.current = null;
    }
  }, []);

  return (
    <>
      <div style={{...styles.chatHeader, ...(isMobile ? styles.chatHeaderMobile : {})}}>
        <h2 style={styles.chatTitle}>
          {currentChat}
          {(() => {
            // "Currently in interview mode" = the latest message carries the tag.
            // After /finalize, subsequent correspondence messages drop the tag,
            // so the suffix disappears automatically.
            const lastMsg: any = messages.length > 0 ? messages[messages.length - 1] : null;
            return lastMsg?._characters_interview_mode ? (
              <span style={{ color: '#ead9b2', fontWeight: 400, marginLeft: '8px' }}>
                — Interview
              </span>
            ) : null;
          })()}
        </h2>
        {chatGameSystem === 'characters' && pipelineState?.characters_state?.channel && (
          <span
            style={{
              fontSize: '0.7rem',
              fontWeight: 600,
              letterSpacing: '0.06em',
              padding: '3px 8px',
              borderRadius: '10px',
              background: '#1f2a44',
              color: '#9ec5ff',
              border: '1px solid #34466e',
              textTransform: 'uppercase' as const,
              flexShrink: 0,
            }}
            title="Current channel — change with /text, /phone, /inperson, or /video"
          >
            {pipelineState.characters_state.channel}
          </span>
        )}
        {viewerCount > 1 && (
          <span style={styles.viewerCount} title={`${viewerCount} viewers connected`}>
            {viewerCount} viewing
          </span>
        )}
        {currentProject && availableGameSystems.length > 0 && (
          <select
            value={projectGameSystem || 'dnd5e'}
            onChange={(e) => handleProjectGameSystemChange(e.target.value)}
            style={styles.modelSelector}
            title="Select game system"
          >
            {availableGameSystems.map(gs => (
              <option key={gs.id} value={gs.id}>{gs.name}</option>
            ))}
          </select>
        )}
        {availableModels.length > 0 && (
          <select
            value={selectedModel}
            onChange={(e) => handleModelChange(e.target.value)}
            style={styles.modelSelector}
            title="Select AI model"
          >
            {availableModels.map(m => (
              <option key={m.id} value={m.id}>{m.name}</option>
            ))}
          </select>
        )}
        {selectedModel.startsWith('claude') && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexShrink: 0 }}>
            <span style={{ fontSize: '0.75rem', color: '#999', userSelect: 'none' as const }}>Async</span>
            <div
              onClick={handleAnthropicSyncToggle}
              style={{
                width: 40, height: 20, borderRadius: 10,
                background: anthropicSync ? '#4ade80' : '#4a4a6e',
                cursor: 'pointer', position: 'relative' as const,
                transition: 'background 0.2s',
              }}
              title={anthropicSync ? 'Sync mode: prompt caching enabled (cost-efficient for live conversations)' : 'Async mode: prompt caching disabled (cost-efficient for infrequent turns)'}
            >
              <div style={{
                width: 16, height: 16, borderRadius: '50%',
                background: 'white', position: 'absolute' as const,
                top: 2, left: 2,
                transform: anthropicSync ? 'translateX(20px)' : 'translateX(0)',
                transition: 'transform 0.2s',
              }} />
            </div>
            <span style={{ fontSize: '0.75rem', color: '#999', userSelect: 'none' as const }}>Sync</span>
          </div>
        )}
        <button
          onClick={handleReloadChat}
          style={styles.reloadButton}
          title="Reload instructions and files"
        >
          🔄
        </button>
        {chatGameSystem === 'novels' && onUnstageAll && (
          <button
            onClick={onUnstageAll}
            style={{ ...styles.reloadButton, fontSize: '0.68rem', padding: '3px 8px', color: '#888' }}
            title="Exclude all messages from context"
          >
            Unselect All
          </button>
        )}
      </div>

      <div ref={messagesContainerRef} style={styles.messagesContainer}>
        {isLoadingMoreMessages && (
          <div style={styles.loadingMoreMessages}>Loading older messages...</div>
        )}
        {messages.map((msg, i) => {
          const nextMsg = i + 1 < messages.length ? messages[i + 1] : null;
          const prevMsg = i > 0 ? messages[i - 1] : null;
          const hideShipCombatInitUser =
            msg.role === 'user' &&
            !!((msg as any).ship_combat_hidden_init);
          if (hideShipCombatInitUser) {
            const opening = String((nextMsg as any)?.ship_combat_opening_narration || '').trim();
            const openingEmbedded = !!(nextMsg as any)?.ship_combat_opening_embedded;
            return (
              <div key={i} className="message" style={{ ...styles.message, backgroundColor: '#23190f' }}>
                <div style={styles.shipCombatStartBanner}>BEGINNING SHIP COMBAT</div>
                {opening && !openingEmbedded && (
                  <div style={styles.shipCombatOpeningNarration}>
                    <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>
                      {convertMathDelimiters(opening)}
                    </ReactMarkdown>
                  </div>
                )}
              </div>
            );
          }

          // Channel-switch marker — render as a centered dim row showing
          // WHERE in the scroll-back the channel changed. The header chip
          // only shows current mode; without this marker, /phone or /text
          // switches are invisible in chat history.
          const channelSwitch = !!(msg as any).channel_switch;
          if (channelSwitch) {
            const newCh = (msg as any).channel || 'unknown';
            const ts = msg.timestamp ? new Date(msg.timestamp).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : null;
            return (
              <div key={i} style={{
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
                padding: '6px 16px',
                color: '#7a8aa8',
                fontSize: '11px',
                letterSpacing: '0.08em',
                textTransform: 'uppercase' as const,
                userSelect: 'none' as const,
              }}>
                <span style={{ flex: 1, height: '1px', background: '#34466e', opacity: 0.5 }} />
                <span>switched to {newCh}</span>
                {ts && <span style={{ color: '#5a6680', textTransform: 'none', letterSpacing: 0 }}>· {ts}</span>}
                <span style={{ flex: 1, height: '1px', background: '#34466e', opacity: 0.5 }} />
              </div>
            );
          }

          // Busy placeholder — render as a centered dim status row, NOT as a
          // chat bubble. The character was asleep / at work and didn't reply;
          // showing this as her message would read as her saying "[no reply
          // — asleep]" which is nonsense.
          const busyPlaceholder = !!(msg as any).busy_placeholder;
          if (busyPlaceholder && msg.role === 'assistant') {
            const ev = (msg as any).busy_event || {};
            const desc = ev.description || ev.kind || 'unreachable';
            return (
              <div key={i} style={{
                textAlign: 'center',
                color: '#888',
                fontSize: '12px',
                padding: '8px 16px',
                fontStyle: 'italic',
                opacity: 0.75,
              }}>
                💤 no reply — {desc}
                {ev.ends_at && (
                  <span style={{ marginLeft: '8px', fontSize: '11px', color: '#666' }}>
                    (until {new Date(ev.ends_at).toLocaleString()})
                  </span>
                )}
              </div>
            );
          }

          // Determine if this message is in context
          // Map display index to actual backend index
          // totalMessages includes system message, messages.length does not
          // So first displayed message is at backend index (totalMessages - messages.length)
          const firstDisplayedBackendIndex = totalMessages - messages.length;
          const actualBackendIndex = firstDisplayedBackendIndex + i;
          const isInContext = actualBackendIndex >= contextStartIndex;

          // Choose background color based on context, role, and hack mode
          let backgroundColor;
          const isHackMode = !!(msg as any).hack_mode;
          const isShipCombatMode = !!(msg as any).ship_combat_mode;
          const isSexMode = !!(msg as any).sex_mode;
          const isChaseMode = !!(msg as any).chase_mode;
          const isCombatMode = !!(msg as any).combat_mode;
          const isNetCombatMode = !!(msg as any).net_combat_mode;
          const isInterviewMode = !!(msg as any)._characters_interview_mode;
          const isSosUser = msg.role === 'user' && !!(msg as any).is_sos;
          // Check if this message pair is manually unstaged (Novels)
          const isUnstaged = msg.role === 'user' ? msg.staged === false
            : (i > 0 && messages[i - 1]?.role === 'user' && messages[i - 1]?.staged === false);

          if (!isInContext || isUnstaged) {
            // Out of context or manually unstaged: grayed out versions
            backgroundColor = msg.role === 'user' ? '#1f1f35' : '#171728';
          } else if (isNetCombatMode) {
            // NET+meatspace combat: violet/purple tint (mix of matrix green
            // and combat red — visually signals "both theaters at once").
            backgroundColor = msg.role === 'user' ? '#251a2e' : '#190f1f';
          } else if (isHackMode) {
            // Hack mode: matrix-themed green/dark tint
            backgroundColor = msg.role === 'user' ? '#1a2e1a' : '#0f1f0f';
          } else if (isCombatMode) {
            // Meatspace combat: crimson/red — adrenaline, gunfire, danger.
            backgroundColor = msg.role === 'user' ? '#2e1717' : '#1f0f0f';
          } else if (isShipCombatMode) {
            // Ship combat mode: tactical amber/copper tint
            backgroundColor = msg.role === 'user' ? '#312417' : '#23190f';
          } else if (isChaseMode) {
            // Chase mode (Hot Pursuit): electric cyan / vehicle HUD tint
            backgroundColor = msg.role === 'user' ? '#142838' : '#0e1e2c';
          } else if (isSexMode) {
            // Sex mode: warm rose/pink tint
            backgroundColor = msg.role === 'user' ? '#2e1a2a' : '#1f0f1f';
          } else if (isInterviewMode) {
            // Interview / re-interview mode: actual parchment cream as bg
            // with dark sepia text — like an actual scroll. Different visual
            // logic from the other modes (which tint dark) because parchment
            // IS light. User = fresh parchment (#e8dcb8). Assistant =
            // visibly more aged (#bda47a) for clear role distinction.
            backgroundColor = msg.role === 'user' ? '#e8dcb8' : '#bda47a';
          } else {
            // In context: normal colors
            backgroundColor = msg.role === 'user' ? '#2a2a4e' : '#1e1e3a';
          }

          return (
            <div
              key={i}
              className={isInterviewMode ? 'message interview-message' : 'message'}
              style={{
                ...styles.message,
                backgroundColor,
                // Interview-mode: dark sepia text on parchment cream bg.
                // Cascades to all descendants (role label, markdown content,
                // footer) so the whole message reads as scroll-like.
                ...(isInterviewMode ? { color: '#3a2818' } : {})
              }}
            >
              <div style={{...styles.messageRole, ...(
                isNetCombatMode ? {borderLeft: '3px solid #a855f7', paddingLeft: '8px'} :
                isHackMode ? {borderLeft: '3px solid #00ff41', paddingLeft: '8px'} :
                isCombatMode ? {borderLeft: '3px solid #dc2626', paddingLeft: '8px'} :
                isShipCombatMode ? {borderLeft: '3px solid #f59e0b', paddingLeft: '8px'} :
                isChaseMode ? {borderLeft: '3px solid #00d4ff', paddingLeft: '8px'} :
                isSexMode ? {borderLeft: '3px solid #e88fa5', paddingLeft: '8px'} :
                isInterviewMode ? {borderLeft: '3px solid #6b4a23', paddingLeft: '8px', color: '#3a2818'} :
                isSosUser ? {borderLeft: '3px solid #ef4444', paddingLeft: '8px'} :
                {}
              )}}>
                {isNetCombatMode && <span style={{color: '#a855f7', marginRight: '6px', fontFamily: 'monospace', fontSize: '11px'}}>NET+MEAT</span>}
                {!isNetCombatMode && isHackMode && <span style={{color: '#00ff41', marginRight: '6px', fontFamily: 'monospace', fontSize: '11px'}}>MATRIX</span>}
                {!isNetCombatMode && !isHackMode && isCombatMode && <span style={{color: '#dc2626', marginRight: '6px', fontFamily: 'monospace', fontSize: '11px'}}>COMBAT</span>}
                {!isNetCombatMode && !isHackMode && !isCombatMode && isShipCombatMode && <span style={{color: '#f59e0b', marginRight: '6px', fontFamily: 'monospace', fontSize: '11px'}}>SHIP</span>}
                {!isNetCombatMode && !isHackMode && !isCombatMode && !isShipCombatMode && isChaseMode && <span style={{color: '#00d4ff', marginRight: '6px', fontFamily: 'monospace', fontSize: '11px'}}>CHASE</span>}
                {!isNetCombatMode && !isHackMode && !isCombatMode && !isShipCombatMode && !isChaseMode && isSexMode && <span style={{color: '#e88fa5', marginRight: '6px', fontFamily: 'monospace', fontSize: '11px'}}>XXX</span>}
                {isSosUser && <span style={{color: '#ef4444', marginRight: '6px', fontFamily: 'monospace', fontSize: '11px', fontWeight: 'bold'}}>🚨 SOS</span>}
                {msg.role === 'user' ? 'You' : 'Assistant'}
                {msg.role === 'user' && editingMessageIndex !== i && (
                  <button
                    onClick={() => startEditMessage(i)}
                    style={styles.editMessageButton}
                    title="Edit message"
                    className="editMessageButton"
                  >
                    ✏️
                  </button>
                )}
                {msg.role === 'user' && editingMessageIndex !== i && (
                  <button
                    onClick={() => deleteMessagePair(i)}
                    style={styles.deleteMessageButton}
                    title="Delete message and everything after it"
                    className="deleteMessageButton"
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <polyline points="3 6 5 6 21 6" />
                      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                    </svg>
                  </button>
                )}
                {msg.role === 'user' && msg.id && bookmarkingMessageIndex !== i && (
                  <button
                    onClick={() => msg.bookmark ? deleteBookmark(i) : startBookmark(i)}
                    style={styles.bookmarkButton}
                    title={msg.bookmark ? 'Remove bookmark' : 'Add bookmark'}
                    className="bookmarkButton"
                    onMouseEnter={(e) => {
                      cancelTooltipHide();
                      if (msg.bookmark) {
                        const rect = (e.target as HTMLElement).getBoundingClientRect();
                        setBookmarkTooltip({ index: i, x: rect.left, y: rect.bottom + 4 });
                      }
                    }}
                    onMouseLeave={() => scheduleTooltipHide()}
                  >
                    {msg.bookmark ? (
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="#4a4ae8">
                        <path d="M6 2h12a2 2 0 0 1 2 2v18l-8-4-8 4V4a2 2 0 0 1 2-2z"/>
                      </svg>
                    ) : (
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#888" strokeWidth="2">
                        <path d="M6 2h12a2 2 0 0 1 2 2v18l-8-4-8 4V4a2 2 0 0 1 2-2z"/>
                      </svg>
                    )}
                  </button>
                )}
                {/* Context staging checkbox (Novels only) */}
                {msg.role === 'user' && msg.id && chatGameSystem === 'novels' && editingMessageIndex !== i && (
                  <button
                    onClick={() => {
                      if (onToggleMessageStaged && msg.id) {
                        onToggleMessageStaged(msg.id, msg.staged === false);
                      }
                    }}
                    style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '2px 4px', marginLeft: '2px', opacity: 0.7, display: 'flex', alignItems: 'center' }}
                    title={msg.staged === false ? 'Include in context' : 'Exclude from context'}
                    className="bookmarkButton"
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={msg.staged === false ? '#555' : '#4a4ae8'} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <rect x="3" y="3" width="18" height="18" rx="2" />
                      {msg.staged !== false && <polyline points="9 11 12 14 22 4" />}
                    </svg>
                  </button>
                )}
              </div>

              {msg.role === 'user' && bookmarkingMessageIndex === i && (
                <div style={styles.bookmarkInputContainer}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="#4a4ae8" style={{flexShrink: 0}}>
                    <path d="M6 2h12a2 2 0 0 1 2 2v18l-8-4-8 4V4a2 2 0 0 1 2-2z"/>
                  </svg>
                  <textarea
                    value={bookmarkText}
                    onChange={(e) => setBookmarkText(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); saveBookmark(); }
                      if (e.key === 'Escape') { e.preventDefault(); cancelBookmark(); }
                    }}
                    onBlur={() => saveBookmark()}
                    style={styles.bookmarkInput}
                    className="bookmarkInput"
                    placeholder="Bookmark this message..."
                    autoFocus
                    rows={1}
                  />
                </div>
              )}

              {editingMessageIndex === i ? (
                <>
                  {/* Show attached files in edit mode (read-only) */}
                  {msg.attached_files && msg.attached_files.length > 0 && (
                    <div style={styles.editModeAttachedFiles}>
                      <span style={styles.editModeFilesLabel}>📎 Attached files will be preserved:</span>
                      {msg.attached_files.map((file, idx) => (
                        <span key={idx} style={styles.editModeFileName}>{file.filename}</span>
                      ))}
                    </div>
                  )}
                  <textarea
                    value={editingMessageContent}
                    onChange={(e) => setEditingMessageContent(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        saveEditedMessage();
                      }
                      if (e.key === 'Escape') {
                        e.preventDefault();
                        cancelEditMessage();
                      }
                    }}
                    style={styles.editMessageTextarea}
                    autoFocus
                  />
                  <div style={styles.editMessageActions}>
                    <button onClick={saveEditedMessage} style={styles.iconButtonCheck} title="Save & Regenerate (Enter)">✓</button>
                    <button onClick={cancelEditMessage} style={styles.iconButtonX} title="Cancel (Esc)">✕</button>
                  </div>
                </>
              ) : (
                <>
                  {/* Collapsible reasoning section for assistant messages.
                      Combines the voice model's own reasoning (when present —
                      e.g., native thinking blocks or parsed inline <thinking>)
                      with the inner_state pre-pass payload (the 4-axis
                      emotional grounding Opus 4.5 / Sonnet 4.6 produced
                      before voice streamed). Inner state is rendered as
                      pseudo-reasoning so the user can see what was driving
                      the voice register on turns where the voice model
                      itself produced no reasoning trace. */}
                  {(() => {
                    if (msg.role !== 'assistant') return null;
                    const inner = (msg as any).inner_state_payload;
                    const hasInner = (
                      inner && typeof inner === 'object' &&
                      Object.values(inner).some(
                        (v: any) => typeof v === 'string' && v.trim()
                      )
                    );
                    const hasReasoning = !!msg.reasoning;
                    if (!hasInner && !hasReasoning) return null;
                    const parts: string[] = [];
                    if (hasInner) {
                      parts.push('**Inner state — pre-pass emotional grounding:**');
                      parts.push('');
                      if (inner.feeling) parts.push(`- **Feeling:** ${inner.feeling}`);
                      if (inner.wanting) parts.push(`- **Wanting:** ${inner.wanting}`);
                      if (inner.noticing) parts.push(`- **Noticing:** ${inner.noticing}`);
                      if (inner.holding_back) parts.push(`- **Holding back:** ${inner.holding_back}`);
                    }
                    if (hasReasoning) {
                      if (parts.length > 0) {
                        parts.push('');
                        parts.push('---');
                        parts.push('');
                      }
                      parts.push(msg.reasoning as string);
                    }
                    const combined = parts.join('\n');
                    return (
                      <div
                        className="reasoningContainer"
                        style={styles.reasoningContainer}
                        onClick={() => {
                          setExpandedReasoning(prev => {
                            const next = new Set(prev);
                            if (next.has(i)) {
                              next.delete(i);
                            } else {
                              next.add(i);
                            }
                            return next;
                          });
                        }}
                      >
                        <div style={styles.reasoningHeader}>
                          <span>{expandedReasoning.has(i) ? '▲' : '▼'}</span>
                          <span style={styles.reasoningLabel}>Reasoning...</span>
                        </div>
                        {expandedReasoning.has(i) && (
                          <div style={styles.reasoningContent} className="messageContent">
                            <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>{convertMathDelimiters(combined)}</ReactMarkdown>
                          </div>
                        )}
                      </div>
                    );
                  })()}
                  {/* Attached files display */}
                  {msg.role === 'user' && msg.attached_files && msg.attached_files.length > 0 && (
                    <div style={styles.attachedFilesDisplay}>
                      {(() => {
                        const imageFiles = msg.attached_files.filter((f: any) => (f.mime_type || '').startsWith('image/'));
                        const otherFiles = msg.attached_files.filter((f: any) => !(f.mime_type || '').startsWith('image/'));
                        return (
                          <>
                            {imageFiles.length > 0 && (
                              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', marginBottom: otherFiles.length > 0 ? '8px' : 0 }}>
                                {imageFiles.map((file: any, idx: number) => (
                                  <img
                                    key={idx}
                                    src={`data:${file.mime_type};base64,${file.content}`}
                                    alt={file.filename}
                                    title={file.filename}
                                    style={{ maxWidth: '320px', maxHeight: '320px', borderRadius: '6px', border: '1px solid #444', objectFit: 'contain' }}
                                  />
                                ))}
                              </div>
                            )}
                            {otherFiles.length === 1 && (
                              <span style={styles.attachedFilesSingle}>📎 {otherFiles[0].filename}</span>
                            )}
                            {otherFiles.length > 1 && (
                              <details style={styles.attachedFilesDetails}>
                                <summary style={styles.attachedFilesSummary}>
                                  📎 {otherFiles.length} files attached
                                </summary>
                                <div style={styles.attachedFilesExpanded}>
                                  {otherFiles.map((file: any, idx: number) => (
                                    <div key={idx} style={styles.attachedFileItem}>📄 {file.filename}</div>
                                  ))}
                                </div>
                              </details>
                            )}
                          </>
                        );
                      })()}
                    </div>
                  )}
                  {msg.role === 'assistant' && (msg as any).ship_combat_started && !(prevMsg && prevMsg.role === 'user' && (prevMsg as any).ship_combat_hidden_init) && (
                    <>
                      <div style={styles.shipCombatStartBanner}>BEGINNING SHIP COMBAT</div>
                      {(() => {
                        const opening = String((msg as any).ship_combat_opening_narration || '').trim();
                        const openingEmbedded = !!(msg as any).ship_combat_opening_embedded;
                        if (!opening) return null;
                        if (openingEmbedded) return null;
                        return (
                          <div style={styles.shipCombatOpeningNarration}>
                            <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>
                              {convertMathDelimiters(opening)}
                            </ReactMarkdown>
                          </div>
                        );
                      })()}
                    </>
                  )}
                  {/* In-fiction HUD timestamp — frozen snapshot from when
                       this assistant message was generated.  Backend-tracked
                       (pipeline_state_after.hud_state), not model-narrated.
                       Lets you scroll back through the chat and see when
                       each scene happened in fiction time. */}
                  {(() => {
                    const hudLine = getMessageHudLine(msg, messages, i);
                    if (!hudLine) return null;
                    return (
                      <div style={{
                        fontFamily: '"Courier New", monospace',
                        fontSize: '11px',
                        color: '#7a7aa0',
                        marginBottom: '6px',
                        marginTop: '2px',
                        letterSpacing: '0.4px',
                        opacity: 0.75,
                        userSelect: 'text',
                      }}>
                        {hudLine}
                      </div>
                    );
                  })()}
                  <div style={styles.messageContent} className="messageContent">
                    <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>{convertMathDelimiters(msg.content)}</ReactMarkdown>
                  </div>
                  {/* Interview-finalize review-bands button. Surfaces when the
                      finalize message includes flakiness_bands_proposal — the
                      bands have been auto-committed already; this is the
                      adjust-if-you-want handle. */}
                  {msg.role === 'assistant' && (msg as any).characters_finalize && (msg as any).flakiness_bands_proposal && (
                    <button
                      onClick={() => {
                        setBandsModalProposal((msg as any).flakiness_bands_proposal);
                        setBandsModalOpen(true);
                      }}
                      style={{
                        marginTop: 8,
                        padding: '6px 12px',
                        fontSize: 13,
                        backgroundColor: '#2a2a4e',
                        color: '#cfcfff',
                        border: '1px solid #4a4a7e',
                        borderRadius: 6,
                        cursor: 'pointer',
                      }}
                    >
                      Review follow-through
                    </button>
                  )}
                  {/* Inline artifact cards */}
                  {msg.artifact_ops && msg.artifact_ops.length > 0 && (
                    <div style={{ display: 'flex', flexWrap: 'wrap' as const, gap: '6px', marginTop: '8px', marginBottom: '4px' }}>
                      {msg.artifact_ops.map((op: any, i: number) => {
                        if (op.action === 'error') return null;
                        const actionLabel = op.action === 'created' ? 'Created' : op.action === 'replaced' ? 'Updated' : op.action === 'edited' ? 'Edited' : 'Read';
                        const actionColor = op.action === 'created' ? '#34d399' : op.action === 'read' ? '#38bdf8' : '#fbbf24';
                        return (
                          <div key={i} style={{
                            display: 'inline-flex', alignItems: 'center', gap: '6px',
                            padding: '4px 10px', borderRadius: '6px',
                            backgroundColor: '#1e1e3a', border: '1px solid #2a2a4e',
                            fontSize: '0.72rem', color: '#ccc', cursor: 'pointer',
                            userSelect: 'none',
                          }}
                          onClick={() => {
                            // Delay single-click to allow double-click to cancel it
                            const timer = setTimeout(() => {
                              if (onSelectArtifact) onSelectArtifact(op.doc_id);
                            }, 250);
                            (window as any).__artifactClickTimer = timer;
                          }}
                          onDoubleClick={(e) => {
                            e.stopPropagation();
                            clearTimeout((window as any).__artifactClickTimer);
                            if (onDownloadArtifact) onDownloadArtifact(op.doc_id);
                          }}
                          >
                            <span style={{ color: actionColor, fontWeight: 600 }}>{actionLabel}</span>
                            <span>{op.title || op.doc_id}</span>
                            {op.version && <span style={{ color: '#666' }}>v{op.version}</span>}
                          </div>
                        );
                      })}
                    </div>
                  )}
                  <div style={styles.messageFooter}>
                    <div style={styles.messageFooterMainRow}>
                      {msg.tokens ? (
                        <span style={styles.messageTokens}>
                          {msg.tokens}{msg.cost && ` | ${msg.cost}`}
                          {msg.service_tier && ` (${msg.service_tier === 'flex' ? 'Flex' : 'Standard'})`}
                          {msg.model && ` | ${msg.model === 'gpt-5.2' ? 'GPT' : msg.model === 'claude-sonnet-4.5' ? 'Sonnet' : msg.model === 'claude-opus-4.5' ? 'Opus' : msg.model === 'claude-3-opus' ? 'Opus 3' : msg.model}`}
                        </span>
                      ) : <span />}
                      {/* Branch navigation - show only for user messages with siblings */}
                      {msg.role === 'user' && (() => {
                        if (!msg.id) return null;
                        const siblings = getSiblings(allMessages, msg.id);
                        if (siblings.length <= 1) return null;
                        const currentIndex = siblings.findIndex(s => s.id === msg.id);
                        const prevSibling = currentIndex > 0 ? siblings[currentIndex - 1] : null;
                        const nextSibling = currentIndex < siblings.length - 1 ? siblings[currentIndex + 1] : null;
                        return (
                          <span style={styles.branchNav}>
                            <button
                              onClick={(e) => { e.stopPropagation(); if (prevSibling?.id) switchBranch(prevSibling.id); }}
                              disabled={!prevSibling}
                              style={{
                                ...styles.branchNavButton,
                                opacity: prevSibling ? 1 : 0.3,
                                cursor: prevSibling ? 'pointer' : 'default'
                              }}
                              title="Previous edit"
                            >
                              ◀
                            </button>
                            <span style={styles.branchNavText}>{currentIndex + 1}/{siblings.length}</span>
                            <button
                              onClick={(e) => { e.stopPropagation(); if (nextSibling?.id) switchBranch(nextSibling.id); }}
                              disabled={!nextSibling}
                              style={{
                                ...styles.branchNavButton,
                                opacity: nextSibling ? 1 : 0.3,
                                cursor: nextSibling ? 'pointer' : 'default'
                              }}
                              title="Next edit"
                            >
                              ▶
                            </button>
                          </span>
                        );
                      })()}
                      {msg.timestamp && (
                        <span style={styles.messageTimestamp}>{formatTimestamp(msg.timestamp)}</span>
                      )}
                    </div>
                    <div style={styles.messageFooterBreakdown}>
                      {(msg as any).flag_agent_usage && (
                        <span style={{ ...styles.messageTokens, opacity: 0.75 }}>
                          Flags: {formatUsageString((msg as any).flag_agent_usage)}
                          {typeof (msg as any).flag_agent_cost === 'number' && ` | $${(msg as any).flag_agent_cost.toFixed(6)}`}
                          {' '}(Haiku)
                        </span>
                      )}
                      {(msg as any).recall_usage && (
                        <span style={{ ...styles.messageTokens, opacity: 0.75 }}>
                          Recall: {formatUsageString((msg as any).recall_usage)}
                          {typeof (msg as any).recall_cost === 'number' && ` | $${(msg as any).recall_cost.toFixed(6)}`}
                          {' '}(Haiku)
                        </span>
                      )}
                      {(msg as any).character_agent_usage && (
                        <span style={{ ...styles.messageTokens, opacity: 0.75 }}>
                          Record: {formatUsageString((msg as any).character_agent_usage)}
                          {typeof (msg as any).character_agent_cost === 'number' && ` | $${(msg as any).character_agent_cost.toFixed(6)}`}
                          {' '}(Sonnet)
                        </span>
                      )}
                      {(msg as any).off_screen_usage && (
                        <span style={{ ...styles.messageTokens, opacity: 0.75 }}>
                          Off-screen: {formatUsageString((msg as any).off_screen_usage)}
                          {typeof (msg as any).off_screen_cost === 'number' && ` | $${(msg as any).off_screen_cost.toFixed(6)}`}
                          {' '}(Opus 4.5)
                        </span>
                      )}
                      {(msg as any).inner_state_usage && (() => {
                        const sp = (msg as any).inner_state_scratchpad_status;
                        let spTag: string | null = null;
                        if (sp === 'reordered') spTag = ' · ⚠️ scratchpad reordered';
                        else if (sp === 'missing') spTag = ' · ⚠️ scratchpad missing';
                        else if (sp === 'no_output') spTag = ' · ⚠️ no inner output';
                        else if (sp === 'no_call') spTag = ' · ⚠️ inner not called';
                        // 'ordered' (happy path) and undefined (legacy / pre-Pattern-C) show nothing
                        return (
                          <span style={{ ...styles.messageTokens, opacity: 0.75 }}>
                            Inner: {formatUsageString((msg as any).inner_state_usage)}
                            {typeof (msg as any).inner_state_cost === 'number' && ` | $${(msg as any).inner_state_cost.toFixed(6)}`}
                            {' '}({(msg as any).inner_state_model === 'claude-sonnet-4-6' ? 'Sonnet' : 'Opus 4.5'})
                            {spTag}
                          </span>
                        );
                      })()}
                      {(msg as any).image_reading_usage && (
                        <span style={{ ...styles.messageTokens, opacity: 0.75 }}>
                          Vision: {formatUsageString((msg as any).image_reading_usage)}
                          {typeof (msg as any).image_reading_cost === 'number' && ` | $${(msg as any).image_reading_cost.toFixed(6)}`}
                          {' '}({(msg as any).image_reading_model || 'Opus 4.5'})
                          {(msg as any).image_reading_count ? `, ${(msg as any).image_reading_count} image${(msg as any).image_reading_count === 1 ? '' : 's'}` : ''}
                        </span>
                      )}
                      {(msg as any).search_usage && (
                        <span style={{ ...styles.messageTokens, opacity: 0.75 }}>
                          Search: {(msg as any).search_calls || 0} call{((msg as any).search_calls || 0) === 1 ? '' : 's'}
                          {typeof (msg as any).search_cost === 'number' && ` | $${(msg as any).search_cost.toFixed(6)}`}
                          {' '}({(msg as any).search_model || 'Sonar'})
                        </span>
                      )}
                      {(msg as any).meme_calls > 0 && (
                        <span style={{ ...styles.messageTokens, opacity: 0.75 }}>
                          Meme: {(msg as any).meme_calls} call{(msg as any).meme_calls === 1 ? '' : 's'}
                          {typeof (msg as any).meme_cost === 'number' && ` | $${(msg as any).meme_cost.toFixed(6)}`}
                          {' '}(Imgflip free + Reddit free + Sonnet vision-pick)
                        </span>
                      )}
                      {(msg as any).fetch_url_calls > 0 && (
                        <span style={{ ...styles.messageTokens, opacity: 0.75 }}>
                          URL fetch: {(msg as any).fetch_url_calls} call{(msg as any).fetch_url_calls === 1 ? '' : 's'}
                        </span>
                      )}
                      {/* Aggregate Total: msg.cost is just the voice agent's cost
                          (Opus 3 reply); the side-agent costs are billed separately
                          and tracked on their own fields. We sum them here so the
                          Total row is the actual money spent on this turn.
                          Suppressed when no side-agents fired (would equal main). */}
                      {msg.role === 'assistant' && msg.cost && (() => {
                        const agentCosts =
                          ((msg as any).flag_agent_cost || 0) +
                          ((msg as any).recall_cost || 0) +
                          ((msg as any).character_agent_cost || 0) +
                          ((msg as any).off_screen_cost || 0) +
                          ((msg as any).inner_state_cost || 0) +
                          ((msg as any).image_reading_cost || 0) +
                          ((msg as any).search_cost || 0) +
                          ((msg as any).meme_cost || 0);
                        const voiceCost = parseFloat(String(msg.cost).replace(/[^0-9.\-]/g, '')) || 0;
                        const totalNum = voiceCost + agentCosts;
                        if (agentCosts < 1e-9) return null;
                        return (
                          <span style={{ ...styles.messageTokens, fontWeight: 600, marginTop: '2px' }}>
                            Total: ${totalNum.toFixed(6)}
                          </span>
                        );
                      })()}
                    </div>
                  </div>
                </>
              )}
            </div>
          );
        })}
        {(isLoading.has(currentChat) || pipelineStage.has(currentChat)) &&
         !(messages.length > 0 && messages[messages.length - 1].role === 'assistant' && messages[messages.length - 1].content) && (
          <div style={styles.message}>
            <div style={styles.messageRole}>Assistant</div>
            <div style={styles.messageContent}>
              {(() => {
                const ps = pipelineStage.get(currentChat);
                if (!ps) return 'Thinking...';
                const stages = ['events', 'mechanics', 'narration'];
                const currentIdx = stages.indexOf(ps.stage);
                return (
                  <span>
                    {stages.map((s, i) => {
                      const label = s.charAt(0).toUpperCase() + s.slice(1);
                      let icon: string;
                      let color: string;
                      if (i < currentIdx || (i === currentIdx && ps.status === 'complete')) {
                        icon = '✓';
                        color = '#4caf50';
                      } else if (i === currentIdx) {
                        icon = '●';
                        color = '#e0e0e0';
                      } else {
                        icon = '○';
                        color = '#666';
                      }
                      return (
                        <span key={s} style={{ color, marginRight: i < stages.length - 1 ? '12px' : '0' }}>
                          {icon} {label}
                        </span>
                      );
                    })}
                  </span>
                );
              })()}
            </div>
          </div>
        )}
        {stateNotifications.length > 0 && (
          <div style={{
            ...styles.stateNotificationsContainer,
            ...(isMobile ? styles.stateNotificationsContainerMobile : {}),
          }}>
            {stateNotifications.map((n: any, i: number) => {
              if (n.type === 'expense_paid') {
                return (
                  <div key={i} style={styles.expensePaidNotification}>
                    <span style={styles.notificationLabel}>{n.edgerunner}</span>
                    {' '}{n.summary}
                    {n.new_balance != null && <span style={{ color: '#888' }}> [Balance: {n.new_balance}eb]</span>}
                  </div>
                );
              }
              if (n.type === 'expense_unpaid') {
                return (
                  <div key={i} style={styles.expenseUnpaidNotification}>
                    <span style={styles.notificationLabel}>{n.edgerunner}</span>
                    {' '}{n.summary}
                  </div>
                );
              }
              if (n.type === 'expense_consequence') {
                const isDeath = n.result === 'dead';
                return (
                  <div key={i} style={{
                    ...styles.expenseConsequenceNotification,
                    ...(isDeath ? { borderLeft: '3px solid #ff0000', background: 'rgba(255,0,0,0.08)' } : {}),
                  }}>
                    <span style={styles.notificationLabel}>{n.edgerunner}</span>
                    {' '}{n.summary}
                  </div>
                );
              }
              if (n.type === 'housing_crammed') {
                return (
                  <div key={i} style={styles.expenseConsequenceNotification}>
                    <span style={styles.notificationLabel}>{n.owner}</span>
                    {' '}{n.summary}
                  </div>
                );
              }
              if (n.type === 'ship_npc_action') {
                const actor = n.character_name || (n.role ? `${String(n.role).charAt(0).toUpperCase()}${String(n.role).slice(1)}` : 'Crew');
                return (
                  <div key={i} style={styles.shipNpcActionNotification}>
                    {n.ship_name || 'Ship'} - {actor}: {n.action || 'Action'} - {n.effect || 'No effect reported'}
                  </div>
                );
              }
              if (n.type === 'npc_memory') {
                return (
                  <div key={i} style={styles.memoryNotification}>
                    <span style={styles.notificationLabel}>{n.npc}</span>
                    {' remembered'}
                    {n.impact ? ` (impact ${n.impact})` : ''}
                    {': '}
                    {n.text}
                    {n.quote && (
                      <div style={styles.notificationQuote}>Quote: "{n.quote}"</div>
                    )}
                  </div>
                );
              }
              if (n.type === 'character_search') {
                const failed = n.ok === false;
                return (
                  <div key={i} style={{
                    ...styles.searchNotification,
                    ...(failed ? styles.searchNotificationError : {}),
                  }}>
                    <span style={styles.notificationLabel}>
                      {failed ? '🔍 search failed' : '🔍 looked up'}
                    </span>
                    {n.reason && <>: {n.reason}</>}
                    {n.query && (
                      <span style={styles.notificationReason}> — "{n.query}"</span>
                    )}
                    {failed && n.error && (
                      <div style={{ fontSize: '11px', color: '#888', marginTop: '2px' }}>
                        {n.error}
                      </div>
                    )}
                  </div>
                );
              }
              if (n.type === 'character_fetch_url') {
                const failed = n.ok === false;
                return (
                  <div key={i} style={{
                    ...styles.searchNotification,
                    ...(failed ? styles.searchNotificationError : {}),
                  }}>
                    <span style={styles.notificationLabel}>
                      {failed ? '🔗 fetch failed' : '🔗 reading link'}
                    </span>
                    {n.reason && <>: {n.reason}</>}
                    {n.title && (
                      <span style={styles.notificationReason}> — "{n.title}"</span>
                    )}
                    {failed && n.error && (
                      <div style={{ fontSize: '11px', color: '#888', marginTop: '2px' }}>
                        {n.error}
                      </div>
                    )}
                  </div>
                );
              }
              if (n.type === 'character_make_meme') {
                const failed = n.ok === false;
                const isNoMatch = failed && n.kind === 'no_match';
                let label: string;
                if (!failed) label = '🎭 making a meme';
                else if (isNoMatch) label = '🎭 template not in library';
                else label = '🎭 meme failed';
                return (
                  <div key={i} style={{
                    ...styles.searchNotification,
                    ...(failed ? styles.searchNotificationError : {}),
                  }}>
                    <span style={styles.notificationLabel}>{label}</span>
                    {n.reason && <>: {n.reason}</>}
                    {(isNoMatch ? n.requested_template : n.template) && (
                      <span style={styles.notificationReason}>
                        {' '}— {isNoMatch ? n.requested_template : n.template}
                      </span>
                    )}
                    {failed && n.error && !isNoMatch && (
                      <div style={{ fontSize: '11px', color: '#888', marginTop: '2px' }}>
                        {n.error}
                      </div>
                    )}
                  </div>
                );
              }
              if (n.type === 'character_busy') {
                const desc = n.description || n.kind || 'busy';
                return (
                  <div key={i} style={styles.searchNotification}>
                    <span style={styles.notificationLabel}>💤 message held</span>
                    <span style={styles.notificationReason}> — {desc}; she'll see it later</span>
                    {n.ends_at && (
                      <div style={{ fontSize: '11px', color: '#888', marginTop: '2px' }}>
                        until {new Date(n.ends_at).toLocaleString()}
                      </div>
                    )}
                  </div>
                );
              }
              if (n.type === 'character_sos_break') {
                const desc = n.description || 'busy';
                return (
                  <div key={i} style={styles.searchNotification}>
                    <span style={styles.notificationLabel}>🚨 SOS broke through</span>
                    <span style={styles.notificationReason}> — interrupting from {desc}</span>
                  </div>
                );
              }
              if (n.type === 'character_memory') {
                return (
                  <div key={i} style={styles.memoryNotification}>
                    <span style={styles.notificationLabel}>📌 memory saved</span>
                    {n.impact ? ` (impact ${n.impact})` : ''}
                    {': '}
                    {n.text}
                    {n.quote && (
                      <div style={styles.notificationQuote}>Quote: "{n.quote}"</div>
                    )}
                  </div>
                );
              }
              if (n.type === 'character_callback_added') {
                return (
                  <div key={i} style={styles.searchNotification}>
                    <span style={styles.notificationLabel}>📅 new plan / callback</span>
                    {n.text && <>: {n.text}</>}
                    {n.due_by && (
                      <span style={styles.notificationReason}> — due {n.due_by}</span>
                    )}
                  </div>
                );
              }
              if (n.type === 'character_callback_resolved') {
                return (
                  <div key={i} style={styles.searchNotification}>
                    <span style={styles.notificationLabel}>✅ callback resolved</span>
                    {n.id != null && <span style={styles.notificationReason}> #{n.id}</span>}
                    {n.reason && <>: {n.reason}</>}
                  </div>
                );
              }
              if (n.type === 'character_callback_expired') {
                return (
                  <div key={i} style={styles.searchNotification}>
                    <span style={styles.notificationLabel}>⌛ plan elapsed</span>
                    {n.text && <>: {n.text}</>}
                    {n.due_by && (
                      <span style={styles.notificationReason}> — was due {n.due_by}</span>
                    )}
                  </div>
                );
              }
              if (n.type === 'character_arc_state') {
                return (
                  <div key={i} style={styles.searchNotification}>
                    <span style={styles.notificationLabel}>💞 arc shift</span>
                    {': '}
                    <span style={styles.notificationReason}>{n.value}</span>
                  </div>
                );
              }
              if (n.type === 'character_user_profile') {
                return (
                  <div key={i} style={styles.memoryNotification}>
                    <span style={styles.notificationLabel}>👤 learned about you</span>
                    {n.category && <span style={styles.notificationReason}> [{n.category}]</span>}
                    {': '}
                    {n.text}
                  </div>
                );
              }
              if (n.type === 'character_growth') {
                return (
                  <div key={i} style={styles.memoryNotification}>
                    <span style={styles.notificationLabel}>🌱 growth note</span>
                    {n.category && <span style={styles.notificationReason}> [{n.category}]</span>}
                    {': '}
                    {n.text}
                  </div>
                );
              }
              if (n.type === 'character_find_meme_post') {
                const failed = n.ok === false;
                return (
                  <div key={i} style={{
                    ...styles.searchNotification,
                    ...(failed ? styles.searchNotificationError : {}),
                  }}>
                    <span style={styles.notificationLabel}>
                      {failed ? '🖼 meme search: no match' : '🖼 finding a meme'}
                    </span>
                    {n.reason && <>: {n.reason}</>}
                    {n.query && (
                      <span style={styles.notificationReason}> — "{n.query}"</span>
                    )}
                    {failed && n.error && (
                      <div style={{ fontSize: '11px', color: '#888', marginTop: '2px' }}>
                        {n.error}
                      </div>
                    )}
                  </div>
                );
              }
              if (n.type === 'voice_update') {
                return (
                  <div key={i} style={styles.voiceNotification}>
                    <span style={styles.notificationLabel}>{n.npc}</span>
                    {' voice '}{n.old_voice ? 'updated' : 'set'}{': '}{n.voice}
                  </div>
                );
              }
              if (n.type === 'time_passed') {
                return (
                  <div key={i} style={styles.timePassedNotification}>
                    <span style={styles.notificationLabel}>{'⏱ '}{n.duration}</span>
                    {n.reason && <span style={styles.notificationReason}> — {n.reason}</span>}
                  </div>
                );
              }
              if (n.type === 'plot_decision') {
                const isDivergence = n.severity === 'divergence';
                return (
                  <div key={i} style={{
                    ...styles.plotNotification,
                    ...(isDivergence ? styles.plotNotificationDivergence : {}),
                  }}>
                    <span style={{
                      ...styles.plotSeverityBadge,
                      ...(isDivergence ? styles.plotSeverityBadgeDivergence : {}),
                    }}>
                      {n.severity || 'plot'}
                    </span>
                    {n.key && <span style={styles.notificationLabel}>{n.key}{n.value ? ` = ${n.value}` : ''}</span>}
                    {n.key ? ' — ' : ''}{n.decision}
                    {n.episode && (
                      <span style={{ color: '#666', fontSize: '11px' }}> [{n.episode}]</span>
                    )}
                  </div>
                );
              }
              // RS/RomS/FR/npc_rs/npc_roms change
              const isNeg = (n.change ?? 0) < 0;
              const label = n.type.replace('_change', '').toUpperCase();
              const sign = n.change > 0 ? '+' : '';
              return (
                <div key={i} style={{
                  ...styles.rsNotification,
                  ...(isNeg ? styles.rsNotificationNegative : {}),
                }}>
                  <span style={styles.notificationLabel}>{sign}{n.change} {label}</span>
                  ({n.target}){n.other ? ` → ${n.other}` : ''}
                  {n.new_total != null && ` [${n.new_total}]`}
                  {n.reason && (
                    <span style={styles.notificationReason}> — {n.reason}</span>
                  )}
                </div>
              );
            })}
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      <div className="inputArea" style={styles.inputArea}>
        {/* Staged files bar */}
        {stagedFiles.length > 0 && (
          <div style={styles.stagedFilesBar}>
            {stagedFiles.map((file, idx) => {
              const isImage = (file as any).mime_type?.startsWith('image/');
              return (
                <div key={idx} style={styles.stagedFileChip}>
                  <span style={styles.stagedFileName}>{isImage ? '🖼️' : '📄'} {file.filename}</span>
                  <button
                    onClick={() => removeStagedFile(idx)}
                    style={styles.stagedFileRemove}
                    title="Remove file"
                  >
                    ✕
                  </button>
                </div>
              );
            })}
          </div>
        )}

        <div style={styles.inputRow}>
          {/* Attach button with dropdown */}
          <div ref={attachMenuRef} style={styles.attachButtonContainer}>
            <button
              onClick={() => setShowAttachMenu(!showAttachMenu)}
              style={styles.attachButton}
              title="Attach files"
            >
              +
            </button>
            {showAttachMenu && (
              <div style={styles.attachMenu}>
                <button
                  onClick={() => chatFileInputRef.current?.click()}
                  style={styles.attachMenuItem}
                >
                  📄 Add a file
                </button>
              </div>
            )}
            <input
              type="file"
              ref={chatFileInputRef}
              onChange={handleChatFileSelect}
              multiple
              accept=".txt,.md,.yaml,.yml,image/png,image/jpeg,image/gif,image/webp"
              style={{ display: 'none' }}
            />
          </div>

          <div
            style={{
              position: 'relative',
              flex: 1,
              ...(isDraggingFile ? { outline: '2px dashed #4a4ae8', borderRadius: '8px' } : {})
            }}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
          >
            <div
              className="resizeHandle"
              style={styles.resizeHandle}
              onMouseDown={handleResizeStart}
              title="Drag to resize"
            >
              ⋮⋮
            </div>
            <textarea
              ref={textareaRef}
              placeholder={isDraggingFile ? "Drop files here..." : "Type a message..."}
              value={newMessage}
              onChange={(e) => setNewMessage(e.target.value)}
              onKeyDown={(e) => {
                // Slash picker is active: intercept arrow keys / Enter / Tab / Esc.
                if (slashPicker.open && slashPicker.filtered.length > 0) {
                  if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    setSlashIdx(i => Math.min(i + 1, slashPicker.filtered.length - 1));
                    return;
                  }
                  if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    setSlashIdx(i => Math.max(i - 1, 0));
                    return;
                  }
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    handleSlashPick(slashPicker.filtered[slashIdx]);
                    return;
                  }
                  if (e.key === 'Tab') {
                    e.preventDefault();
                    handleSlashInsert(slashPicker.filtered[slashIdx]);
                    return;
                  }
                  if (e.key === 'Escape') {
                    e.preventDefault();
                    setNewMessage('');
                    return;
                  }
                }
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  sendMessage();
                }
              }}
              style={{
                ...styles.messageInput,
                height: isMobile ? '60px' : `${textareaHeight}px`
              }}
            />
            {slashPicker.open && slashPicker.filtered.length > 0 && (
              <SlashCommandPicker
                commands={slashPicker.filtered}
                selectedIndex={slashIdx}
                onSelectIndex={setSlashIdx}
                onPick={handleSlashPick}
                onInsert={handleSlashInsert}
              />
            )}
          </div>

          <div style={styles.buttonColumn}>
            <button
              onClick={onNotesClick}
              style={{
                ...styles.updateButton,
                backgroundColor: updatesText.trim() ? '#2d6a4f' : '#2a2a4e'
              }}
              title={updatesText.trim() ? 'Notes (click to edit)' : 'Add notes'}
            >
              Notes
            </button>
            <button
              onClick={sendMessage}
              disabled={isLoading.has(currentChat)}
              style={{
                ...styles.sendButton,
                opacity: isLoading.has(currentChat) ? 0.5 : 1
              }}
            >
              Send
            </button>
          </div>
        </div>
      </div>
      {bookmarkTooltip && messages[bookmarkTooltip.index]?.bookmark && (
        <div
          style={{
            ...styles.bookmarkTooltip,
            left: bookmarkTooltip.x,
            top: bookmarkTooltip.y,
          }}
          onMouseEnter={() => cancelTooltipHide()}
          onMouseLeave={() => scheduleTooltipHide()}
          onClick={() => {
            const idx = bookmarkTooltip.index;
            setBookmarkTooltip(null);
            startBookmark(idx);
          }}
        >
          {messages[bookmarkTooltip.index].bookmark}
        </div>
      )}
      {/* Flakiness-bands review modal — opened from interview-finalize messages. */}
      <FlakinessBandsModal
        isOpen={bandsModalOpen}
        username={username}
        project={currentProject || ''}
        proposal={bandsModalProposal || {}}
        onClose={() => setBandsModalOpen(false)}
      />
    </>
  );
}
