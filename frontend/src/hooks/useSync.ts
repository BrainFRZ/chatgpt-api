import { useState, useEffect, useRef, useCallback } from 'react';
import { ChatMessage, ChatStats } from '../types';

interface UseSyncDeps {
  user: { username: string } | null;
  currentChat: string | null;
  currentProject: string | null;
  currentChatRef: React.MutableRefObject<string | null>;
  currentProjectRef: React.MutableRefObject<string | null>;
  isLoadingRef: React.MutableRefObject<Set<string>>;
  setMessages: React.Dispatch<React.SetStateAction<ChatMessage[]>>;
  setAllMessages: React.Dispatch<React.SetStateAction<ChatMessage[]>>;
  setTotalMessages: React.Dispatch<React.SetStateAction<number>>;
  setCurrentLeafId: (v: string | null) => void;
  setStats: (v: ChatStats | null) => void;
  setContextStartIndex: (v: number) => void;
  setPipelineStage: React.Dispatch<React.SetStateAction<Map<string, {stage: string, status: string}>>>;
  setPipelineState: (v: any) => void;
  setStateNotifications: (v: any[]) => void;
  setSelectedModel: (v: string) => void;
  setAnthropicSync: (v: boolean) => void;
  setDocsRefreshed: (v: boolean) => void;
  setChats: React.Dispatch<React.SetStateAction<string[]>>;
  setProjectChatsCache: React.Dispatch<React.SetStateAction<{[key: string]: string[]}>>;
  setRootChatsCache: React.Dispatch<React.SetStateAction<string[] | null>>;
  resetChatState: () => void;
  fetchUserStats: () => void;
}

export function useSync(deps: UseSyncDeps) {
  const [viewerCount, setViewerCount] = useState(1);
  const [needsSyncReload, setNeedsSyncReload] = useState(false);

  const syncWsRef = useRef<WebSocket | null>(null);
  const syncReconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const syncHeartbeatIntervalRef = useRef<NodeJS.Timeout | null>(null);
  const userWsRef = useRef<WebSocket | null>(null);
  const userWsReconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const userWsHeartbeatIntervalRef = useRef<NodeJS.Timeout | null>(null);

  // Handle sync events from WebSocket
  const handleSyncEvent = useCallback((event: { type: string; data: any }) => {
    // If we're currently streaming (loading) for the current chat, ignore streaming-related
    // sync events since we're already handling them via SSE. This prevents duplicates.
    const isCurrentlyStreaming = deps.currentChatRef.current && deps.isLoadingRef.current.has(deps.currentChatRef.current);

    switch (event.type) {
      case 'client_joined':
      case 'client_left':
        setViewerCount(event.data.connection_count);
        break;

      case 'user_message_added':
        // Ignore if we're the one streaming - we already added our own message
        if (isCurrentlyStreaming) break;
        // Another client sent a message or edited one - add it to our view
        deps.setAllMessages(prev => [...prev, event.data.message]);
        deps.setTotalMessages(prev => prev + 1);
        deps.setCurrentLeafId(event.data.current_leaf_id);
        // Detect branch edits and truncate to show the new branch:
        // 1. Parent is in our path (not the last msg) → truncate after parent
        // 2. Parent is the root/system msg (not in visible path) → find sibling
        //    with same parent_id and truncate before it
        // 3. Otherwise → append normally (new message at end of conversation)
        deps.setMessages(prev => {
          const parentId = event.data.message.parent_id;
          const parentIndex = parentId ? prev.findIndex(m => m.id === parentId) : -1;
          if (parentIndex >= 0 && parentIndex < prev.length - 1) {
            // Parent found in path — truncate after parent
            const base = prev.slice(0, parentIndex + 1);
            return [...base, event.data.message, {
              role: 'assistant', content: '', timestamp: new Date().toISOString()
            }];
          }
          if (parentId && parentIndex === -1) {
            // Parent not in visible path — check if a sibling exists
            // (editing the first user msg, whose parent is the system/root msg)
            const siblingIndex = prev.findIndex(m => m.parent_id === parentId);
            if (siblingIndex >= 0) {
              const base = prev.slice(0, siblingIndex);
              return [...base, event.data.message, {
                role: 'assistant', content: '', timestamp: new Date().toISOString()
              }];
            }
          }
          // Normal append — new message at end of conversation
          return [...prev, event.data.message, {
            role: 'assistant', content: '', timestamp: new Date().toISOString()
          }];
        });
        break;

      case 'stream_content':
        // Ignore if we're the one streaming - we handle this via SSE
        if (isCurrentlyStreaming) break;
        // Append content delta to the streaming message
        deps.setMessages(prev => {
          const newMessages = [...prev];
          const lastIdx = newMessages.length - 1;
          if (lastIdx >= 0 && newMessages[lastIdx].role === 'assistant') {
            newMessages[lastIdx] = {
              ...newMessages[lastIdx],
              content: (newMessages[lastIdx].content || '') + event.data.delta
            };
          }
          return newMessages;
        });
        break;

      case 'stream_thinking':
        // Ignore if we're the one streaming - we handle this via SSE
        if (isCurrentlyStreaming) break;
        // Append thinking delta to the streaming message
        deps.setMessages(prev => {
          const newMessages = [...prev];
          const lastIdx = newMessages.length - 1;
          if (lastIdx >= 0 && newMessages[lastIdx].role === 'assistant') {
            newMessages[lastIdx] = {
              ...newMessages[lastIdx],
              reasoning: (newMessages[lastIdx].reasoning || '') + event.data.delta
            };
          }
          return newMessages;
        });
        break;

      case 'stream_done': {
        // Ignore if we're the one streaming - we handle this via SSE
        if (isCurrentlyStreaming) break;
        // Replace streaming placeholder with complete message
        let hadPlaceholder = false;
        deps.setMessages(prev => {
          const newMessages = [...prev];
          const lastIdx = newMessages.length - 1;
          if (lastIdx >= 0 && newMessages[lastIdx].role === 'assistant' && !newMessages[lastIdx].id) {
            // Normal path: we have a placeholder from user_message_added
            hadPlaceholder = true;
            newMessages[lastIdx] = event.data.assistant_message;
            return newMessages;
          }
          // No placeholder — earlier events were lost
          return prev;
        });
        if (hadPlaceholder) {
          // Update metadata for successful placeholder replacement
          deps.setAllMessages(prev => [...prev, event.data.assistant_message]);
          deps.setTotalMessages(prev => prev + 1);
          deps.setCurrentLeafId(event.data.current_leaf_id);
          deps.setStats(event.data.stats);
          deps.setContextStartIndex(event.data.context_start_index || 1);
          if (event.data.pipeline_state) {
            deps.setPipelineState(event.data.pipeline_state);
          }
          deps.fetchUserStats();
          deps.setPipelineStage(prev => {
            if (!deps.currentChatRef.current) return prev;
            const next = new Map(prev);
            next.delete(deps.currentChatRef.current);
            return next;
          });
        } else {
          // No placeholder existed — reload to get correct branch state
          setNeedsSyncReload(true);
        }
        break;
      }

      case 'stream_error':
        // Ignore if we're the one streaming - we handle this via SSE
        if (isCurrentlyStreaming) break;
        // Remove the streaming placeholder on error
        deps.setMessages(prev => {
          if (prev.length > 0 && prev[prev.length - 1].role === 'assistant' && !prev[prev.length - 1].id) {
            return prev.slice(0, -1);
          }
          return prev;
        });
        deps.setPipelineStage(prev => {
          if (!deps.currentChatRef.current) return prev;
          const next = new Map(prev);
          next.delete(deps.currentChatRef.current);
          return next;
        });
        break;

      case 'pipeline_stage':
        if (isCurrentlyStreaming) break;
        if (deps.currentChatRef.current) {
          deps.setPipelineStage(prev => {
            const next = new Map(prev);
            next.set(deps.currentChatRef.current!, event.data);
            return next;
          });
        }
        break;

      case 'branch_switched':
        // Another client switched branches - trigger a reload via state
        // Using state instead of calling handleReloadChat directly avoids stale closure
        setNeedsSyncReload(true);
        break;

      case 'chat_settings_changed':
        // Another client changed model or sync setting
        if (event.data.model !== undefined) {
          deps.setSelectedModel(event.data.model);
          if (event.data.context_start_index !== undefined) {
            deps.setContextStartIndex(event.data.context_start_index);
          }
        }
        if (event.data.anthropic_sync !== undefined) {
          deps.setAnthropicSync(event.data.anthropic_sync);
        }
        break;

      case 'docs_refreshed':
        deps.setDocsRefreshed(true);
        setTimeout(() => deps.setDocsRefreshed(false), 4000);
        break;

      case 'state_update':
        if (event.data?.pipeline_state) {
          deps.setPipelineState(event.data.pipeline_state);
        }
        break;

      case 'state_notifications':
        if (event.data?.notifications) {
          deps.setStateNotifications(event.data.notifications);
        }
        break;

      case 'bookmark_updated':
        deps.setMessages(prev => prev.map(m => {
          if (m.id === event.data.message_id) {
            const updated = { ...m };
            if (event.data.bookmark) updated.bookmark = event.data.bookmark;
            else delete updated.bookmark;
            return updated;
          }
          return m;
        }));
        deps.setAllMessages(prev => prev.map(m => {
          if (m.id === event.data.message_id) {
            const updated = { ...m };
            if (event.data.bookmark) updated.bookmark = event.data.bookmark;
            else delete updated.bookmark;
            return updated;
          }
          return m;
        }));
        break;

      case 'chat_created':
        // Another client created a chat - update the chat list
        // Add to appropriate list based on whether it's a project chat or root chat
        if (event.data.project) {
          deps.setProjectChatsCache(prev => {
            const projectChats = prev[event.data.project] || [];
            if (!projectChats.includes(event.data.chat_name)) {
              return { ...prev, [event.data.project]: [event.data.chat_name, ...projectChats] };
            }
            return prev;
          });
        } else {
          deps.setChats(prev => {
            if (!prev.includes(event.data.chat_name)) {
              return [event.data.chat_name, ...prev];
            }
            return prev;
          });
          deps.setRootChatsCache(prev => {
            if (prev && !prev.includes(event.data.chat_name)) {
              return [event.data.chat_name, ...prev];
            }
            return prev;
          });
        }
        break;

      case 'chat_deleted':
        // Another client deleted a chat - update the chat list
        if (event.data.project) {
          deps.setProjectChatsCache(prev => {
            const projectChats = prev[event.data.project] || [];
            return { ...prev, [event.data.project]: projectChats.filter((c: string) => c !== event.data.chat_name) };
          });
        } else {
          deps.setChats(prev => prev.filter(c => c !== event.data.chat_name));
          deps.setRootChatsCache(prev => prev ? prev.filter(c => c !== event.data.chat_name) : null);
        }
        // If the deleted chat is currently open (same name AND same project), close it
        if (deps.currentChatRef.current === event.data.chat_name &&
            (deps.currentProjectRef.current || null) === (event.data.project || null)) {
          deps.resetChatState();
        }
        break;
    }
  }, []);

  // Real-time sync WebSocket connection
  useEffect(() => {
    if (!deps.user || !deps.currentChat) {
      // Clean up if no chat is open
      if (syncWsRef.current) {
        syncWsRef.current.close();
        syncWsRef.current = null;
      }
      setViewerCount(1);
      return;
    }

    let reconnectAttempts = 0;
    const maxReconnectAttempts = 5;
    const baseReconnectDelay = 1000;

    const connect = () => {
      // Build WebSocket URL
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const projectParam = deps.currentProject ? `?project=${encodeURIComponent(deps.currentProject)}` : '';
      const wsUrl = `${protocol}//${window.location.host}/api/ws/chat/${encodeURIComponent(deps.user!.username)}/${encodeURIComponent(deps.currentChat!)}${projectParam}`;

      const ws = new WebSocket(wsUrl);
      syncWsRef.current = ws;

      ws.onopen = () => {
        reconnectAttempts = 0;

        // Start heartbeat
        syncHeartbeatIntervalRef.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'ping' }));
          }
        }, 30000); // Ping every 30 seconds
      };

      ws.onmessage = (msgEvent) => {
        try {
          const data = JSON.parse(msgEvent.data);
          // Skip user-level events here - they're handled by the user-level WebSocket
          if (data.type === 'chat_created' || data.type === 'chat_deleted') return;
          handleSyncEvent(data);
        } catch (e) {
          console.error('[WS parse error]', e);
        }
      };

      ws.onclose = (event) => {
        // Clear heartbeat
        if (syncHeartbeatIntervalRef.current) {
          clearInterval(syncHeartbeatIntervalRef.current);
          syncHeartbeatIntervalRef.current = null;
        }

        // Don't reconnect if:
        // - Intentionally closed (1000)
        // - Chat no longer exists (4004 from server)
        // - Chat changed in the UI
        if (event.code === 1000 || event.code === 4004 || deps.currentChatRef.current !== deps.currentChat) {
          return;
        }

        // Attempt reconnect with exponential backoff
        if (reconnectAttempts < maxReconnectAttempts) {
          const delay = baseReconnectDelay * Math.pow(2, reconnectAttempts);
          reconnectAttempts++;
          syncReconnectTimeoutRef.current = setTimeout(connect, delay);
        }
      };

      ws.onerror = () => {
        // Error will trigger onclose
      };
    };

    connect();

    // Cleanup on unmount or chat change
    return () => {
      if (syncWsRef.current) {
        syncWsRef.current.close(1000); // Normal closure
        syncWsRef.current = null;
      }
      if (syncReconnectTimeoutRef.current) {
        clearTimeout(syncReconnectTimeoutRef.current);
        syncReconnectTimeoutRef.current = null;
      }
      if (syncHeartbeatIntervalRef.current) {
        clearInterval(syncHeartbeatIntervalRef.current);
        syncHeartbeatIntervalRef.current = null;
      }
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deps.user?.username, deps.currentChat, deps.currentProject]);

  // User-level WebSocket for chat list sync (always connected when logged in)
  useEffect(() => {
    if (!deps.user) {
      // Clean up if not logged in
      if (userWsRef.current) {
        userWsRef.current.close();
        userWsRef.current = null;
      }
      if (userWsReconnectTimeoutRef.current) {
        clearTimeout(userWsReconnectTimeoutRef.current);
        userWsReconnectTimeoutRef.current = null;
      }
      if (userWsHeartbeatIntervalRef.current) {
        clearInterval(userWsHeartbeatIntervalRef.current);
        userWsHeartbeatIntervalRef.current = null;
      }
      return;
    }

    let reconnectAttempts = 0;
    const maxReconnectAttempts = 5;
    const baseReconnectDelay = 1000;

    const connect = () => {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsUrl = `${protocol}//${window.location.host}/api/ws/user/${encodeURIComponent(deps.user!.username)}`;

      const ws = new WebSocket(wsUrl);
      userWsRef.current = ws;

      ws.onopen = () => {
        reconnectAttempts = 0;

        // Start heartbeat
        userWsHeartbeatIntervalRef.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'ping' }));
          }
        }, 30000);
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          // Only handle user-level events (chat_created, chat_deleted)
          if (data.type === 'chat_created' || data.type === 'chat_deleted') {
            handleSyncEvent(data);
          }
        } catch (e) {
          // Ignore parse errors
        }
      };

      ws.onclose = (event) => {
        if (userWsHeartbeatIntervalRef.current) {
          clearInterval(userWsHeartbeatIntervalRef.current);
          userWsHeartbeatIntervalRef.current = null;
        }

        // Don't reconnect if intentionally closed
        if (event.code === 1000) {
          return;
        }

        // Attempt reconnect with exponential backoff
        if (reconnectAttempts < maxReconnectAttempts) {
          const delay = baseReconnectDelay * Math.pow(2, reconnectAttempts);
          reconnectAttempts++;
          userWsReconnectTimeoutRef.current = setTimeout(connect, delay);
        }
      };

      ws.onerror = () => {
        // Error will trigger onclose
      };
    };

    connect();

    return () => {
      if (userWsRef.current) {
        userWsRef.current.close(1000);
        userWsRef.current = null;
      }
      if (userWsReconnectTimeoutRef.current) {
        clearTimeout(userWsReconnectTimeoutRef.current);
        userWsReconnectTimeoutRef.current = null;
      }
      if (userWsHeartbeatIntervalRef.current) {
        clearInterval(userWsHeartbeatIntervalRef.current);
        userWsHeartbeatIntervalRef.current = null;
      }
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deps.user?.username]);

  return {
    viewerCount,
    needsSyncReload,
    setNeedsSyncReload,
    setViewerCount,
    syncWsRef,
    syncReconnectTimeoutRef,
    syncHeartbeatIntervalRef,
  };
}
