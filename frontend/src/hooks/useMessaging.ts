import { useState, useRef, useCallback, useEffect } from 'react';
import { ChatMessage, ChatStats, Artifact } from '../types';

// Side-agent cost / usage / metadata fields that the backend mirrors into
// the `done` event payload. We copy them onto the assistant message so the
// per-message cost breakdown (Recall / State / Inner / Vision / Meme / etc.)
// renders IMMEDIATELY at end-of-stream, not just after a page reload.
const SIDE_AGENT_DONE_FIELDS = [
  'flag_agent_usage', 'flag_agent_cost', 'flag_agent_model',
  'character_agent_usage', 'character_agent_cost', 'character_agent_model', 'character_agent_ops',
  'off_screen_usage', 'off_screen_cost', 'off_screen_model',
  'recall_usage', 'recall_cost', 'recall_model', 'recall_ids',
  'inner_state_usage', 'inner_state_cost', 'inner_state_model', 'inner_state_payload',
  'search_usage', 'search_cost', 'search_calls', 'search_model', 'search_log',
  'fetch_url_calls', 'fetch_url_log',
  'image_reading_usage', 'image_reading_cost', 'image_reading_model', 'image_reading_count',
  'meme_calls', 'meme_cost', 'meme_log', 'meme_vision_usage', 'meme_vision_model',
] as const;

function mergeSideAgentFields(target: any, data: any): void {
  for (const k of SIDE_AGENT_DONE_FIELDS) {
    if (k in data) {
      target[k] = (data as any)[k];
    }
  }
}

interface UseMessagingDeps {
  user: { username: string } | null;
  currentChat: string | null;
  currentProject: string | null;
  currentChatRef: React.MutableRefObject<string | null>;
  currentProjectRef: React.MutableRefObject<string | null>;
  messages: ChatMessage[];
  setMessages: React.Dispatch<React.SetStateAction<ChatMessage[]>>;
  allMessages: ChatMessage[];
  setAllMessages: React.Dispatch<React.SetStateAction<ChatMessage[]>>;
  currentLeafId: string | null;
  setCurrentLeafId: (v: string | null) => void;
  totalMessages: number;
  setTotalMessages: React.Dispatch<React.SetStateAction<number>>;
  setHasMoreMessages: (v: boolean) => void;
  setMessageOffset: React.Dispatch<React.SetStateAction<number>>;
  selectedModel: string;
  setSelectedModel: (v: string) => void;
  contextStartIndex: number;
  setContextStartIndex: (v: number) => void;
  stats: ChatStats | null;
  setStats: (v: ChatStats | null) => void;
  isLoading: Set<string>;
  setIsLoading: React.Dispatch<React.SetStateAction<Set<string>>>;
  setPipelineStage: React.Dispatch<React.SetStateAction<Map<string, {stage: string, status: string}>>>;
  setPipelineState: (v: any) => void;
  setStateNotifications: (v: any[]) => void;
  setHackState: (v: any) => void;
  setDocsRefreshed: (v: boolean) => void;
  setError: (v: string) => void;
  setArtifacts: React.Dispatch<React.SetStateAction<Record<string, Artifact>>>;
  editingMessageIndex: number | null;
  editingMessageContent: string;
  setEditingMessageIndex: (v: number | null) => void;
  setEditingMessageContent: (v: string) => void;
  fetchUserStats: () => void;
  fetchFreeTokens: () => void;
}

export function useMessaging(deps: UseMessagingDeps) {
  const [newMessage, setNewMessageRaw] = useState('');
  const [stagedFiles, setStagedFiles] = useState<{filename: string, content: string, mime_type?: string}[]>([]);
  const [showAttachMenu, setShowAttachMenu] = useState(false);
  const [isDraggingFile, setIsDraggingFile] = useState(false);
  const [textareaHeight, setTextareaHeight] = useState(85);

  const chatFileInputRef = useRef<HTMLInputElement>(null);

  // Load draft from sessionStorage when chat changes
  useEffect(() => {
    if (deps.user && deps.currentChat) {
      const key = `inputDraft:${deps.user.username}:${deps.currentChat}`;
      const saved = sessionStorage.getItem(key);
      setNewMessageRaw(saved || '');
    } else {
      setNewMessageRaw('');
    }
  }, [deps.currentChat, deps.user?.username]);

  // Wrapped setter that persists to sessionStorage
  const setNewMessage = useCallback((value: string) => {
    setNewMessageRaw(value);
    if (deps.user && deps.currentChat) {
      const key = `inputDraft:${deps.user.username}:${deps.currentChat}`;
      if (value) {
        sessionStorage.setItem(key, value);
      } else {
        sessionStorage.removeItem(key);
      }
    }
  }, [deps.user?.username, deps.currentChat]);

  const createContextGuard = () => {
    const chat = deps.currentChat;
    const project = deps.currentProject;
    return {
      chat,
      project,
      isChatStale: () => deps.currentChatRef.current !== chat,
      isProjectStale: () => deps.currentProjectRef.current !== project,
      isStale: () => deps.currentChatRef.current !== chat || deps.currentProjectRef.current !== project,
    };
  };

  // Parse SSE events from a chunk of text
  const parseSSEEvents = (text: string): Array<{type: string, data: any}> => {
    const events: Array<{type: string, data: any}> = [];
    const lines = text.split('\n');
    let currentEvent: string | null = null;
    let currentData: string[] = [];

    for (const line of lines) {
      if (line.startsWith('event: ')) {
        currentEvent = line.slice(7);
      } else if (line.startsWith('data: ')) {
        currentData.push(line.slice(6));
      } else if (line === '' && currentEvent && currentData.length > 0) {
        try {
          const dataStr = currentData.join('\n');
          events.push({
            type: currentEvent,
            data: JSON.parse(dataStr)
          });
        } catch (e) {
          console.error('Failed to parse SSE data:', e);
        }
        currentEvent = null;
        currentData = [];
      }
    }

    return events;
  };

  const saveEditedMessage = async () => {
    if (!deps.user || !deps.currentChat || deps.editingMessageIndex === null || !deps.editingMessageContent.trim()) return;
    deps.setStateNotifications([]);

    // Bounds check - messages array might have changed since edit started
    if (deps.editingMessageIndex < 0 || deps.editingMessageIndex >= deps.messages.length) {
      deps.setError('Message index out of bounds. Please cancel and try again.');
      deps.setEditingMessageIndex(null);
      deps.setEditingMessageContent('');
      return;
    }

    const ctx = createContextGuard();

    // Save original state for rollback
    const originalMessages = [...deps.messages];
    const originalAllMessages = [...deps.allMessages];
    const originalLeafId = deps.currentLeafId;

    deps.setIsLoading(prev => new Set(prev).add(ctx.chat!));

    try {
      // Get original message to preserve attached files and find parent
      const originalMessage = deps.messages[deps.editingMessageIndex];
      const parentId = originalMessage.parent_id || null;

      // Optimistically show truncated messages + edited user msg + streaming placeholder
      const truncatedMessages = deps.messages.slice(0, deps.editingMessageIndex);
      const editedMessage: ChatMessage = {
        role: 'user',
        content: deps.editingMessageContent.trim(),
        attached_files: originalMessage.attached_files
      };
      const streamingAssistantMsg: ChatMessage = {
        role: 'assistant',
        content: '',
        timestamp: new Date().toISOString()
      };
      deps.setMessages([...truncatedMessages, editedMessage, streamingAssistantMsg]);

      // Clear editing state
      deps.setEditingMessageIndex(null);
      deps.setEditingMessageContent('');

      let accumulatedContent = '';
      let accumulatedThinking = '';
      let userMsgId: string | null = null;
      let shipCombatChaining = false;
      let shipCombatChainContent = '';
      let shipCombatChainThinking = '';

      const response = await fetch('/api/send-message-stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: deps.user.username,
          chat_name: ctx.chat,
          message: editedMessage.content,
          project: ctx.project,
          parent_id: parentId,
          attached_files: originalMessage.attached_files || undefined,
          model: deps.selectedModel
        })
      });

      if (!response.ok) {
        const data = await response.json();
        if (
          response.status === 412 &&
          data.detail &&
          typeof data.detail === 'object' &&
          data.detail.kind === 'characters_profile_missing'
        ) {
          deps.setError(data.detail.banner || 'character_profile.di is missing.');
        } else if (data.detail && typeof data.detail === 'object') {
          deps.setError(data.detail.banner || data.detail.message || JSON.stringify(data.detail));
        } else {
          deps.setError(data.detail || 'Failed to regenerate response');
        }
        if (!ctx.isStale()) {
          deps.setMessages(originalMessages);
          deps.setAllMessages(originalAllMessages);
          deps.setCurrentLeafId(originalLeafId);
        }
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) throw new Error('No response body');

      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const events = parseSSEEvents(buffer);
        const lastNewline = buffer.lastIndexOf('\n\n');
        if (lastNewline !== -1) {
          buffer = buffer.slice(lastNewline + 2);
        }

        for (const event of events) {
          if (ctx.isStale()) break;

          if (event.type === 'init') {
            userMsgId = event.data.user_message_id;
          } else if (event.type === 'docs_refreshed') {
            deps.setDocsRefreshed(true);
            setTimeout(() => deps.setDocsRefreshed(false), 4000);
          } else if (event.type === 'pipeline_stage') {
            deps.setPipelineStage(prev => {
              const next = new Map(prev);
              next.set(ctx.chat!, { stage: event.data.stage, status: event.data.status });
              return next;
            });
          } else if (event.type === 'content') {
            if (shipCombatChaining) {
              shipCombatChainContent += event.data.delta;
            } else {
              accumulatedContent += event.data.delta;
            }
            deps.setMessages(prev => {
              const newMessages = [...prev];
              const lastIdx = newMessages.length - 1;
              if (lastIdx >= 0 && newMessages[lastIdx].role === 'assistant') {
                newMessages[lastIdx] = {
                  ...newMessages[lastIdx],
                  content: shipCombatChaining ? shipCombatChainContent : accumulatedContent
                };
              }
              return newMessages;
            });
          } else if (event.type === 'thinking') {
            if (shipCombatChaining) {
              shipCombatChainThinking += event.data.delta;
            } else {
              accumulatedThinking += event.data.delta;
            }
            deps.setMessages(prev => {
              const newMessages = [...prev];
              const lastIdx = newMessages.length - 1;
              if (lastIdx >= 0 && newMessages[lastIdx].role === 'assistant') {
                newMessages[lastIdx] = {
                  ...newMessages[lastIdx],
                  reasoning: shipCombatChaining ? shipCombatChainThinking : accumulatedThinking
                };
              }
              return newMessages;
            });
          } else if (event.type === 'state_update') {
            deps.setPipelineState(event.data);
          } else if (event.type === 'state_notifications') {
            deps.setStateNotifications(event.data);
          } else if (event.type === 'channel_changed') {
            deps.setPipelineState((prev: any) => prev
              ? { ...prev, characters_state: { ...(prev.characters_state || {}), channel: event.data.channel } }
              : { characters_state: { channel: event.data.channel } });
          } else if (event.type === 'hack_mode_start' || event.type === 'hack_state_update') {
            deps.setHackState(event.data);
          } else if (event.type === 'hack_complete') {
            deps.setHackState(null);
          } else if (event.type === 'chase_mode_start') {
            // Chase state lives only in pipeline_state.chase (no parallel hook).
            // Merge the seeded chase dict into pipeline_state so the UI picks
            // up route, vehicles, grid, etc. immediately.
            deps.setPipelineState((prev: any) => prev ? { ...prev, chase: event.data } : { chase: event.data });
          } else if (event.type === 'artifact_update') {
            const doc = event.data as Artifact;
            if (doc.doc_id) {
              deps.setArtifacts(prev => ({ ...prev, [doc.doc_id]: doc }));
            }
          } else if (event.type === 'done') {
            const data = event.data;

            if (data.channel_only) {
              // Pure channel switch on an edit-resubmit (rare). Roll the chat
              // back to before the edit fired — no message was saved backend-side.
              deps.setMessages(truncatedMessages);
              deps.fetchUserStats();
              deps.fetchFreeTokens();
              continue;
            }

            const newUserMessage: ChatMessage = {
              id: data.user_message_id,
              parent_id: parentId,
              role: 'user',
              content: editedMessage.content,
              timestamp: new Date().toISOString(),
              attached_files: originalMessage.attached_files
            };
            if (data.ship_combat_mode) (newUserMessage as any).ship_combat_mode = true;
            if (data.net_combat_mode) (newUserMessage as any).net_combat_mode = true;
            if (data.chase_mode) (newUserMessage as any).chase_mode = true;
            if (data._characters_interview_mode) (newUserMessage as any)._characters_interview_mode = true;

            const assistantMessage: ChatMessage = {
              id: data.assistant_message_id,
              parent_id: (data.ship_combat_init_message && (data.ship_combat_init_message as any).ship_combat_hidden_init)
                ? data.ship_combat_init_message.id
                : data.user_message_id,
              role: 'assistant',
              content: data.assistant_message,
              timestamp: new Date().toISOString(),
              tokens: data.tokens,
              cost: data.cost,
              reasoning: data.reasoning,
              model: data.model,
              service_tier: data.service_tier
            };
            if (data.ship_combat_mode) (assistantMessage as any).ship_combat_mode = true;
            if (data.net_combat_mode) (assistantMessage as any).net_combat_mode = true;
            if (data.chase_mode) (assistantMessage as any).chase_mode = true;
            if (data.sex_mode) (assistantMessage as any).sex_mode = true;
            if (data._characters_interview_mode) (assistantMessage as any)._characters_interview_mode = true;
            if (data.ship_combat_started) (assistantMessage as any).ship_combat_started = true;
            if (data.ship_combat_opening_narration) (assistantMessage as any).ship_combat_opening_narration = data.ship_combat_opening_narration;
            if (typeof data.ship_combat_opening_embedded === 'boolean') (assistantMessage as any).ship_combat_opening_embedded = data.ship_combat_opening_embedded;
            // Stamp the message with the post-pipeline HUD so ChatView's per-message
            // timestamp matches the live sidebar clock immediately, without waiting
            // for a page reload to fetch the saved pipeline_state_after.
            if (data.hud_state) (assistantMessage as any).pipeline_state_after = { hud_state: data.hud_state };
            if (data.artifact_ops) assistantMessage.artifact_ops = data.artifact_ops;
            if (data.artifacts) deps.setArtifacts(data.artifacts);
            // Per-agent cost / usage breakdown — render breakdown immediately
            mergeSideAgentFields(assistantMessage, data);
            const hiddenInitMessage = (data.ship_combat_init_message && (data.ship_combat_init_message as any).ship_combat_hidden_init)
              ? (data.ship_combat_init_message as ChatMessage)
              : null;

            if (!ctx.isStale()) {
              const finalMessages = hiddenInitMessage
                ? [...truncatedMessages, newUserMessage, hiddenInitMessage, assistantMessage]
                : [...truncatedMessages, newUserMessage, assistantMessage];
              deps.setMessages(finalMessages);
              deps.setAllMessages(prev => hiddenInitMessage
                ? [...prev, newUserMessage, hiddenInitMessage, assistantMessage]
                : [...prev, newUserMessage, assistantMessage]);
              deps.setCurrentLeafId(data.current_leaf_id || data.assistant_message_id);
              deps.setStats(data.stats);
              deps.setContextStartIndex(data.context_start_index || 1);
              // Update model dropdown to reflect what actually ran
              if (data.original_model && (data.hack_complete || data.combat_complete || data.ship_combat_complete || data.net_combat_complete || data.sex_complete)) {
                  deps.setSelectedModel(data.original_model);
              } else if (data.model) {
                  deps.setSelectedModel(data.model);
              }

              const branchTotalMessages = data.total_messages || (finalMessages.length + 1);
              deps.setTotalMessages(branchTotalMessages);
              deps.setHasMoreMessages(branchTotalMessages > finalMessages.length + 1);
              deps.setMessageOffset(finalMessages.length);

              deps.fetchUserStats();
              deps.fetchFreeTokens();
            }
          } else if (event.type === 'ship_combat_auto_init') {
            shipCombatChaining = true;
            shipCombatChainContent = '';
            shipCombatChainThinking = '';
            const chainPlaceholder: ChatMessage = {
              role: 'assistant',
              content: '',
              timestamp: new Date().toISOString()
            };
            deps.setMessages(prev => [...prev, chainPlaceholder]);
          } else if (event.type === 'ship_combat_done') {
            const data = event.data;
            shipCombatChaining = false;
            const hiddenInitMessage = (data.ship_combat_init_message && (data.ship_combat_init_message as any).ship_combat_hidden_init)
              ? (data.ship_combat_init_message as ChatMessage)
              : null;
            const chainAddedCount = hiddenInitMessage ? 2 : 1;
            const assistantMessage: ChatMessage = {
              id: data.assistant_message_id,
              parent_id: hiddenInitMessage?.id || data.user_message_id,
              role: 'assistant',
              content: data.assistant_message || shipCombatChainContent,
              timestamp: new Date().toISOString(),
              tokens: data.tokens,
              cost: data.cost,
              reasoning: shipCombatChainThinking || data.reasoning,
              model: data.model,
              service_tier: data.service_tier
            };
            if (data.ship_combat_mode) (assistantMessage as any).ship_combat_mode = true;
            if (data.ship_combat_started) (assistantMessage as any).ship_combat_started = true;
            if (data.ship_combat_opening_narration) (assistantMessage as any).ship_combat_opening_narration = data.ship_combat_opening_narration;
            if (typeof data.ship_combat_opening_embedded === 'boolean') (assistantMessage as any).ship_combat_opening_embedded = data.ship_combat_opening_embedded;
            if (data.chase_mode) (assistantMessage as any).chase_mode = true;
            if (data.chase_started) (assistantMessage as any).chase_started = true;
            if (data.hud_state) (assistantMessage as any).pipeline_state_after = { hud_state: data.hud_state };

            deps.setMessages(prev => {
              const base = prev.slice(0, -1); // drop chain placeholder
              return hiddenInitMessage ? [...base, hiddenInitMessage, assistantMessage] : [...base, assistantMessage];
            });
            deps.setAllMessages(prev => hiddenInitMessage
              ? [...prev, hiddenInitMessage, assistantMessage]
              : [...prev, assistantMessage]);
            deps.setCurrentLeafId(data.current_leaf_id || data.assistant_message_id);
            deps.setStats(data.stats);
            deps.setContextStartIndex(data.context_start_index || 1);
            deps.setTotalMessages(prev => data.total_messages || (prev + chainAddedCount));
            deps.setMessageOffset(prev => prev + chainAddedCount);
            if (data.original_model && data.ship_combat_complete) {
              deps.setSelectedModel(data.original_model);
            } else if (data.model) {
              deps.setSelectedModel(data.model);
            }
            deps.fetchUserStats();
            deps.fetchFreeTokens();
          } else if (event.type === 'ship_combat_error') {
            shipCombatChaining = false;
            shipCombatChainContent = '';
            shipCombatChainThinking = '';
            deps.setError(event.data.detail || 'Ship combat init failed');
            if (!ctx.isStale()) {
              deps.setMessages(prev => prev.slice(0, -1)); // remove chain placeholder only
            }
          } else if (event.type === 'error') {
            deps.setError(event.data.detail || 'Failed to regenerate response');
            if (!ctx.isStale()) {
              deps.setMessages(originalMessages);
              deps.setAllMessages(originalAllMessages);
              deps.setCurrentLeafId(originalLeafId);
            }
          }
        }
      }
    } catch (err) {
      if ((err as Error).name === 'AbortError') {
        if (!ctx.isStale()) {
          deps.setMessages(originalMessages);
          deps.setAllMessages(originalAllMessages);
          deps.setCurrentLeafId(originalLeafId);
        }
      } else {
        console.error('Error saving edited message:', err);
        deps.setError(`Could not save edited message: ${(err as Error).message || err}`);
        if (!ctx.isStale()) {
          deps.setMessages(originalMessages);
          deps.setAllMessages(originalAllMessages);
          deps.setCurrentLeafId(originalLeafId);
        }
      }
    } finally {
      deps.setIsLoading(prev => {
        const next = new Set(prev);
        next.delete(ctx.chat!);
        return next;
      });
      deps.setPipelineStage(prev => {
        const next = new Map(prev);
        next.delete(ctx.chat!);
        return next;
      });
    }
  };

  const sendMessage = async (overrideText?: string) => {
    // overrideText lets callers (e.g. the slash-command picker) bypass the
    // input-state read so we don't race the React render after setNewMessage.
    const effectiveText = overrideText !== undefined ? overrideText : newMessage;
    if (!effectiveText.trim() || !deps.user || !deps.currentChat || deps.isLoading.has(deps.currentChat)) return;
    deps.setStateNotifications([]);

    const ctx = createContextGuard();

    // Handle /sex (no args) → end scene via API
    const trimmedMsg = effectiveText.trim();
    if (trimmedMsg.toLowerCase() === '/sex' && deps.currentProject) {
      setNewMessage('');
      deps.setIsLoading(prev => new Set(prev).add(ctx.chat!));
      try {
        const response = await fetch('/api/end-sex-scene', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            username: deps.user.username,
            chat_name: ctx.chat,
            project: ctx.project
          })
        });
        if (!response.ok) {
          const err = await response.json().catch(() => ({ detail: 'Failed to end scene' }));
          deps.setError(err.detail || 'Failed to end sex scene');
        } else {
          const payload = await response.json().catch(() => null);
          if (payload?.model) {
            deps.setSelectedModel(payload.model);
          }
          // Clear sex_scene from pipeline state so CharacterPanel updates
          (deps.setPipelineState as any)((prev: any) => prev ? { ...prev, sex_scene: null } : prev);
        }
      } catch (err) {
        deps.setError('Failed to end sex scene');
      } finally {
        deps.setIsLoading(prev => { const next = new Set(prev); next.delete(ctx.chat!); return next; });
      }
      return;
    }

    const messageText = effectiveText;
    const filesToSend = [...stagedFiles];

    setNewMessage('');
    setStagedFiles([]);
    deps.setIsLoading(prev => new Set(prev).add(ctx.chat!));

    // Optimistically add user message (without ID yet - will be updated from response)
    const optimisticUserMsg: ChatMessage = {
      role: 'user',
      content: messageText,
      timestamp: new Date().toISOString(),
      attached_files: filesToSend.length > 0 ? filesToSend : undefined
    };
    deps.setMessages(prev => [...prev, optimisticUserMsg]);
    deps.setTotalMessages(prev => prev + 1);

    // Add placeholder for streaming assistant message
    const streamingAssistantMsg: ChatMessage = {
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString()
    };
    deps.setMessages(prev => [...prev, streamingAssistantMsg]);

    // Track accumulated content for the streaming message
    let accumulatedContent = '';
    let accumulatedThinking = '';
    let shipCombatChainContent = '';
    let shipCombatChainThinking = '';
    let shipCombatChaining = false;
    let userMsgId: string | null = null;

    // Create AbortController for cancellation
    const abortController = new AbortController();

    try {
      const response = await fetch('/api/send-message-stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: deps.user.username,
          chat_name: ctx.chat,
          message: messageText,
          project: ctx.project,
          attached_files: filesToSend.length > 0 ? filesToSend : undefined,
          model: deps.selectedModel
        }),
        signal: abortController.signal
      });

      if (!response.ok) {
        const data = await response.json();
        // Characters gamesystem hard-fail: surface as banner modal instead of generic error
        if (
          response.status === 412 &&
          data.detail &&
          typeof data.detail === 'object' &&
          data.detail.kind === 'characters_profile_missing'
        ) {
          deps.setError(data.detail.banner || 'character_profile.di is missing.');
        } else if (data.detail && typeof data.detail === 'object') {
          deps.setError(data.detail.banner || data.detail.message || JSON.stringify(data.detail));
        } else {
          deps.setError(data.detail || 'Failed to send message');
        }
        if (!ctx.isStale()) {
          // Remove both optimistic messages
          deps.setMessages(prev => prev.slice(0, -2));
          deps.setTotalMessages(prev => prev - 1);
          setNewMessage(messageText);
          setStagedFiles(filesToSend);
        }
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error('No response body');
      }

      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // Process complete events from buffer
        const events = parseSSEEvents(buffer);

        // Keep any incomplete event data in the buffer
        const lastNewline = buffer.lastIndexOf('\n\n');
        if (lastNewline !== -1) {
          buffer = buffer.slice(lastNewline + 2);
        }

        for (const event of events) {
          if (ctx.isStale()) break;

          if (event.type === 'init') {
            userMsgId = event.data.user_message_id;
          } else if (event.type === 'docs_refreshed') {
            deps.setDocsRefreshed(true);
            setTimeout(() => deps.setDocsRefreshed(false), 4000);
          } else if (event.type === 'pipeline_stage') {
            deps.setPipelineStage(prev => {
              const next = new Map(prev);
              next.set(ctx.chat!, { stage: event.data.stage, status: event.data.status });
              return next;
            });
          } else if (event.type === 'content') {
            if (shipCombatChaining) {
              shipCombatChainContent += event.data.delta;
            } else {
              accumulatedContent += event.data.delta;
            }
            const displayContent = shipCombatChaining ? shipCombatChainContent : accumulatedContent;
            // Update the streaming message with new content
            deps.setMessages(prev => {
              const newMessages = [...prev];
              const lastIdx = newMessages.length - 1;
              if (lastIdx >= 0 && newMessages[lastIdx].role === 'assistant') {
                newMessages[lastIdx] = {
                  ...newMessages[lastIdx],
                  content: displayContent
                };
              }
              return newMessages;
            });
            // Don't auto-scroll during streaming - let user read from the top
          } else if (event.type === 'thinking') {
            if (shipCombatChaining) {
              shipCombatChainThinking += event.data.delta;
            } else {
              accumulatedThinking += event.data.delta;
            }
            const displayThinking = shipCombatChaining ? shipCombatChainThinking : accumulatedThinking;
            // Update reasoning in real-time
            deps.setMessages(prev => {
              const newMessages = [...prev];
              const lastIdx = newMessages.length - 1;
              if (lastIdx >= 0 && newMessages[lastIdx].role === 'assistant') {
                newMessages[lastIdx] = {
                  ...newMessages[lastIdx],
                  reasoning: displayThinking
                };
              }
              return newMessages;
            });
          } else if (event.type === 'state_update') {
            deps.setPipelineState(event.data);
          } else if (event.type === 'state_notifications') {
            deps.setStateNotifications(event.data);
          } else if (event.type === 'channel_changed') {
            // Pure channel switch (Characters /text /phone /inperson /video).
            // Backend short-circuited — no model call, no message saved.
            // Just update the channel chip so the user can see the new mode.
            deps.setPipelineState((prev: any) => prev
              ? { ...prev, characters_state: { ...(prev.characters_state || {}), channel: event.data.channel } }
              : { characters_state: { channel: event.data.channel } });
          } else if (event.type === 'hack_mode_start' || event.type === 'hack_state_update') {
            deps.setHackState(event.data);
          } else if (event.type === 'hack_complete') {
            deps.setHackState(null);
          } else if (event.type === 'chase_mode_start') {
            // Chase state lives only in pipeline_state.chase (no parallel hook).
            // Merge the seeded chase dict into pipeline_state so the UI picks
            // up route, vehicles, grid, etc. immediately.
            deps.setPipelineState((prev: any) => prev ? { ...prev, chase: event.data } : { chase: event.data });
          } else if (event.type === 'artifact_update') {
            const doc = event.data as Artifact;
            if (doc.doc_id) {
              deps.setArtifacts(prev => ({ ...prev, [doc.doc_id]: doc }));
            }
          } else if (event.type === 'done') {
            const data = event.data;

            if (data.channel_only) {
              // Pure channel switch: backend rolled back the user message and
              // saved no assistant reply. Strip the optimistic user msg + the
              // streaming-assistant placeholder we eagerly appended.
              deps.setMessages(prev => prev.slice(0, -2));
              deps.setTotalMessages(prev => Math.max(0, prev - 1));
              deps.fetchUserStats();
              deps.fetchFreeTokens();
            } else if (data.sex_mode_handoff) {
              // Sex handoff: messages were deleted backend-side — remove optimistic messages, update state only
              deps.setMessages(prev => prev.slice(0, -2));
              deps.setTotalMessages(prev => prev - 1); // reverse optimistic +1 from submission
              deps.setCurrentLeafId(data.current_leaf_id);
              deps.setStats(data.stats);
              deps.setContextStartIndex(data.context_start_index || 1);
              if (data.model) deps.setSelectedModel(data.model);
              deps.fetchUserStats();
              deps.fetchFreeTokens();
            } else {
            // Build complete messages with IDs from response
            const userMsgWithId: ChatMessage = {
              ...optimisticUserMsg,
              id: data.user_message_id,
              parent_id: deps.currentLeafId
            };

            const assistantMessage: ChatMessage = {
              id: data.assistant_message_id,
              parent_id: (data.ship_combat_init_message && (data.ship_combat_init_message as any).ship_combat_hidden_init)
                ? data.ship_combat_init_message.id
                : data.user_message_id,
              role: 'assistant',
              content: data.assistant_message,
              timestamp: new Date().toISOString(),
              tokens: data.tokens,
              cost: data.cost,
              reasoning: data.reasoning,
              model: data.model,
              service_tier: data.service_tier,
              ...(data.hack_mode ? { hack_mode: true } : {}),
              ...(data.sex_mode ? { sex_mode: true } : {}),
              ...(data.sex_mode_handoff ? { sex_mode_handoff: true } : {})
            };
            if (data.ship_combat_mode) (assistantMessage as any).ship_combat_mode = true;
            if (data.net_combat_mode) (assistantMessage as any).net_combat_mode = true;
            if (data.chase_mode) (assistantMessage as any).chase_mode = true;
            if (data.ship_combat_started) (assistantMessage as any).ship_combat_started = true;
            if (data.chase_started) (assistantMessage as any).chase_started = true;
            if (data.ship_combat_opening_narration) (assistantMessage as any).ship_combat_opening_narration = data.ship_combat_opening_narration;
            if (typeof data.ship_combat_opening_embedded === 'boolean') {
              (assistantMessage as any).ship_combat_opening_embedded = data.ship_combat_opening_embedded;
            }
            if (data.artifact_ops) assistantMessage.artifact_ops = data.artifact_ops;
            if (data.artifacts) deps.setArtifacts(data.artifacts);
            // Per-agent cost / usage breakdown — render breakdown immediately
            mergeSideAgentFields(assistantMessage, data);
            if (data.hack_mode) {
              userMsgWithId.hack_mode = true;
            }
            if (data.ship_combat_mode) {
              (userMsgWithId as any).ship_combat_mode = true;
            }
            if (data.net_combat_mode) {
              (userMsgWithId as any).net_combat_mode = true;
            }
            if (data.chase_mode) {
              (userMsgWithId as any).chase_mode = true;
            }
            if (data.sex_mode) {
              (userMsgWithId as any).sex_mode = true;
            }
            // Characters interview mode: tag both user and assistant so the
            // parchment styling kicks in immediately, not just after refresh.
            if (data._characters_interview_mode) {
              (userMsgWithId as any)._characters_interview_mode = true;
              (assistantMessage as any)._characters_interview_mode = true;
            }
            const hiddenInitMessage = (data.ship_combat_init_message && (data.ship_combat_init_message as any).ship_combat_hidden_init)
              ? (data.ship_combat_init_message as ChatMessage)
              : null;

            // Replace optimistic messages with complete ones
            deps.setMessages(prev => hiddenInitMessage
              ? [...prev.slice(0, -2), userMsgWithId, hiddenInitMessage, assistantMessage]
              : [...prev.slice(0, -2), userMsgWithId, assistantMessage]);

            // Add to the full message tree
            deps.setAllMessages(prev => hiddenInitMessage
              ? [...prev, userMsgWithId, hiddenInitMessage, assistantMessage]
              : [...prev, userMsgWithId, assistantMessage]);

            // Update current leaf
            deps.setCurrentLeafId(data.current_leaf_id || data.assistant_message_id);

            deps.setTotalMessages(prev => prev + (hiddenInitMessage ? 2 : 1));
            deps.setMessageOffset(prev => prev + (hiddenInitMessage ? 3 : 2));
            deps.setStats(data.stats);
            deps.setContextStartIndex(data.context_start_index || 1);
            // Update model dropdown to reflect what actually ran
            if (data.original_model && (data.hack_complete || data.combat_complete || data.ship_combat_complete || data.net_combat_complete || data.sex_complete)) {
                // Auto-switched mode just ended — restore to original model
                deps.setSelectedModel(data.original_model);
            } else if (data.model) {
                deps.setSelectedModel(data.model);
            }
            deps.fetchUserStats();
            deps.fetchFreeTokens();
            } // end non-handoff
            // Don't scroll on done - let user stay where they were reading
          } else if (event.type === 'ship_combat_auto_init') {
            // Backend chained ship combat init — add a new assistant message placeholder
            shipCombatChaining = true;
            shipCombatChainContent = '';
            shipCombatChainThinking = '';
            deps.setStateNotifications([]);
            const chainPlaceholder: ChatMessage = {
              role: 'assistant',
              content: '',
              timestamp: new Date().toISOString()
            };
            deps.setMessages(prev => [...prev, chainPlaceholder]);
          } else if (event.type === 'ship_combat_done') {
            // Finalize the chained ship combat assistant message
            const data = event.data;
            shipCombatChaining = false;
            const hiddenInitMessage = (data.ship_combat_init_message && (data.ship_combat_init_message as any).ship_combat_hidden_init)
              ? (data.ship_combat_init_message as ChatMessage)
              : null;
            const assistantMessage: ChatMessage = {
              id: data.assistant_message_id,
              parent_id: hiddenInitMessage?.id || data.user_message_id,
              role: 'assistant',
              content: data.assistant_message,
              timestamp: new Date().toISOString(),
              tokens: data.tokens,
              cost: data.cost,
              reasoning: data.reasoning,
              model: data.model,
              service_tier: data.service_tier
            };
            if (data.ship_combat_mode) (assistantMessage as any).ship_combat_mode = true;
            if (data.ship_combat_started) (assistantMessage as any).ship_combat_started = true;
            if (data.ship_combat_opening_narration) (assistantMessage as any).ship_combat_opening_narration = data.ship_combat_opening_narration;
            if (typeof data.ship_combat_opening_embedded === 'boolean') (assistantMessage as any).ship_combat_opening_embedded = data.ship_combat_opening_embedded;
            if (data.chase_mode) (assistantMessage as any).chase_mode = true;
            if (data.chase_started) (assistantMessage as any).chase_started = true;

            deps.setMessages(prev => {
              const base = prev.slice(0, -1);  // Remove chain placeholder
              if (hiddenInitMessage) return [...base, hiddenInitMessage, assistantMessage];
              return [...base, assistantMessage];
            });
            deps.setAllMessages(prev => hiddenInitMessage
              ? [...prev, hiddenInitMessage, assistantMessage]
              : [...prev, assistantMessage]);
            deps.setCurrentLeafId(data.current_leaf_id || data.assistant_message_id);
            deps.setTotalMessages(prev => prev + (hiddenInitMessage ? 2 : 1));
            deps.setMessageOffset(prev => prev + (hiddenInitMessage ? 2 : 1));
            deps.setStats(data.stats);
            deps.setContextStartIndex(data.context_start_index || 1);
            if (data.original_model && data.ship_combat_complete) {
              deps.setSelectedModel(data.original_model);
            } else if (data.model) {
              deps.setSelectedModel(data.model);
            }
            deps.fetchUserStats();
            deps.fetchFreeTokens();
          } else if (event.type === 'ship_combat_error') {
            shipCombatChaining = false;
            shipCombatChainContent = '';
            shipCombatChainThinking = '';
            deps.setError(event.data.detail || 'Ship combat init failed');
            if (!ctx.isStale()) {
              deps.setMessages(prev => prev.slice(0, -1)); // remove chain placeholder only
            }
          } else if (event.type === 'error') {
            deps.setError(event.data.detail || 'Failed to send message');
            if (!ctx.isStale()) {
              deps.setMessages(prev => prev.slice(0, -2));
              deps.setTotalMessages(prev => prev - 1);
              setNewMessage(messageText);
              setStagedFiles(filesToSend);
            }
          }
        }
      }
    } catch (err) {
      if ((err as Error).name === 'AbortError') {
        // Request was cancelled - clean up
        if (!ctx.isStale()) {
          deps.setMessages(prev => prev.slice(0, -2));
          deps.setTotalMessages(prev => prev - 1);
        }
      } else {
        deps.setError(`Could not send message: ${(err as Error).message || err}`);
        if (!ctx.isStale()) {
          deps.setMessages(prev => prev.slice(0, -2));
          deps.setTotalMessages(prev => prev - 1);
          setNewMessage(messageText);
          setStagedFiles(filesToSend);
        }
      }
    } finally {
      deps.setIsLoading(prev => {
        const next = new Set(prev);
        next.delete(ctx.chat!);
        return next;
      });
      deps.setPipelineStage(prev => {
        const next = new Map(prev);
        next.delete(ctx.chat!);
        return next;
      });
    }
  };

  // Chat file attachment handlers
  const handleChatFileSelect = async (event: React.ChangeEvent<HTMLInputElement>) => {
    if (!event.target.files) return;

    const files = Array.from(event.target.files);
    const newStagedFiles: {filename: string, content: string, mime_type?: string}[] = [];

    const TEXT_EXTS = ['txt', 'md', 'yaml', 'yml'];
    const IMAGE_MIME = new Set(['image/png', 'image/jpeg', 'image/jpg', 'image/gif', 'image/webp']);
    const MAX_IMAGE_BYTES = 5 * 1024 * 1024;  // 5MB

    for (const file of files) {
      const ext = file.name.split('.').pop()?.toLowerCase();
      const mime = file.type || '';
      const isImage = IMAGE_MIME.has(mime);
      const isText = TEXT_EXTS.includes(ext || '');

      if (!isImage && !isText) {
        deps.setError(`${file.name}: only .txt/.md/.yaml/.yml or image files (PNG/JPEG/GIF/WebP) are allowed`);
        continue;
      }

      if (isImage && file.size > MAX_IMAGE_BYTES) {
        deps.setError(`${file.name}: image is over 5MB; please use a smaller version`);
        continue;
      }

      try {
        if (isImage) {
          // Read as base64 (strip the data URL prefix). Backend stores this
          // base64 string and re-emits it as an Anthropic image content block.
          const dataUrl: string = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result as string);
            reader.onerror = () => reject(new Error('FileReader failed'));
            reader.readAsDataURL(file);
          });
          const commaIdx = dataUrl.indexOf(',');
          const b64 = commaIdx >= 0 ? dataUrl.slice(commaIdx + 1) : dataUrl;
          // Normalize image/jpg → image/jpeg (Anthropic expects the canonical form)
          const normalizedMime = mime === 'image/jpg' ? 'image/jpeg' : mime;
          newStagedFiles.push({ filename: file.name, content: b64, mime_type: normalizedMime });
        } else {
          const content = await file.text();
          newStagedFiles.push({ filename: file.name, content });
        }
      } catch (err) {
        deps.setError(`Could not read ${file.name}`);
      }
    }

    if (newStagedFiles.length > 0) {
      setStagedFiles(prev => [...prev, ...newStagedFiles]);
    }

    if (chatFileInputRef.current) {
      chatFileInputRef.current.value = '';
    }
    setShowAttachMenu(false);
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDraggingFile(true);
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDraggingFile(false);
  };

  const handleDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDraggingFile(false);

    const files = Array.from(e.dataTransfer.files);
    const newStagedFiles: {filename: string, content: string}[] = [];

    for (const file of files) {
      const ext = file.name.split('.').pop()?.toLowerCase();
      if (!['txt', 'md', 'yaml', 'yml'].includes(ext || '')) {
        deps.setError(`${file.name}: Only .txt, .md, .yaml, and .yml files are allowed`);
        continue;
      }

      try {
        const content = await file.text();
        newStagedFiles.push({ filename: file.name, content });
      } catch (err) {
        deps.setError(`Could not read ${file.name}`);
      }
    }

    if (newStagedFiles.length > 0) {
      setStagedFiles(prev => [...prev, ...newStagedFiles]);
    }
  };

  const removeStagedFile = (index: number) => {
    setStagedFiles(prev => prev.filter((_, i) => i !== index));
  };

  return {
    newMessage,
    setNewMessage,
    stagedFiles,
    setStagedFiles,
    showAttachMenu,
    setShowAttachMenu,
    isDraggingFile,
    textareaHeight,
    setTextareaHeight,
    chatFileInputRef,
    sendMessage,
    saveEditedMessage,
    parseSSEEvents,
    handleChatFileSelect,
    handleDragOver,
    handleDragLeave,
    handleDrop,
    removeStagedFile,
  };
}
