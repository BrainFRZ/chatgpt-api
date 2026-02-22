import { useState, useRef, useCallback } from 'react';
import { ChatMessage, ChatStats } from '../types';

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
  editingMessageIndex: number | null;
  editingMessageContent: string;
  setEditingMessageIndex: (v: number | null) => void;
  setEditingMessageContent: (v: string) => void;
  fetchUserStats: () => void;
  fetchFreeTokens: () => void;
}

export function useMessaging(deps: UseMessagingDeps) {
  const [newMessage, setNewMessage] = useState('');
  const [stagedFiles, setStagedFiles] = useState<{filename: string, content: string}[]>([]);
  const [showAttachMenu, setShowAttachMenu] = useState(false);
  const [isDraggingFile, setIsDraggingFile] = useState(false);
  const [textareaHeight, setTextareaHeight] = useState(85);

  const chatFileInputRef = useRef<HTMLInputElement>(null);

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
        deps.setError(data.detail || 'Failed to regenerate response');
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
            accumulatedContent += event.data.delta;
            deps.setMessages(prev => {
              const newMessages = [...prev];
              const lastIdx = newMessages.length - 1;
              if (lastIdx >= 0 && newMessages[lastIdx].role === 'assistant') {
                newMessages[lastIdx] = { ...newMessages[lastIdx], content: accumulatedContent };
              }
              return newMessages;
            });
          } else if (event.type === 'thinking') {
            accumulatedThinking += event.data.delta;
            deps.setMessages(prev => {
              const newMessages = [...prev];
              const lastIdx = newMessages.length - 1;
              if (lastIdx >= 0 && newMessages[lastIdx].role === 'assistant') {
                newMessages[lastIdx] = { ...newMessages[lastIdx], reasoning: accumulatedThinking };
              }
              return newMessages;
            });
          } else if (event.type === 'state_update') {
            deps.setPipelineState(event.data);
          } else if (event.type === 'state_notifications') {
            deps.setStateNotifications(event.data);
          } else if (event.type === 'hack_mode_start' || event.type === 'hack_state_update') {
            deps.setHackState(event.data);
          } else if (event.type === 'hack_complete') {
            deps.setHackState(null);
          } else if (event.type === 'done') {
            const data = event.data;

            const newUserMessage: ChatMessage = {
              id: data.user_message_id,
              parent_id: parentId,
              role: 'user',
              content: editedMessage.content,
              timestamp: new Date().toISOString(),
              attached_files: originalMessage.attached_files
            };

            const assistantMessage: ChatMessage = {
              id: data.assistant_message_id,
              parent_id: data.user_message_id,
              role: 'assistant',
              content: data.assistant_message,
              timestamp: new Date().toISOString(),
              tokens: data.tokens,
              cost: data.cost,
              reasoning: data.reasoning,
              model: data.model,
              service_tier: data.service_tier
            };

            if (!ctx.isStale()) {
              const finalMessages = [...truncatedMessages, newUserMessage, assistantMessage];
              deps.setMessages(finalMessages);
              deps.setAllMessages(prev => [...prev, newUserMessage, assistantMessage]);
              deps.setCurrentLeafId(data.current_leaf_id || data.assistant_message_id);
              deps.setStats(data.stats);
              deps.setContextStartIndex(data.context_start_index || 1);

              const branchTotalMessages = data.total_messages || (finalMessages.length + 1);
              deps.setTotalMessages(branchTotalMessages);
              deps.setHasMoreMessages(branchTotalMessages > finalMessages.length + 1);
              deps.setMessageOffset(finalMessages.length);

              deps.fetchUserStats();
              deps.fetchFreeTokens();
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

  const sendMessage = async () => {
    if (!newMessage.trim() || !deps.user || !deps.currentChat || deps.isLoading.has(deps.currentChat)) return;
    deps.setStateNotifications([]);

    const ctx = createContextGuard();
    const messageText = newMessage;
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
        deps.setError(data.detail || 'Failed to send message');
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
            accumulatedContent += event.data.delta;
            // Update the streaming message with new content
            deps.setMessages(prev => {
              const newMessages = [...prev];
              const lastIdx = newMessages.length - 1;
              if (lastIdx >= 0 && newMessages[lastIdx].role === 'assistant') {
                newMessages[lastIdx] = {
                  ...newMessages[lastIdx],
                  content: accumulatedContent
                };
              }
              return newMessages;
            });
            // Don't auto-scroll during streaming - let user read from the top
          } else if (event.type === 'thinking') {
            accumulatedThinking += event.data.delta;
            // Update reasoning in real-time
            deps.setMessages(prev => {
              const newMessages = [...prev];
              const lastIdx = newMessages.length - 1;
              if (lastIdx >= 0 && newMessages[lastIdx].role === 'assistant') {
                newMessages[lastIdx] = {
                  ...newMessages[lastIdx],
                  reasoning: accumulatedThinking
                };
              }
              return newMessages;
            });
          } else if (event.type === 'state_update') {
            deps.setPipelineState(event.data);
          } else if (event.type === 'state_notifications') {
            deps.setStateNotifications(event.data);
          } else if (event.type === 'hack_mode_start' || event.type === 'hack_state_update') {
            deps.setHackState(event.data);
          } else if (event.type === 'hack_complete') {
            deps.setHackState(null);
          } else if (event.type === 'done') {
            const data = event.data;

            // Build complete messages with IDs from response
            const userMsgWithId: ChatMessage = {
              ...optimisticUserMsg,
              id: data.user_message_id,
              parent_id: deps.currentLeafId
            };

            const assistantMessage: ChatMessage = {
              id: data.assistant_message_id,
              parent_id: data.user_message_id,
              role: 'assistant',
              content: data.assistant_message,
              timestamp: new Date().toISOString(),
              tokens: data.tokens,
              cost: data.cost,
              reasoning: data.reasoning,
              model: data.model,
              service_tier: data.service_tier,
              ...(data.hack_mode ? { hack_mode: true } : {})
            };
            if (data.hack_mode) {
              userMsgWithId.hack_mode = true;
            }

            // Replace optimistic messages with complete ones
            deps.setMessages(prev => [...prev.slice(0, -2), userMsgWithId, assistantMessage]);

            // Add to the full message tree
            deps.setAllMessages(prev => [...prev, userMsgWithId, assistantMessage]);

            // Update current leaf
            deps.setCurrentLeafId(data.current_leaf_id || data.assistant_message_id);

            deps.setTotalMessages(prev => prev + 1);
            deps.setMessageOffset(prev => prev + 2);
            deps.setStats(data.stats);
            deps.setContextStartIndex(data.context_start_index || 1);
            deps.fetchUserStats();
            deps.fetchFreeTokens();
            // Don't scroll on done - let user stay where they were reading
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
    const newStagedFiles: {filename: string, content: string}[] = [];

    for (const file of files) {
      // Check extension
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

    // Clear input so same file can be selected again
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
