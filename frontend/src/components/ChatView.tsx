import React, { useRef, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import { styles } from '../styles';
import { ChatMessage, ModelInfo, convertMathDelimiters, formatTimestamp } from '../types';

interface ChatViewProps {
  isMobile: boolean;
  currentChat: string;
  currentProject: string | null;
  viewerCount: number;
  projectGameSystem: string | null;
  availableGameSystems: {id: string, name: string}[];
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
  sendMessage: () => void;
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
}

export default function ChatView({
  isMobile,
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
}: ChatViewProps) {
  const tooltipHideTimeout = useRef<NodeJS.Timeout | null>(null);

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
        <h2 style={styles.chatTitle}>{currentChat}</h2>
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
          if (!isInContext) {
            // Out of context: grayed out versions
            backgroundColor = msg.role === 'user' ? '#1f1f35' : '#171728';
          } else if (isHackMode) {
            // Hack mode: matrix-themed green/dark tint
            backgroundColor = msg.role === 'user' ? '#1a2e1a' : '#0f1f0f';
          } else if (isShipCombatMode) {
            // Ship combat mode: tactical amber/copper tint
            backgroundColor = msg.role === 'user' ? '#312417' : '#23190f';
          } else if (isSexMode) {
            // Sex mode: warm rose/pink tint
            backgroundColor = msg.role === 'user' ? '#2e1a2a' : '#1f0f1f';
          } else {
            // In context: normal colors
            backgroundColor = msg.role === 'user' ? '#2a2a4e' : '#1e1e3a';
          }

          return (
            <div
              key={i}
              className="message"
              style={{
                ...styles.message,
                backgroundColor
              }}
            >
              <div style={{...styles.messageRole, ...(isHackMode ? {borderLeft: '3px solid #00ff41', paddingLeft: '8px'} : isShipCombatMode ? {borderLeft: '3px solid #f59e0b', paddingLeft: '8px'} : isSexMode ? {borderLeft: '3px solid #e88fa5', paddingLeft: '8px'} : {})}}>
                {isHackMode && <span style={{color: '#00ff41', marginRight: '6px', fontFamily: 'monospace', fontSize: '11px'}}>MATRIX</span>}
                {!isHackMode && isShipCombatMode && <span style={{color: '#f59e0b', marginRight: '6px', fontFamily: 'monospace', fontSize: '11px'}}>SHIP</span>}
                {!isHackMode && !isShipCombatMode && isSexMode && <span style={{color: '#e88fa5', marginRight: '6px', fontFamily: 'monospace', fontSize: '11px'}}>XXX</span>}
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
                  {/* Collapsible reasoning section for assistant messages */}
                  {msg.role === 'assistant' && msg.reasoning && (
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
                          <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>{convertMathDelimiters(msg.reasoning || '')}</ReactMarkdown>
                        </div>
                      )}
                    </div>
                  )}
                  {/* Attached files display */}
                  {msg.role === 'user' && msg.attached_files && msg.attached_files.length > 0 && (
                    <div style={styles.attachedFilesDisplay}>
                      {msg.attached_files.length === 1 ? (
                        <span style={styles.attachedFilesSingle}>📎 {msg.attached_files[0].filename}</span>
                      ) : (
                        <details style={styles.attachedFilesDetails}>
                          <summary style={styles.attachedFilesSummary}>
                            📎 {msg.attached_files.length} files attached
                          </summary>
                          <div style={styles.attachedFilesExpanded}>
                            {msg.attached_files.map((file, idx) => (
                              <div key={idx} style={styles.attachedFileItem}>📄 {file.filename}</div>
                            ))}
                          </div>
                        </details>
                      )}
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
                  <div style={styles.messageContent} className="messageContent">
                    <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>{convertMathDelimiters(msg.content)}</ReactMarkdown>
                  </div>
                  <div style={styles.messageFooter}>
                    {msg.tokens && (
                      <span style={styles.messageTokens}>
                        {msg.tokens}
                        {msg.service_tier && ` (${msg.service_tier === 'flex' ? 'Flex' : 'Standard'})`}
                        {msg.model && ` | ${msg.model === 'gpt-5.2' ? 'GPT' : msg.model === 'claude-sonnet-4.5' ? 'Sonnet' : msg.model === 'claude-opus-4.5' ? 'Opus' : msg.model}`}
                      </span>
                    )}
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
                    {(() => {
                      return msg.timestamp ? (
                        <span style={styles.messageTimestamp}>{formatTimestamp(msg.timestamp)}</span>
                      ) : null;
                    })()}
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
            {stagedFiles.map((file, idx) => (
              <div key={idx} style={styles.stagedFileChip}>
                <span style={styles.stagedFileName}>📄 {file.filename}</span>
                <button
                  onClick={() => removeStagedFile(idx)}
                  style={styles.stagedFileRemove}
                  title="Remove file"
                >
                  ✕
                </button>
              </div>
            ))}
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
              accept=".txt,.md,.yaml,.yml"
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
              placeholder={isDraggingFile ? "Drop files here..." : "Type a message..."}
              value={newMessage}
              onChange={(e) => setNewMessage(e.target.value)}
              onKeyDown={(e) => {
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
    </>
  );
}
