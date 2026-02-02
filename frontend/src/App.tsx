import React, { useState, useEffect, useRef, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';

// Convert LaTeX-style math delimiters to dollar-sign style for remark-math
const convertMathDelimiters = (text: string): string => {
  return text
    .replace(/\\\[/g, '$$')      // \[ to $$
    .replace(/\\\]/g, '$$')      // \] to $$
    .replace(/\\\(/g, '$')       // \( to $
    .replace(/\\\)/g, '$');      // \) to $
};

// Format timestamp to "Month Day, Year  HH:MM" in Eastern time
const formatTimestamp = (timestamp: string | undefined): string => {
  if (!timestamp) return '';
  try {
    const date = new Date(timestamp);
    const options: Intl.DateTimeFormatOptions = {
      timeZone: 'America/New_York',
      month: 'long',
      day: 'numeric',
      year: 'numeric',
    };
    const timeOptions: Intl.DateTimeFormatOptions = {
      timeZone: 'America/New_York',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false
    };
    const datePart = date.toLocaleDateString('en-US', options);
    const timePart = date.toLocaleTimeString('en-US', timeOptions);
    return `${datePart}  ${timePart}`;
  } catch {
    return '';
  }
};

interface LoginResponse {
  username: string;
  has_api_key: boolean;
  is_new_user: boolean;
}

interface ChatMessage {
  id?: string;  // Unique message ID (for branching)
  parent_id?: string | null;  // ID of parent message (for branching)
  role: string;
  content: string;
  timestamp?: string;
  tokens?: string;
  cost?: string;
  reasoning?: string;
  attached_files?: {filename: string, content: string}[];
  model?: string;  // Model used for this response (assistant messages only)
  service_tier?: 'flex' | 'standard' | null;  // GPT-5.2 service tier (flex or standard)
}

interface ChatStats {
  total_input_tokens: number;
  total_cached_tokens: number;
  total_output_tokens: number;
  total_cost: number;
  total_prompts: number;
  gpt_prompts?: number;
  sonnet_prompts?: number;
  avg_gpt_context_growth?: number;
  avg_sonnet_context_growth?: number;
  first_prompt_date?: string;
  last_accessed?: string;
}

interface UserStats {
  lifetime_prompts: number;
  lifetime_gpt_prompts: number;
  lifetime_sonnet_prompts: number;
  lifetime_input_tokens: number;
  lifetime_cached_tokens: number;
  lifetime_output_tokens: number;
  lifetime_reasoning_tokens: number;
  lifetime_cost: number;
  lifetime_cache_miss_percent: number;
  monthly_active_days: number;
  monthly_prompts: number;
  monthly_gpt_prompts: number;
  monthly_sonnet_prompts: number;
  monthly_input_tokens: number;
  monthly_cached_tokens: number;
  monthly_output_tokens: number;
  monthly_reasoning_tokens: number;
  monthly_total_tokens: number;
  monthly_cost: number;
  today_prompts: number;
  today_gpt_prompts: number;
  today_sonnet_prompts: number;
  today_input_tokens: number;
  today_cached_tokens: number;
  today_output_tokens: number;
  today_reasoning_tokens: number;
  today_total_tokens: number;
  today_cost: number;
  avg_prompts_per_day: number;
  avg_gpt_prompts_per_day: number;
  avg_sonnet_prompts_per_day: number;
  avg_input_per_day: number;
  avg_cached_per_day: number;
  avg_output_per_day: number;
  avg_reasoning_per_day: number;
  avg_total_per_day: number;
  avg_cost_per_day: number;
  avg_gpt_context_growth: number;
  avg_sonnet_context_growth: number;
  days_since_first: number;
}

interface ModelInfo {
  id: string;
  name: string;
  pricing: {
    input_new: number;
    input_cached: number;
    output: number;
    reasoning: number;
  };
  context_limits: {
    threshold: number;
    target: number;
  };
}

interface ApiKeysStatus {
  has_openai: boolean;
  has_anthropic: boolean;
}

interface FreeTokens {
  total_free: number;
  used: number;
  remaining: number;
  resets_at_eastern: string;
}

interface ProjectFileInfo {
  filename: string;
  tokens: number;
  size_bytes: number;
  staged: boolean;
}

interface ProjectFilesResponse {
  files: ProjectFileInfo[];
  total_tokens: number;
  staged_tokens: number;
}

interface ProjectInstructions {
  instructions: string;
  tokens: number;
}

interface ChatCardInfo {
  name: string;
  lastMessage: string;
  lastActive: string;
  messageCount: number;
  cost: number;
}

function App() {
  const [username, setUsername] = useState('');
  
  // Inject markdown styles once
  useEffect(() => {
    const styleId = 'markdown-styles';
    if (!document.getElementById(styleId)) {
      // Add KaTeX CSS from CDN
      const katexLink = document.createElement('link');
      katexLink.rel = 'stylesheet';
      katexLink.href = 'https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css';
      document.head.appendChild(katexLink);
      
      const style = document.createElement('style');
      style.id = styleId;
      style.textContent = `
        html, body {
          margin: 0;
          padding: 0;
          overflow: hidden;
          height: 100%;
          height: 100dvh;
        }
        #root {
          height: 100%;
          height: 100dvh;
        }
        .messageContent p {
          margin: 0.75em 0;
        }
        .messageContent p:first-child {
          margin-top: 0;
        }
        .messageContent p:last-child {
          margin-bottom: 0;
        }
        .messageContent strong {
          font-weight: bold;
          color: #fff;
        }
        .messageContent em {
          font-style: italic;
        }
        .messageContent code {
          background-color: #2a2a4e;
          padding: 2px 6px;
          border-radius: 3px;
          font-family: 'Consolas', 'Monaco', monospace;
          font-size: 0.9em;
        }
        .messageContent pre {
          background-color: #2a2a4e;
          padding: 12px;
          border-radius: 6px;
          overflow-x: auto;
          margin: 0.5em 0;
          white-space: pre-wrap;
        }
        .messageContent pre code {
          background-color: transparent;
          padding: 0;
        }
        .messageContent ul, .messageContent ol {
          margin: 0.5em 0;
          padding-left: 1.5em;
          list-style-position: inside;
        }
        .messageContent li {
          margin: 0.25em 0;
        }
        .messageContent li > p {
          display: inline;
          margin: 0;
        }
        .messageContent h1, .messageContent h2, .messageContent h3, 
        .messageContent h4, .messageContent h5, .messageContent h6 {
          margin: 0.8em 0 0.4em 0;
          font-weight: bold;
        }
        .messageContent h1 { font-size: 1.5em; }
        .messageContent h2 { font-size: 1.3em; }
        .messageContent h3 { font-size: 1.1em; }
        .messageContent a {
          color: #6b6bff;
          text-decoration: underline;
        }
        .messageContent blockquote {
          border-left: 3px solid #4a4ae8;
          padding-left: 12px;
          margin: 0.5em 0;
          color: #bbb;
        }
        .resizeHandle:hover {
          color: #aaa;
          background-color: rgba(255, 255, 255, 0.1);
          border-radius: 3px;
        }
        .editMessageButton:hover {
          background-color: rgba(255, 255, 255, 0.1);
        }
        .reasoningContainer:hover {
          background-color: #2a2a4e;
        }
        @media (max-width: 767px) {
          .message {
            padding: 12px !important;
          }
          .messageContent {
            font-size: 0.95rem !important;
          }
          .inputArea {
            padding: 8px !important;
            gap: 8px !important;
            position: fixed !important;
            bottom: 0 !important;
            left: 0 !important;
            right: 0 !important;
            background-color: #1a1a2e !important;
            z-index: 100 !important;
          }
        }
      `;
      document.head.appendChild(style);
    }
  }, []);
  
  const [user, setUser] = useState<LoginResponse | null>(null);
  const [apiKey, setApiKey] = useState('');
  const [anthropicKey, setAnthropicKey] = useState('');
  const [apiKeysStatus, setApiKeysStatus] = useState<ApiKeysStatus>({ has_openai: false, has_anthropic: false });
  const [needsApiKey, setNeedsApiKey] = useState(false);
  const [error, setError] = useState('');
  const [availableModels, setAvailableModels] = useState<ModelInfo[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>('gpt-5.2');
  const [showApiKeyModal, setShowApiKeyModal] = useState(false);
  const [pendingModelSwitch, setPendingModelSwitch] = useState<string | null>(null);
  const [modalApiKey, setModalApiKey] = useState('');
  const [savingApiKey, setSavingApiKey] = useState(false);

  // Ref to prevent saving to localStorage during restoration
  // Start as true to prevent clearing on initial mount
  const isRestoringRef = useRef(true);
  const restorationTimeoutRef = useRef<NodeJS.Timeout | null>(null); // Track restoration timeout for cleanup

  const [creatingChat, setCreatingChat] = useState(false);
  const [creatingProject, setCreatingProject] = useState(false);
  const [newItemName, setNewItemName] = useState('');

  const [chats, setChats] = useState<string[]>([]);
  const [currentProject, setCurrentProject] = useState<string | null>(null);
  const [currentChat, setCurrentChat] = useState<string | null>(null);
  const [editingChat, setEditingChat] = useState<string | null>(null);
  const [editingName, setEditingName] = useState('');
  const [editingProject, setEditingProject] = useState<string | null>(null);
  const [editingProjectName, setEditingProjectName] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);  // Current branch path (displayed messages)
  const [allMessages, setAllMessages] = useState<ChatMessage[]>([]);  // Full message tree (for branch navigation)
  const [currentLeafId, setCurrentLeafId] = useState<string | null>(null);  // Current branch leaf
  const [totalMessages, setTotalMessages] = useState(0); // Track total messages in full conversation
  const [contextStartIndex, setContextStartIndex] = useState(1); // Index of first message in context (1 = all in context)
  const [editingMessageIndex, setEditingMessageIndex] = useState<number | null>(null);
  const [editingMessageContent, setEditingMessageContent] = useState('');
  const [hasMoreMessages, setHasMoreMessages] = useState(false);
  const [isLoadingMoreMessages, setIsLoadingMoreMessages] = useState(false);
  const [messageOffset, setMessageOffset] = useState(0);
  const lastLoadTimeRef = useRef(0); // Prevent rapid-fire loads
  const currentProjectRef = useRef<string | null>(null); // Track current project for async operations
  const currentChatRef = useRef<string | null>(null); // Track current chat for async operations
  const [stats, setStats] = useState<ChatStats | null>(null);
  const [newMessage, setNewMessage] = useState('');
  const [textareaHeight, setTextareaHeight] = useState(85); // Initial height in pixels
  const [updatesText, setUpdatesText] = useState('');
  const [updatesTokenCount, setUpdatesTokenCount] = useState(0);
  const [showUpdatesModal, setShowUpdatesModal] = useState(false);
  const [updatesLoading, setUpdatesLoading] = useState(false);
  const [expandedReasoning, setExpandedReasoning] = useState<Set<number>>(new Set());
  const tokenCountTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const [isResizing, setIsResizing] = useState(false);
  const [resizeStartY, setResizeStartY] = useState(0);
  const [resizeStartHeight, setResizeStartHeight] = useState(0);
  const [isLoading, setIsLoading] = useState<Set<string>>(new Set());
  const [userStats, setUserStats] = useState<UserStats | null>(null);
  const [freeTokens, setFreeTokens] = useState<FreeTokens | null>(null);
  const [showStatsTooltip, setShowStatsTooltip] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [isMobile, setIsMobile] = useState(false);

  // Project landing page state
  const [projectFiles, setProjectFiles] = useState<ProjectFileInfo[]>([]);
  const [projectFilesTotalTokens, setProjectFilesTotalTokens] = useState(0);
  const [projectInstructions, setProjectInstructions] = useState('');
  const [projectInstructionsTokens, setProjectInstructionsTokens] = useState(0);
  const [showInstructionsModal, setShowInstructionsModal] = useState(false);
  const [editingInstructions, setEditingInstructions] = useState('');
  const [instructionsSaving, setInstructionsSaving] = useState(false);
  const [filesUploading, setFilesUploading] = useState(false);
  const [projectChatsDetailed, setProjectChatsDetailed] = useState<ChatCardInfo[]>([]);
  const [chatSearchQuery, setChatSearchQuery] = useState('');
  const [hoveredFilename, setHoveredFilename] = useState<string | null>(null);
  const [filenameTooltipPos, setFilenameTooltipPos] = useState<{x: number, y: number}>({x: 0, y: 0});
  const filenameTooltipTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Chat file attachment state
  const [stagedFiles, setStagedFiles] = useState<{filename: string, content: string}[]>([]);
  const [showAttachMenu, setShowAttachMenu] = useState(false);
  const [isDraggingFile, setIsDraggingFile] = useState(false);
  const chatFileInputRef = useRef<HTMLInputElement>(null);
  const attachMenuRef = useRef<HTMLDivElement>(null);

  // Close attach menu when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (showAttachMenu && attachMenuRef.current && !attachMenuRef.current.contains(event.target as Node)) {
        setShowAttachMenu(false);
      }
    };
    
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [showAttachMenu]);

  // Detect mobile screen size
  useEffect(() => {
    const checkMobile = () => {
      const mobile = window.innerWidth < 768;
      setIsMobile(mobile);
      if (mobile) {
        setSidebarOpen(false); // Collapse sidebar on mobile by default
      }
    };
    
    checkMobile();
    window.addEventListener('resize', checkMobile);
    return () => window.removeEventListener('resize', checkMobile);
  }, []);

  const [projectsExpanded, setProjectsExpanded] = useState(true);
  const [chatsExpanded, setChatsExpanded] = useState(true);
  const [projects, setProjects] = useState<string[]>([]);
  const [viewMode, setViewMode] = useState<'chat' | 'projectList' | 'chatList'>('chat');
  const [projectChatsCache, setProjectChatsCache] = useState<{[key: string]: string[]}>({});
  const [rootChatsCache, setRootChatsCache] = useState<string[] | null>(null);
  const [projectModel, setProjectModel] = useState<string | null>(null);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);

  // ============================================================================
  // CENTRALIZED STATE RESET FUNCTIONS
  // These prevent the "forgot to reset X in function Y" class of bugs
  // ============================================================================

  // Clear any pending timeouts
  const clearPendingTimeouts = () => {
    if (tokenCountTimeoutRef.current) {
      clearTimeout(tokenCountTimeoutRef.current);
      tokenCountTimeoutRef.current = null;
    }
  };

  // Reset UI state (creation dialogs, rename dialogs, errors)
  const resetUIState = () => {
    setError('');
    setCreatingChat(false);
    setCreatingProject(false);
    setNewItemName('');
    setEditingChat(null);
    setEditingName('');
    setEditingProject(null);
    setEditingProjectName('');
  };

  // Reset chat-specific state (called when switching/closing chats)
  const resetChatState = () => {
    clearPendingTimeouts();

    setCurrentChat(null);
    currentChatRef.current = null;
    setMessages([]);
    setAllMessages([]);
    setCurrentLeafId(null);
    setTotalMessages(0);
    setContextStartIndex(1);
    setHasMoreMessages(false);
    setIsLoadingMoreMessages(false);
    setMessageOffset(0);
    setStats(null);
    setNewMessage('');
    setUpdatesText('');
    setUpdatesTokenCount(0);
    setShowUpdatesModal(false);
    setUpdatesLoading(false);
    setExpandedReasoning(new Set());
    setEditingMessageIndex(null);
    setEditingMessageContent('');
    setStagedFiles([]);
    setShowAttachMenu(false);
    setIsDraggingFile(false);
    setIsResizing(false);
    
    // Reset load cooldown
    lastLoadTimeRef.current = 0;
  };

  // Reset project-specific state (called when switching/exiting projects)
  // Includes chat state since project contains chats
  const resetProjectState = () => {
    resetChatState();
    resetUIState();

    setCurrentProject(null);
    currentProjectRef.current = null;
    setProjectFiles([]);
    setProjectFilesTotalTokens(0);
    setProjectInstructions('');
    setProjectInstructionsTokens(0);
    setShowInstructionsModal(false);
    setEditingInstructions('');
    setInstructionsSaving(false);
    setFilesUploading(false);
    setProjectChatsDetailed([]);
    setChatSearchQuery('');
    setViewMode('chat');
    setProjectModel(null);
  };

  // Reset all user session state (called on logout)
  const resetAllState = () => {
    resetProjectState();
    
    // Clear restoration timeout if pending
    if (restorationTimeoutRef.current) {
      clearTimeout(restorationTimeoutRef.current);
      restorationTimeoutRef.current = null;
    }
    // Reset restoration flag for next login
    isRestoringRef.current = true;
    
    setUser(null);
    setNeedsApiKey(false);
    setApiKey('');
    setChats([]);
    setProjects([]);
    setProjectChatsCache({});
    setRootChatsCache(null);
    setUserStats(null);
    setFreeTokens(null);
    setIsLoading(new Set());
  };

  // ============================================================================
  // ASYNC CONTEXT GUARD
  // Prevents stale closure bugs by capturing context and providing staleness checks
  // Usage: const ctx = createContextGuard(); ... if (ctx.isStale()) return;
  // ============================================================================

  const createContextGuard = () => {
    const chat = currentChat;
    const project = currentProject;
    return {
      chat,
      project,
      isChatStale: () => currentChatRef.current !== chat,
      isProjectStale: () => currentProjectRef.current !== project,
      isStale: () => currentChatRef.current !== chat || currentProjectRef.current !== project,
    };
  };

  // ============================================================================
  // BRANCHING HELPER FUNCTIONS
  // Tree traversal utilities for branch navigation
  // ============================================================================

  // Build an index of messages by ID for O(1) lookup
  const buildMessageIndex = (msgs: ChatMessage[]): Map<string, ChatMessage> => {
    const index = new Map<string, ChatMessage>();
    for (const msg of msgs) {
      if (msg.id) index.set(msg.id, msg);
    }
    return index;
  };

  // Get siblings of a message (messages with the same parent)
  const getSiblings = (msgs: ChatMessage[], messageId: string): ChatMessage[] => {
    const index = buildMessageIndex(msgs);
    const target = index.get(messageId);
    if (!target) return [];

    const parentId = target.parent_id;
    const siblings = msgs.filter(m => m.parent_id === parentId);

    // Sort by timestamp for consistent ordering
    siblings.sort((a, b) => (a.timestamp || '').localeCompare(b.timestamp || ''));
    return siblings;
  };

  // Get the path from root to a specific message
  const getPathToMessage = (msgs: ChatMessage[], leafId: string): ChatMessage[] => {
    if (!msgs.length || !leafId) return [];

    const index = buildMessageIndex(msgs);
    const path: ChatMessage[] = [];
    let currentId: string | null | undefined = leafId;

    while (currentId) {
      const msg = index.get(currentId);
      if (!msg) break;
      path.unshift(msg);
      currentId = msg.parent_id;
    }

    return path;
  };

  // Switch to a different branch by selecting a sibling message
  const switchBranch = async (targetMessageId: string) => {
    if (!user || !currentChat) return;

    const ctx = createContextGuard();

    try {
      const projectParam = ctx.project ? `&project=${encodeURIComponent(ctx.project)}` : '';
      const response = await fetch(
        `/api/switch-branch/${encodeURIComponent(user.username)}/${encodeURIComponent(ctx.chat!)}?target_message_id=${encodeURIComponent(targetMessageId)}${projectParam}`,
        { method: 'POST' }
      );

      if (!response.ok) {
        const data = await response.json();
        setError(data.detail || 'Failed to switch branch');
        return;
      }

      const data = await response.json();

      if (ctx.isStale()) return;

      // Update current leaf and fetch the new branch
      setCurrentLeafId(data.new_leaf_id);

      // Fetch the updated chat to get the new branch path
      const chatUrl = ctx.project
        ? `/api/chat/${user.username}/${ctx.chat}?project=${ctx.project}&leaf_id=${data.new_leaf_id}&limit=30&offset=0`
        : `/api/chat/${user.username}/${ctx.chat}?leaf_id=${data.new_leaf_id}&limit=30&offset=0`;

      const chatResponse = await fetch(chatUrl);
      if (!chatResponse.ok) return;

      const chatData = await chatResponse.json();

      if (ctx.isStale()) return;

      const loadedMessages = chatData.messages.filter((m: ChatMessage) => m.role !== 'system');
      setMessages(loadedMessages);
      setAllMessages(chatData.all_messages || chatData.messages);  // Full tree for branch navigation
      setTotalMessages(chatData.total_messages);
      setHasMoreMessages(chatData.has_more_messages || false);
      setMessageOffset(loadedMessages.length);

    } catch (err) {
      console.error('Error switching branch:', err);
      setError('Could not switch branch');
    }
  };

  // ============================================================================
  // API HELPERS
  // Consolidate repeated fetch patterns
  // ============================================================================

  // Refresh project chat list and update cache. Returns chat list or null on failure.
  const refreshProjectChats = async (projectName: string, includeDetailed = false): Promise<string[] | null> => {
    if (!user) return null;
    try {
      const response = await fetch(`/api/project-chats/${encodeURIComponent(user.username)}/${encodeURIComponent(projectName)}`);
      if (!response.ok) return null;
      const data = await response.json();
      const chatList = data.chats || [];
      
      // Update cache (always safe - keyed by project name)
      setProjectChatsCache(prev => ({ ...prev, [projectName]: chatList }));
      
      // Only update current view state if still on this project
      if (currentProjectRef.current === projectName) {
        setChats(chatList);
        if (includeDetailed) fetchProjectChatsDetailed(projectName);
      }
      return chatList;
    } catch {
      return null;
    }
  };

  // ============================================================================

  // Helper function to scroll to bottom instantly
  const scrollToBottom = () => {
    const container = messagesContainerRef.current;
    if (container) {
      container.scrollTop = container.scrollHeight;
    }
  };

  useEffect(() => {
    if (user && user.has_api_key) {
      loadChatList();
      fetchUserStats();
      fetchFreeTokens();
      
      // Restore last viewed project and chat after a brief delay to ensure everything is loaded
      restorationTimeoutRef.current = setTimeout(async () => {
        // Check if user is still logged in (could have logged out during the timeout)
        if (!user) {
          isRestoringRef.current = false;
          return;
        }
        
        const savedProject = localStorage.getItem('chatgpt-current-project');
        const savedChat = localStorage.getItem('chatgpt-current-chat');
        
        try {
          if (savedProject) {
            await enterProject(savedProject);

            if (savedChat) {
              await openChat(savedChat, savedProject);
            }
          } else if (savedChat) {
            await openChat(savedChat, null);
          }
        } catch (err) {
          // Failed to restore previous session state - clear invalid saved values
          localStorage.removeItem('chatgpt-current-project');
          localStorage.removeItem('chatgpt-current-chat');
        } finally {
          // Re-enable saving after restoration completes (even if nothing to restore)
          isRestoringRef.current = false;
          restorationTimeoutRef.current = null;
        }
      }, 300); // Wait 300ms for initial data to load
    } else if (user && !user.has_api_key) {
      // If user doesn't have API key, still re-enable saving
      isRestoringRef.current = false;
    }
    
    // Cleanup: clear timeout if user changes or component unmounts
    return () => {
      if (restorationTimeoutRef.current) {
        clearTimeout(restorationTimeoutRef.current);
        restorationTimeoutRef.current = null;
      }
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  // Save current project and chat to localStorage whenever they change
  useEffect(() => {
    // Don't save during restoration to avoid clearing values
    if (user && !isRestoringRef.current) {
      if (currentProject) {
        localStorage.setItem('chatgpt-current-project', currentProject);
      } else {
        localStorage.removeItem('chatgpt-current-project');
      }

      if (currentChat) {
        localStorage.setItem('chatgpt-current-chat', currentChat);
      } else {
        localStorage.removeItem('chatgpt-current-chat');
      }
    }
  }, [currentProject, currentChat, user]);

  useEffect(() => {
    const savedUsername = localStorage.getItem('chatgpt-username');
    if (savedUsername) {
      fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: savedUsername })
      })
        .then(res => res.json())
        .then(data => {
          if (data.username) {
            setUser(data);
            if (!data.has_api_key) {
              setNeedsApiKey(true);
            }
            // Fetch API keys status
            fetch(`/api/api-keys/${data.username}`)
              .then(res => res.json())
              .then(keysData => setApiKeysStatus(keysData))
              .catch(() => {});
          }
        })
        .catch(() => {
          localStorage.removeItem('chatgpt-username');
          localStorage.removeItem('chatgpt-current-project');
          localStorage.removeItem('chatgpt-current-chat');
        });
    }

    // Fetch available models with fallback
    fetch('/api/models')
      .then(res => res.json())
      .then(data => setAvailableModels(data))
      .catch(() => {
        // Fallback models if API fails
        setAvailableModels([
          { id: 'gpt-5.2', name: 'GPT-5.2', pricing: { input_new: 1.75, input_cached: 0.175, output: 14, reasoning: 14 }, context_limits: { threshold: 275000, target: 225000 } },
          { id: 'claude-sonnet-4.5', name: 'Claude Sonnet 4.5', pricing: { input_new: 6, input_cached: 0.3, output: 15, reasoning: 15 }, context_limits: { threshold: 195000, target: 150000 } },
          { id: 'claude-opus-4.5', name: 'Claude Opus 4.5', pricing: { input_new: 5, input_cached: 0.5, output: 25, reasoning: 25 }, context_limits: { threshold: 80000, target: 55000 } }
        ]);
      });
  }, []);

  const loadChatList = async () => {
    if (!user) return;
    
    // If we have cached root chats, use them immediately
    if (rootChatsCache !== null) {
      setChats(rootChatsCache);
    }
    
    // Still fetch to get any updates
    try {
      const response = await fetch(`/api/chats/${user.username}`);
      if (!response.ok) {
        console.error('Failed to load chats: server returned', response.status);
        return;
      }
      const data = await response.json();
      
      // Handle both old format {chats, projects} and new format {chats, total, has_more, projects}
      const chatList = data.chats || [];
      
      setChats(chatList);
      setProjects(data.projects || []);
      
      // Update cache
      setRootChatsCache(chatList);
      
      // Preload chats for visible projects (first 5-6)
      const visibleProjects = (data.projects || []).slice(0, 6);
      preloadProjectChats(visibleProjects);
    } catch (err) {
      console.error('Failed to load chats:', err);
    }
  };

  const preloadProjectChats = async (projectNames: string[]) => {
    if (!user) return;
    
    // Fetch chats for each visible project in parallel
    const fetchPromises = projectNames.map(async (projectName) => {
      // Skip if already cached
      if (projectChatsCache[projectName]) return;
      
      try {
        const response = await fetch(`/api/project-chats/${user.username}/${projectName}`);
        if (!response.ok) return; // Silent fail for preload
        const data = await response.json();
        
        // Update cache
        setProjectChatsCache(prev => ({
          ...prev,
          [projectName]: data.chats || []
        }));
      } catch {
        // Silent fail - preloading is not critical
      }
    });
    
    // Fire all requests in parallel (don't wait)
    Promise.all(fetchPromises);
  };

  const enterProject = async (projectName: string) => {
    if (!user) return;

    // Check if we're re-entering the same project (e.g., clicking project name while in a chat)
    const isSameProject = currentProject === projectName;

    // Reset previous state
    resetChatState();
    resetUIState();

    // Clear project-specific state only when entering a different project
    // When re-entering the same project, preserve existing data to avoid flash of empty content
    if (!isSameProject) {
      setProjectFiles([]);
      setProjectFilesTotalTokens(0);
      setProjectInstructions('');
      setProjectInstructionsTokens(0);
      setShowInstructionsModal(false);
      setEditingInstructions('');
      setInstructionsSaving(false);
      setFilesUploading(false);
      setProjectChatsDetailed([]);
      setProjectModel(null);
    }
    setChatSearchQuery('');
    setViewMode('chat');

    // Set new project
    setCurrentProject(projectName);
    currentProjectRef.current = projectName;

    // If we have cached chats for this project, use them immediately
    if (projectChatsCache[projectName]) {
      setChats(projectChatsCache[projectName]);
    }

    // Fetch project metadata to get the project's default model
    try {
      const metadataResponse = await fetch(`/api/project-metadata/${user.username}/${projectName}`);
      if (currentProjectRef.current !== projectName) return;

      if (metadataResponse.ok) {
        const metadataData = await metadataResponse.json();
        if (currentProjectRef.current !== projectName) return;
        setProjectModel(metadataData.model || 'gpt-5.2');
      }
    } catch (err) {
      console.error('Could not fetch project metadata:', err);
    }

    // Fetch project files and instructions (refresh in background)
    fetchProjectFiles(projectName);
    fetchProjectInstructions(projectName);
    fetchProjectChatsDetailed(projectName);

    // Fetch chat list (uses refreshProjectChats which has built-in stale check)
    const chatList = await refreshProjectChats(projectName);
    if (!chatList && currentProjectRef.current === projectName) {
      setError('Could not load project chats');
    }
  };

  const exitProject = () => {
    resetProjectState();
    loadChatList();
  };

  const startCreateProject = () => {
    setCreatingProject(true);
    setNewItemName('');
  };

  const startCreateChat = () => {
    setCreatingChat(true);
    setNewItemName('');
  };

  const cancelCreate = () => {
    setCreatingProject(false);
    setCreatingChat(false);
    setNewItemName('');
  };

  const saveNewProject = async () => {
    if (!newItemName.trim() || !user) return;

    try {
      const response = await fetch('/api/create-project', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: user.username,
          project_name: newItemName.trim()
        })
      });

      if (response.ok) {
        cancelCreate();
        loadChatList();
      } else {
        const data = await response.json();
        setError(data.detail || 'Failed to create project');
      }
    } catch (err) {
      setError('Could not create project');
    }
  };

  const saveNewChat = async () => {
    if (!newItemName.trim() || !user) return;

    // Capture project at start to avoid stale closure
    const projectForNewChat = currentProject;
    const chatName = newItemName.trim();

    // When in a project, use project's model; otherwise use selectedModel
    // Note: backend will also inherit from project if model is not passed,
    // but passing it explicitly ensures UI consistency
    const modelForNewChat = projectForNewChat ? (projectModel || selectedModel) : selectedModel;

    try {
      const response = await fetch('/api/create-chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: user.username,
          chat_name: chatName,
          project: projectForNewChat,
          model: modelForNewChat
        })
      });

      if (response.ok) {
        cancelCreate();
        if (projectForNewChat) {
          // Refresh the project where the chat was created
          await refreshProjectChats(projectForNewChat);
        } else {
          loadChatList();
        }
        openChat(chatName, projectForNewChat);
      } else {
        const data = await response.json();
        setError(data.detail || 'Failed to create chat');
      }
    } catch (err) {
      setError('Could not create chat');
    }
  };

  const handleLogin = async () => {
    if (!username.trim()) return;
    
    try {
      const response = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username.trim() })
      });
      
      if (!response.ok) {
        const data = await response.json();
        setError(data.detail || 'Login failed');
        return;
      }
      
      const data: LoginResponse = await response.json();
      setUser(data);
      localStorage.setItem('chatgpt-username', data.username);
      setError('');
      
      if (!data.has_api_key) {
        setNeedsApiKey(true);
      }
    } catch (err) {
      setError('Could not connect to server');
    }
  };

  const handleLogout = () => {
    localStorage.removeItem('chatgpt-username');
    localStorage.removeItem('chatgpt-current-project');
    localStorage.removeItem('chatgpt-current-chat');
    resetAllState();
  };

  const handleSaveApiKey = async () => {
    if (!user) return;
    // Allow saving if at least one key is provided
    if (!apiKey.trim() && !anthropicKey.trim()) return;

    try {
      const response = await fetch('/api/set-api-keys', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: user.username,
          openai_key: apiKey.trim() || null,
          anthropic_key: anthropicKey.trim() || null
        })
      });

      if (response.ok) {
        const data = await response.json();
        setApiKeysStatus(data);
        // Consider API key set if at least OpenAI key is configured
        if (data.has_openai) {
          setNeedsApiKey(false);
          setUser({ ...user, has_api_key: true });
        }
        // Clear the input fields after saving
        setApiKey('');
        setAnthropicKey('');
      }
    } catch (err) {
      setError('Could not save API keys');
    }
  };

  const handleModelChange = async (newModel: string) => {
    if (!user || !currentChat) return;

    // Check if user has the required API key for this model
    const requiredKey = newModel.startsWith('claude') ? 'anthropic' : 'openai';
    const hasKey = requiredKey === 'anthropic' ? apiKeysStatus.has_anthropic : apiKeysStatus.has_openai;

    if (!hasKey) {
      // Show modal to enter the missing API key
      setPendingModelSwitch(newModel);
      setModalApiKey('');
      setShowApiKeyModal(true);
      return;
    }

    await completeModelSwitch(newModel);
  };

  const completeModelSwitch = async (newModel: string) => {
    if (!user || !currentChat) return;

    const previousModel = selectedModel;
    setSelectedModel(newModel);  // Optimistic update for responsive UI

    try {
      const response = await fetch('/api/set-chat-model', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: user.username,
          chat_name: currentChat,
          project: currentProject,
          model: newModel
        })
      });

      if (!response.ok) {
        // Rollback on server error
        setSelectedModel(previousModel);
        const data = await response.json().catch(() => ({}));

        // If error is about missing API key, show the modal instead of error
        if (data.detail && data.detail.includes('API key')) {
          setPendingModelSwitch(newModel);
          setModalApiKey('');
          setShowApiKeyModal(true);
          // Refresh API keys status since it may be stale
          fetch(`/api/api-keys/${user.username}`)
            .then(res => res.json())
            .then(keysData => setApiKeysStatus(keysData))
            .catch(() => {});
        } else {
          setError(data.detail || 'Could not switch model');
        }
      } else {
        // Success - update context window gray out effect
        const data = await response.json().catch(() => ({}));
        if (data.context_start_index !== undefined) {
          setContextStartIndex(data.context_start_index);
        }
      }
    } catch (err) {
      // Rollback on network error
      setSelectedModel(previousModel);
      setError('Could not switch model - network error');
      console.error('Could not save model preference:', err);
    }
  };

  const handleApiKeyModalSave = async () => {
    if (!user || !pendingModelSwitch || !modalApiKey.trim()) return;

    setSavingApiKey(true);
    const isAnthropic = pendingModelSwitch.startsWith('claude');

    try {
      const response = await fetch('/api/set-api-keys', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: user.username,
          openai_key: isAnthropic ? null : modalApiKey.trim(),
          anthropic_key: isAnthropic ? modalApiKey.trim() : null
        })
      });

      if (response.ok) {
        const data = await response.json();
        setApiKeysStatus(data);
        setShowApiKeyModal(false);
        setModalApiKey('');
        // Now complete the model switch
        await completeModelSwitch(pendingModelSwitch);
        setPendingModelSwitch(null);
      } else {
        setError('Could not save API key');
      }
    } catch (err) {
      setError('Could not save API key');
    } finally {
      setSavingApiKey(false);
    }
  };

  const handleApiKeyModalCancel = () => {
    setShowApiKeyModal(false);
    setModalApiKey('');
    setPendingModelSwitch(null);
  };

  const handleProjectModelChange = async (newModel: string) => {
    if (!user || !currentProject) return;

    // Check if user has the required API key for this model
    const requiredKey = newModel.startsWith('claude') ? 'anthropic' : 'openai';
    const hasKey = requiredKey === 'anthropic' ? apiKeysStatus.has_anthropic : apiKeysStatus.has_openai;

    if (!hasKey) {
      // Show modal to enter the missing API key
      setPendingModelSwitch(newModel);
      setModalApiKey('');
      setShowApiKeyModal(true);
      return;
    }

    await completeProjectModelSwitch(newModel);
  };

  const completeProjectModelSwitch = async (newModel: string) => {
    if (!user || !currentProject) return;

    const previousModel = projectModel;
    setProjectModel(newModel);  // Optimistic update for responsive UI

    try {
      const response = await fetch('/api/set-project-model', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: user.username,
          project: currentProject,
          model: newModel
        })
      });

      if (!response.ok) {
        // Rollback on server error
        setProjectModel(previousModel);
        const data = await response.json().catch(() => ({}));

        // If error is about missing API key, show the modal instead of error
        if (data.detail && data.detail.includes('API key')) {
          setPendingModelSwitch(newModel);
          setModalApiKey('');
          setShowApiKeyModal(true);
          // Refresh API keys status since it may be stale
          fetch(`/api/api-keys/${user.username}`)
            .then(res => res.json())
            .then(keysData => setApiKeysStatus(keysData))
            .catch(() => {});
        } else {
          setError(data.detail || 'Could not switch project model');
        }
      } else {
        // Success - re-fetch project files and instructions with new tokenizer
        fetchProjectFiles(currentProject);
        fetchProjectInstructions(currentProject);
      }
    } catch (err) {
      // Rollback on network error
      setProjectModel(previousModel);
      setError('Could not switch project model - network error');
      console.error('Could not save project model preference:', err);
    }
  };

  const fetchUserStats = async () => {
    if (!user) return;

    try {
      const response = await fetch(`/api/user-stats/${user.username}`);
      if (response.ok) {
        const data: UserStats = await response.json();
        setUserStats(data);
      }
    } catch (err) {
      console.error('Could not fetch user stats:', err);
    }
  };

  const fetchFreeTokens = async () => {
    if (!user) return;

    try {
      const response = await fetch(`/api/free-tokens/${user.username}`);
      if (response.ok) {
        const data: FreeTokens = await response.json();
        setFreeTokens(data);
      }
    } catch (err) {
      console.error('Could not fetch free tokens:', err);
    }
  };

  const fetchProjectFiles = async (projectName: string) => {
    if (!user) return;

    try {
      const response = await fetch(`/api/project-files/${user.username}/${projectName}`);
      // Check if we're still on the same project after await
      if (currentProjectRef.current !== projectName) return;
      
      if (response.ok) {
        const data: ProjectFilesResponse = await response.json();
        // Double-check after second await
        if (currentProjectRef.current !== projectName) return;
        setProjectFiles(data.files || []);
        setProjectFilesTotalTokens(data.staged_tokens || 0);
      }
    } catch (err) {
      console.error('Could not fetch project files:', err);
    }
  };

  const fetchProjectInstructions = async (projectName: string) => {
    if (!user) return;

    try {
      const response = await fetch(`/api/project-instructions/${user.username}/${projectName}`);
      // Check if we're still on the same project after await
      if (currentProjectRef.current !== projectName) return;
      
      if (response.ok) {
        const data: ProjectInstructions = await response.json();
        // Double-check after second await
        if (currentProjectRef.current !== projectName) return;
        setProjectInstructions(data.instructions || '');
        setProjectInstructionsTokens(data.tokens || 0);
      }
    } catch (err) {
      console.error('Could not fetch project instructions:', err);
    }
  };

  const updateProjectInstructions = async () => {
    const ctx = createContextGuard();
    if (!user || !ctx.project) return;

    const instructionsToSave = editingInstructions;

    setInstructionsSaving(true);
    try {
      const response = await fetch(`/api/project-instructions/${user.username}/${ctx.project}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instructions: instructionsToSave })
      });

      if (ctx.isProjectStale()) return;

      if (response.ok) {
        const data = await response.json();
        setProjectInstructions(instructionsToSave);
        setProjectInstructionsTokens(data.tokens);
        setShowInstructionsModal(false);
      } else {
        const data = await response.json();
        setError(data.detail || 'Failed to save instructions');
      }
    } catch (err) {
      setError('Could not save instructions');
    } finally {
      setInstructionsSaving(false);
    }
  };

  const handleFileUpload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    if (!user || !currentProject || !event.target.files) return;

    const ctx = createContextGuard();
    const files = Array.from(event.target.files);
    if (files.length === 0) return;

    setFilesUploading(true);
    try {
      const formData = new FormData();
      files.forEach(file => {
        formData.append('files', file);
      });

      const response = await fetch(`/api/project-files/${user.username}/${ctx.project}`, {
        method: 'POST',
        body: formData
      });

      if (ctx.isProjectStale()) return;

      if (response.ok) {
        const data = await response.json();
        // Build status message
        const messages: string[] = [];
        if (data.total_overwritten > 0) {
          const overwrittenNames = data.uploaded
            .filter((f: any) => f.overwritten)
            .map((f: any) => f.filename)
            .join(', ');
          messages.push(`Overwrote existing file${data.total_overwritten > 1 ? 's' : ''}: ${overwrittenNames}`);
        }
        if (data.errors && data.errors.length > 0) {
          messages.push(`Some files failed: ${data.errors.join(', ')}`);
        }
        if (messages.length > 0) {
          setError(messages.join('. '));
        }
        await fetchProjectFiles(ctx.project!);
      } else {
        const data = await response.json();
        setError(data.detail || 'Failed to upload files');
      }
    } catch (err) {
      setError('Could not upload files');
    } finally {
      setFilesUploading(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
    }
  };

  const handleDeleteProjectFile = async (filename: string) => {
    if (!user || !currentProject) return;
    if (!window.confirm(`Delete "${filename}"?`)) return;

    const ctx = createContextGuard();

    try {
      const response = await fetch(`/api/project-files/${user.username}/${ctx.project}/${filename}`, {
        method: 'DELETE'
      });

      if (ctx.isProjectStale()) return;

      if (response.ok) {
        await fetchProjectFiles(ctx.project!);
      } else {
        const data = await response.json();
        setError(data.detail || 'Failed to delete file');
      }
    } catch (err) {
      setError('Could not delete file');
    }
  };

  const handleToggleFileStaged = async (filename: string, currentStaged: boolean) => {
    const ctx = createContextGuard();
    if (!user || !ctx.project) return;

    const newStaged = !currentStaged;

    // Find the file to get its token count
    const file = projectFiles.find(f => f.filename === filename);
    if (!file) return;

    // Optimistically update local state
    setProjectFiles(prev => prev.map(f =>
      f.filename === filename ? { ...f, staged: newStaged } : f
    ));

    // Optimistically update token count
    setProjectFilesTotalTokens(prev =>
      newStaged ? prev + file.tokens : prev - file.tokens
    );

    try {
      const response = await fetch(`/api/project-files/${user.username}/${ctx.project}/staged/${encodeURIComponent(filename)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ staged: newStaged })
      });

      if (ctx.isProjectStale()) return;

      if (!response.ok) {
        // Revert on failure
        setProjectFiles(prev => prev.map(f =>
          f.filename === filename ? { ...f, staged: currentStaged } : f
        ));
        setProjectFilesTotalTokens(prev =>
          newStaged ? prev - file.tokens : prev + file.tokens
        );
        const data = await response.json();
        setError(data.detail || 'Failed to update file staged status');
      }
    } catch (err) {
      // Revert on error
      setProjectFiles(prev => prev.map(f =>
        f.filename === filename ? { ...f, staged: currentStaged } : f
      ));
      setProjectFilesTotalTokens(prev =>
        newStaged ? prev - file.tokens : prev + file.tokens
      );
      setError('Could not update file staged status');
    }
  };

  const fetchProjectChatsDetailed = async (projectName: string) => {
    if (!user) return;

    try {
      // Use the detailed endpoint to get all chat summaries in one request
      const response = await fetch(`/api/project-chats-detailed/${user.username}/${projectName}`);
      if (currentProjectRef.current !== projectName) return;

      if (response.ok) {
        const data = await response.json();
        if (currentProjectRef.current !== projectName) return;

        // Map backend response to frontend ChatCardInfo format
        const detailedChats: ChatCardInfo[] = data.chats.map((chat: any) => ({
          name: chat.name,
          lastMessage: chat.last_message || '',
          lastActive: chat.last_active || '',
          messageCount: chat.message_count || 0,
          cost: chat.cost || 0
        }));

        setProjectChatsDetailed(detailedChats);
      }
    } catch (err) {
      console.error('Could not fetch project chats:', err);
    }
  };

  const openChat = async (chatName: string, explicitProject?: string | null) => {
    if (!user) return;
    
    // Determine project to use before any state changes
    const projectToUse = explicitProject !== undefined ? explicitProject : currentProject;
    
    // Update ref IMMEDIATELY to signal "we're loading this chat now"
    // This must happen BEFORE any await so racing calls can detect staleness
    currentChatRef.current = chatName;
    
    // Clear timeouts and UI state before fetch
    clearPendingTimeouts();
    resetUIState();
    setIsLoadingMoreMessages(false);
    setIsDraggingFile(false);
    setUpdatesLoading(false);
    setIsResizing(false);
    lastLoadTimeRef.current = 0;

    try {
      const url = projectToUse 
        ? `/api/chat/${user.username}/${chatName}?project=${projectToUse}&limit=30&offset=0`
        : `/api/chat/${user.username}/${chatName}?limit=30&offset=0`;
      const response = await fetch(url);
      
      // Check if another openChat was called while we were fetching
      if (currentChatRef.current !== chatName) return;
      
      if (!response.ok) {
        const data = await response.json();
        setError(data.detail || 'Could not open chat');
        return;
      }
      
      const data = await response.json();
      
      // Check again after parsing
      if (currentChatRef.current !== chatName) return;
      
      // Set new chat state (ref already set above)
      setCurrentChat(chatName);
      setStagedFiles([]);
      setShowAttachMenu(false);
      setEditingMessageIndex(null);
      setEditingMessageContent('');
      setNewMessage('');
      setShowUpdatesModal(false);
      
      // Close sidebar on mobile when chat is opened
      if (isMobile) {
        setSidebarOpen(false);
      }
      
      // Defensive check for malformed API response
      if (!data.messages || !Array.isArray(data.messages)) {
        console.error('Invalid API response: missing messages array', data);
        setError('Server returned invalid data. Please try again.');
        return;
      }
      
      // Debug: log what we got from API
      console.log('Chat data from API:', {
        messagesCount: data.messages?.length,
        allMessagesCount: data.all_messages?.length,
        firstMsgHasId: data.messages?.[0]?.id ? 'yes' : 'no',
        firstMsgHasTimestamp: data.messages?.[0]?.timestamp ? 'yes' : 'no',
        current_leaf_id: data.current_leaf_id
      });

      const loadedMessages = data.messages.filter((m: ChatMessage) => m.role !== 'system');
      setMessages(loadedMessages);
      setAllMessages(data.all_messages || data.messages);  // Full tree for branch navigation
      setCurrentLeafId(data.current_leaf_id || null);  // Track current branch
      setStats(data.stats);
      setContextStartIndex(1);
      setExpandedReasoning(new Set());

      // Set model selection from chat data
      // Priority: chat's model > project's model > 'gpt-5.2'
      setSelectedModel(data.model || projectModel || 'gpt-5.2');

      // Validate total_messages from backend
      if (!data.total_messages || data.total_messages < 1) {
        console.error('Backend returned invalid total_messages:', data.total_messages);
        setError('Server error: invalid message count. Please refresh.');
        return;
      }

      setTotalMessages(data.total_messages);
      setHasMoreMessages(data.has_more_messages || false);
      // Use backend's message count for offset (includes system message)
      // This keeps offset in sync with backend pagination expectations
      setMessageOffset(data.messages.length);
      
      // Fetch updates for this chat
      try {
        const updatesUrl = projectToUse 
          ? `/api/updates/${user.username}/${chatName}?project=${projectToUse}`
          : `/api/updates/${user.username}/${chatName}`;
        const updatesResponse = await fetch(updatesUrl);
        if (currentChatRef.current !== chatName) return;
        
        if (updatesResponse.ok) {
          const updatesData = await updatesResponse.json();
          if (currentChatRef.current !== chatName) return;
          
          const loadedUpdates = updatesData.updates || '';
          setUpdatesText(loadedUpdates);
          
          if (loadedUpdates.trim()) {
            try {
              const tokenResponse = await fetch('/api/count-tokens', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text: loadedUpdates })
              });
              if (currentChatRef.current !== chatName) return;
              
              if (tokenResponse.ok) {
                const tokenData = await tokenResponse.json();
                if (currentChatRef.current !== chatName) return;
                setUpdatesTokenCount(tokenData.tokens);
              }
            } catch {
              // Silently fail
            }
          } else {
            setUpdatesTokenCount(0);
          }
        }
      } catch (err) {
        console.error('Could not fetch updates:', err);
        if (currentChatRef.current === chatName) {
          setUpdatesText('');
          setUpdatesTokenCount(0);
        }
      }
      
      // Scroll to bottom
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          scrollToBottom();
        });
      });
    } catch (err) {
      console.error('Error opening chat:', err);
      setError('Could not open chat');
    }
  };

  const saveUpdates = async () => {
    const ctx = createContextGuard();
    if (!user || !ctx.chat) return;
    
    setUpdatesLoading(true);
    try {
      const response = await fetch('/api/save-updates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: user.username,
          chat_name: ctx.chat,
          updates: updatesText,
          project: ctx.project
        })
      });
      
      if (ctx.isChatStale()) return;
      
      if (response.ok) {
        setShowUpdatesModal(false);
      } else {
        const data = await response.json();
        setError(data.detail || 'Failed to save updates');
      }
    } catch (err) {
      setError('Could not save updates');
    } finally {
      setUpdatesLoading(false);
    }
  };

  const updateUpdatesText = (text: string) => {
    setUpdatesText(text);
    
    const ctx = createContextGuard();
    
    if (tokenCountTimeoutRef.current) {
      clearTimeout(tokenCountTimeoutRef.current);
    }
    
    if (!text.trim()) {
      setUpdatesTokenCount(0);
      return;
    }
    
    tokenCountTimeoutRef.current = setTimeout(async () => {
      if (ctx.isChatStale()) return;
      
      try {
        const response = await fetch('/api/count-tokens', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text })
        });
        
        if (ctx.isChatStale()) return;
        
        if (response.ok) {
          const data = await response.json();
          setUpdatesTokenCount(data.tokens);
        }
      } catch (err) {
        // Silently fail - token count is not critical
      }
    }, 300);
  };

  const loadMoreMessages = useCallback(async () => {
    if (!user || !currentChat || isLoadingMoreMessages || !hasMoreMessages) return;
    
    const ctx = createContextGuard();
    
    // Prevent rapid-fire calls (minimum 500ms between loads)
    const now = Date.now();
    if (now - lastLoadTimeRef.current < 500) {
      return;
    }
    lastLoadTimeRef.current = now;
    
    setIsLoadingMoreMessages(true);
    
    try {
      const url = ctx.project 
        ? `/api/chat/${user.username}/${ctx.chat}?project=${ctx.project}&limit=30&offset=${messageOffset}`
        : `/api/chat/${user.username}/${ctx.chat}?limit=30&offset=${messageOffset}`;
      
      const response = await fetch(url);
      if (ctx.isChatStale()) return;
      
      if (!response.ok) {
        console.error('Failed to load more messages: server returned', response.status);
        setError('Could not load older messages');
        return;
      }
      const data = await response.json();
      
      if (ctx.isChatStale()) return;
      
      // Defensive check for malformed API response
      if (!data.messages || !Array.isArray(data.messages)) {
        console.error('Invalid API response: missing messages array', data);
        setError('Server returned invalid data');
        return;
      }
      
      const olderMessages = data.messages.filter((m: ChatMessage) => m.role !== 'system');

      const container = messagesContainerRef.current;
      const oldScrollHeight = container?.scrollHeight || 0;
      const oldScrollTop = container?.scrollTop || 0;
      
      setMessages(prev => [...olderMessages, ...prev]);
      setHasMoreMessages(data.has_more_messages || false);
      // Use backend's message count for offset (may include system message on oldest page)
      // This keeps offset in sync with backend pagination expectations
      setMessageOffset(prev => prev + data.messages.length);

      requestAnimationFrame(() => {
        if (container) {
          const newScrollHeight = container.scrollHeight;
          const heightDifference = newScrollHeight - oldScrollHeight;
          container.scrollTop = oldScrollTop + heightDifference;
        }
      });
    } catch (err) {
      console.error('Could not load more messages:', err);
      setError('Could not load older messages');
    } finally {
      setIsLoadingMoreMessages(false);
    }
  }, [user, currentChat, isLoadingMoreMessages, hasMoreMessages, currentProject, messageOffset, totalMessages]);

  const handleReloadChat = async () => {
    const ctx = createContextGuard();
    if (!user || !ctx.chat) return;

    try {
      const payload: any = {
        username: user.username,
        chat_name: ctx.chat
      };
      if (ctx.project) {
        payload.project = ctx.project;
      }

      const response = await fetch('/api/reload-chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (ctx.isChatStale()) return;

      if (response.ok) {
        const data = await response.json();
        await openChat(ctx.chat, ctx.project);
        // Update context graying based on new system prompt size
        if (data.context_start_index !== undefined) {
          setContextStartIndex(data.context_start_index);
        }
        setError('Instructions and files reloaded ✓');
        setTimeout(() => setError(''), 2000);
      } else {
        const data = await response.json();
        setError(data.detail || 'Failed to reload');
      }
    } catch (err) {
      setError('Could not reload chat');
    }
  };

  // Textarea resize handlers
  const handleResizeStart = (e: React.MouseEvent) => {
    e.preventDefault();
    setIsResizing(true);
    setResizeStartY(e.clientY);
    setResizeStartHeight(textareaHeight);
  };

  const handleResizeMove = (e: MouseEvent) => {
    if (!isResizing) return;
    
    // Calculate new height (inverted: dragging up increases height)
    const deltaY = resizeStartY - e.clientY;
    const newHeight = Math.min(400, Math.max(44, resizeStartHeight + deltaY));
    setTextareaHeight(newHeight);
  };

  const handleResizeEnd = () => {
    setIsResizing(false);
  };

  // Add/remove mouse event listeners for resize
  useEffect(() => {
    if (isResizing) {
      document.addEventListener('mousemove', handleResizeMove);
      document.addEventListener('mouseup', handleResizeEnd);
      return () => {
        document.removeEventListener('mousemove', handleResizeMove);
        document.removeEventListener('mouseup', handleResizeEnd);
      };
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isResizing, resizeStartY, resizeStartHeight]);

  // Scroll handler for lazy loading more messages
  useEffect(() => {
    const container = messagesContainerRef.current;
    if (!container || !currentChat) return;

    const handleScroll = () => {
      // Check if scrolled near the top (within 100px)
      if (container.scrollTop < 100 && hasMoreMessages && !isLoadingMoreMessages) {
        loadMoreMessages();
      }
    };

    container.addEventListener('scroll', handleScroll);
    return () => container.removeEventListener('scroll', handleScroll);
  }, [currentChat, hasMoreMessages, isLoadingMoreMessages, loadMoreMessages]);

  const startEditMessage = (index: number) => {
    if (index < 0 || index >= messages.length) return;
    setEditingMessageIndex(index);
    setEditingMessageContent(messages[index].content);
  };

  const cancelEditMessage = () => {
    setEditingMessageIndex(null);
    setEditingMessageContent('');
  };

  const saveEditedMessage = async () => {
    if (!user || !currentChat || editingMessageIndex === null || !editingMessageContent.trim()) return;

    // Bounds check - messages array might have changed since edit started
    if (editingMessageIndex < 0 || editingMessageIndex >= messages.length) {
      setError('Message index out of bounds. Please cancel and try again.');
      cancelEditMessage();
      return;
    }

    const ctx = createContextGuard();

    // Save original state for rollback
    const originalMessages = [...messages];
    const originalAllMessages = [...allMessages];
    const originalLeafId = currentLeafId;

    setIsLoading(prev => new Set(prev).add(ctx.chat!));

    try {
      // Get original message to preserve attached files and find parent
      const originalMessage = messages[editingMessageIndex];

      // For branching: the new message becomes a sibling of the original
      // So its parent is the same as the original message's parent
      const parentId = originalMessage.parent_id || null;

      // Optimistically show truncated messages + placeholder for new edit
      const truncatedMessages = messages.slice(0, editingMessageIndex);
      const editedMessage: ChatMessage = {
        role: 'user',
        content: editingMessageContent.trim(),
        attached_files: originalMessage.attached_files
      };
      const newMessages = [...truncatedMessages, editedMessage];
      setMessages(newMessages);

      // Clear editing state
      setEditingMessageIndex(null);
      setEditingMessageContent('');

      const response = await fetch('/api/send-message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: user.username,
          chat_name: ctx.chat,
          message: editedMessage.content,
          project: ctx.project,
          parent_id: parentId,  // Use parent_id for branching instead of truncate_to_index
          attached_files: originalMessage.attached_files || undefined,
          model: selectedModel
        })
      });

      if (response.ok) {
        const data = await response.json();

        // Defensive check for malformed response
        if (!data.assistant_message) {
          console.error('Invalid API response: missing assistant_message', data);
          setError('Server returned invalid response');
          if (!ctx.isStale()) {
            setMessages(originalMessages);
            setAllMessages(originalAllMessages);
            setCurrentLeafId(originalLeafId);
          }
          return;
        }

        // Build complete new user and assistant messages with IDs from response
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
          model: data.model
        };

        if (!ctx.isStale()) {
          // Update displayed messages (the new branch path)
          const finalMessages = [...truncatedMessages, newUserMessage, assistantMessage];
          setMessages(finalMessages);

          // Add the new messages to the full tree
          setAllMessages(prev => [...prev, newUserMessage, assistantMessage]);

          // Update current leaf to the new assistant message
          setCurrentLeafId(data.current_leaf_id || data.assistant_message_id);

          setStats(data.stats);
          setContextStartIndex(data.context_start_index || 1);

          // Use backend's total_messages for accurate pagination after edit
          // Backend returns total messages in the new branch (including system message)
          const branchTotalMessages = data.total_messages || (finalMessages.length + 1);
          setTotalMessages(branchTotalMessages);
          // Check if branch has more messages than we're displaying (+1 accounts for system message)
          setHasMoreMessages(branchTotalMessages > finalMessages.length + 1);
          // Offset = count of messages loaded from end (for next pagination request)
          setMessageOffset(finalMessages.length);

          fetchUserStats();
          fetchFreeTokens();
          requestAnimationFrame(() => scrollToBottom());
        }
      } else {
        const data = await response.json();
        setError(data.detail || 'Failed to regenerate response');
        if (!ctx.isStale()) {
          setMessages(originalMessages);
          setAllMessages(originalAllMessages);
          setCurrentLeafId(originalLeafId);
        }
      }
    } catch (err) {
      console.error('Error saving edited message:', err);
      setError('Could not save edited message');
      if (!ctx.isStale()) {
        setMessages(originalMessages);
        setAllMessages(originalAllMessages);
        setCurrentLeafId(originalLeafId);
      }
    } finally {
      setIsLoading(prev => {
        const next = new Set(prev);
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
      if (ext !== 'txt' && ext !== 'md') {
        setError(`${file.name}: Only .txt and .md files are allowed`);
        continue;
      }
      
      try {
        const content = await file.text();
        newStagedFiles.push({ filename: file.name, content });
      } catch (err) {
        setError(`Could not read ${file.name}`);
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
      if (ext !== 'txt' && ext !== 'md') {
        setError(`${file.name}: Only .txt and .md files are allowed`);
        continue;
      }
      
      try {
        const content = await file.text();
        newStagedFiles.push({ filename: file.name, content });
      } catch (err) {
        setError(`Could not read ${file.name}`);
      }
    }
    
    if (newStagedFiles.length > 0) {
      setStagedFiles(prev => [...prev, ...newStagedFiles]);
    }
  };

  const removeStagedFile = (index: number) => {
    setStagedFiles(prev => prev.filter((_, i) => i !== index));
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

  const sendMessage = async () => {
    if (!newMessage.trim() || !user || !currentChat || isLoading.has(currentChat)) return;

    const ctx = createContextGuard();
    const messageText = newMessage;
    const filesToSend = [...stagedFiles];

    setNewMessage('');
    setStagedFiles([]);
    setIsLoading(prev => new Set(prev).add(ctx.chat!));

    // Optimistically add user message (without ID yet - will be updated from response)
    const optimisticUserMsg: ChatMessage = {
      role: 'user',
      content: messageText,
      timestamp: new Date().toISOString(),
      attached_files: filesToSend.length > 0 ? filesToSend : undefined
    };
    setMessages(prev => [...prev, optimisticUserMsg]);
    setTotalMessages(prev => prev + 1);

    // Add placeholder for streaming assistant message
    const streamingAssistantMsg: ChatMessage = {
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString()
    };
    setMessages(prev => [...prev, streamingAssistantMsg]);
    // Scroll to show the start of the new message, then stop auto-scrolling
    // so user can read from the top as it streams
    requestAnimationFrame(() => scrollToBottom());

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
          username: user.username,
          chat_name: ctx.chat,
          message: messageText,
          project: ctx.project,
          attached_files: filesToSend.length > 0 ? filesToSend : undefined,
          model: selectedModel
        }),
        signal: abortController.signal
      });

      if (!response.ok) {
        const data = await response.json();
        setError(data.detail || 'Failed to send message');
        if (!ctx.isStale()) {
          // Remove both optimistic messages
          setMessages(prev => prev.slice(0, -2));
          setTotalMessages(prev => prev - 1);
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
          } else if (event.type === 'content') {
            accumulatedContent += event.data.delta;
            // Update the streaming message with new content
            setMessages(prev => {
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
            setMessages(prev => {
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
          } else if (event.type === 'done') {
            const data = event.data;

            // Build complete messages with IDs from response
            const userMsgWithId: ChatMessage = {
              ...optimisticUserMsg,
              id: data.user_message_id,
              parent_id: currentLeafId
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

            // Replace optimistic messages with complete ones
            setMessages(prev => [...prev.slice(0, -2), userMsgWithId, assistantMessage]);

            // Add to the full message tree
            setAllMessages(prev => [...prev, userMsgWithId, assistantMessage]);

            // Update current leaf
            setCurrentLeafId(data.current_leaf_id || data.assistant_message_id);

            setTotalMessages(prev => prev + 1);
            setMessageOffset(prev => prev + 2);
            setStats(data.stats);
            setContextStartIndex(data.context_start_index || 1);
            fetchUserStats();
            fetchFreeTokens();
            // Don't scroll on done - let user stay where they were reading
          } else if (event.type === 'error') {
            setError(event.data.detail || 'Failed to send message');
            if (!ctx.isStale()) {
              setMessages(prev => prev.slice(0, -2));
              setTotalMessages(prev => prev - 1);
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
          setMessages(prev => prev.slice(0, -2));
          setTotalMessages(prev => prev - 1);
        }
      } else {
        setError('Could not send message');
        if (!ctx.isStale()) {
          setMessages(prev => prev.slice(0, -2));
          setTotalMessages(prev => prev - 1);
          setNewMessage(messageText);
          setStagedFiles(filesToSend);
        }
      }
    } finally {
      setIsLoading(prev => {
        const next = new Set(prev);
        next.delete(ctx.chat!);
        return next;
      });
    }
  };

  const startRenameChat = (chatName: string) => {
    setEditingChat(chatName);
    setEditingName(chatName);
  };

  const cancelRename = () => {
    setEditingChat(null);
    setEditingName('');
  };

  const saveRename = async () => {
    if (!editingChat || !editingName.trim() || !user) return;
    if (isLoading.has(editingChat)) {
      setError('Cannot rename a chat while it is processing a message');
      return;
    }
    if (editingName.trim() === editingChat) {
      cancelRename();
      return;
    }

    const ctx = createContextGuard();
    const oldName = editingChat;
    const newName = editingName.trim();

    try {
      const response = await fetch('/api/rename-chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: user.username,
          old_name: oldName,
          new_name: newName,
          project: ctx.project
        })
      });

      if (response.ok) {
        // Update current chat name if this is the active chat
        if (ctx.chat === oldName) {
          setCurrentChat(newName);
          currentChatRef.current = newName;
        }
        
        // Reload the appropriate chat list
        if (ctx.project) {
          const chatList = await refreshProjectChats(ctx.project, true);
          if (!chatList) {
            setError('Failed to reload chats after rename');
            return;
          }
        } else {
          await loadChatList();
        }
        
        cancelRename();
      } else {
        const data = await response.json();
        setError(data.detail || 'Failed to rename chat');
      }
    } catch (err) {
      console.error('Rename error:', err);
      setError('Could not rename chat');
    }
  };

  const handleDeleteChat = async (chatName: string) => {
    if (isLoading.has(chatName)) {
      setError('Cannot delete a chat while it is processing a message');
      return;
    }
    if (!window.confirm(`Delete "${chatName}"? This cannot be undone.`)) return;
    if (!user) return;

    const ctx = createContextGuard();

    try {
      const response = await fetch('/api/delete-chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: user.username,
          chat_name: chatName,
          project: ctx.project
        })
      });

      if (response.ok) {
        if (ctx.project) {
          await refreshProjectChats(ctx.project, true);
        } else {
          loadChatList();
        }
        
        if (ctx.chat === chatName) {
          resetChatState();
        }
      } else {
        const data = await response.json();
        setError(data.detail || 'Failed to delete chat');
      }
    } catch (err) {
      setError('Could not delete chat');
    }
  };

  const startRenameProject = (projectName: string) => {
    setEditingProject(projectName);
    setEditingProjectName(projectName);
  };

  const cancelRenameProject = () => {
    setEditingProject(null);
    setEditingProjectName('');
  };

  const saveRenameProject = async () => {
    if (!editingProject || !editingProjectName.trim() || !user) return;
    if (editingProjectName.trim() === editingProject) {
      cancelRenameProject();
      return;
    }

    const ctx = createContextGuard();
    const oldName = editingProject;
    const newName = editingProjectName.trim();

    try {
      const response = await fetch('/api/rename-project', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: user.username,
          old_name: oldName,
          new_name: newName
        })
      });

      if (response.ok) {
        // Migrate cache from old name to new name
        setProjectChatsCache(prev => {
          const newCache = { ...prev };
          if (newCache[oldName]) {
            newCache[newName] = newCache[oldName];
            delete newCache[oldName];
          }
          return newCache;
        });
        
        cancelRenameProject();
        
        if (ctx.project === oldName) {
          enterProject(newName);
        } else {
          loadChatList();
        }
      } else {
        const data = await response.json();
        setError(data.detail || 'Failed to rename project');
      }
    } catch (err) {
      setError('Could not rename project');
    }
  };

  const handleDeleteProject = async (projectName: string) => {
    if (!window.confirm(`Delete project "${projectName}" and all its chats? This cannot be undone.`)) return;
    if (!user) return;

    const ctx = createContextGuard();

    try {
      const response = await fetch('/api/delete-project', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: user.username,
          project_name: projectName
        })
      });

      if (response.ok) {
        setProjectChatsCache(prev => {
          const newCache = { ...prev };
          delete newCache[projectName];
          return newCache;
        });
        
        loadChatList();
        
        if (ctx.project === projectName) {
          resetProjectState();
        }
      } else {
        const data = await response.json();
        setError(data.detail || 'Failed to delete project');
      }
    } catch (err) {
      setError('Could not delete project');
    }
  };

  // Login screen
  if (!user) {
    return (
      <div style={styles.container}>
        <div style={styles.loginBox}>
          <h1 style={styles.title}>ChatGPT Interface</h1>
          <input
            type="text"
            placeholder="Username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleLogin()}
            style={styles.input}
          />
          <button onClick={handleLogin} style={styles.button}>
            Login
          </button>
          {error && <p style={styles.error}>{error}</p>}
        </div>
      </div>
    );
  }

  // API key entry screen
  if (needsApiKey) {
    return (
      <div style={styles.container}>
        <div style={styles.loginBox}>
          <h1 style={styles.title}>Welcome, {user.username}!</h1>
          <p style={styles.subtitle}>Enter your API keys (at least OpenAI required):</p>

          <div style={{ marginBottom: '12px' }}>
            <label style={{ display: 'block', marginBottom: '4px', fontSize: '14px', color: '#aaa' }}>
              OpenAI API Key {apiKeysStatus.has_openai && <span style={{ color: '#4ade80' }}>(configured)</span>}
            </label>
            <input
              type="password"
              placeholder="sk-..."
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              style={styles.input}
            />
          </div>

          <div style={{ marginBottom: '16px' }}>
            <label style={{ display: 'block', marginBottom: '4px', fontSize: '14px', color: '#aaa' }}>
              Anthropic API Key (optional) {apiKeysStatus.has_anthropic && <span style={{ color: '#4ade80' }}>(configured)</span>}
            </label>
            <input
              type="password"
              placeholder="sk-ant-..."
              value={anthropicKey}
              onChange={(e) => setAnthropicKey(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSaveApiKey()}
              style={styles.input}
            />
          </div>

          <button onClick={handleSaveApiKey} style={styles.button}>
            Save API Keys
          </button>
          {error && <p style={styles.error}>{error}</p>}
        </div>
      </div>
    );
  }

  // Main chat interface
  return (
    <div style={styles.container}>
      <div style={styles.mainLayout}>
        {/* Mobile hamburger button */}
        {isMobile && (
          <button 
            style={styles.hamburgerButton}
            onClick={() => setSidebarOpen(!sidebarOpen)}
          >
            ☰
          </button>
        )}
        
        {/* Mobile overlay when sidebar is open */}
        {isMobile && sidebarOpen && (
          <div 
            style={styles.sidebarOverlay}
            onClick={() => setSidebarOpen(false)}
          />
        )}
        
        {/* Sidebar */}
        <div style={{
          ...styles.sidebar,
          ...(isMobile ? styles.sidebarMobile : {}),
          ...(isMobile && !sidebarOpen ? styles.sidebarHidden : {})
        }}>
          <div style={styles.sidebarHeader}>
            <div style={styles.sidebarHeaderRow}>
              <h2 style={styles.sidebarTitle}>ChatGPT</h2>
              {isMobile && (
                <button 
                  onClick={() => setSidebarOpen(false)} 
                  style={styles.closeSidebarButton}
                >
                  ✕
                </button>
              )}
              <button onClick={handleLogout} style={styles.logoutButton} title="Logout">↩</button>
            </div>
            <p style={styles.muted}>
              {user.username}
              <span 
                style={styles.statsIcon}
                onMouseEnter={() => setShowStatsTooltip(true)}
                onMouseLeave={() => setShowStatsTooltip(false)}
              >
                ⓘ
              </span>
              {showStatsTooltip && userStats && (
                <div style={styles.statsTooltip}>
                  {freeTokens && (
                    <>
                      <div style={styles.statsSection}>
                        <div style={styles.statsSectionTitle}>Free Tokens (Resets {freeTokens.resets_at_eastern})</div>
                        <div style={styles.statsRow}>{Math.max(0, freeTokens.remaining).toLocaleString()} / {freeTokens.total_free.toLocaleString()}</div>
                      </div>
                      <div style={styles.statsSeparator} />
                    </>
                  )}
                  <div style={styles.statsSection}>
                    <div style={styles.statsSectionTitle}>Lifetime</div>
                    <div style={styles.statsRow}>Prompts: {userStats.lifetime_gpt_prompts.toLocaleString()} GPT | {userStats.lifetime_sonnet_prompts.toLocaleString()} Sonnet | {userStats.lifetime_prompts.toLocaleString()} Total</div>
                    <div style={styles.statsRow}>
                      Tokens: I:{userStats.lifetime_input_tokens.toLocaleString()} C:{userStats.lifetime_cached_tokens.toLocaleString()} O:{userStats.lifetime_output_tokens.toLocaleString()} R:{userStats.lifetime_reasoning_tokens.toLocaleString()}
                    </div>
                    <div style={styles.statsRow}>Cost: ${userStats.lifetime_cost.toFixed(4)}</div>
                    <div style={styles.statsRow}>Cache Miss: {userStats.lifetime_cache_miss_percent.toFixed(1)}%</div>
                  </div>
                  <div style={styles.statsSeparator} />
                  <div style={styles.statsSection}>
                    <div style={styles.statsSectionTitle}>Current Month</div>
                    <div style={styles.statsRow}>Active Days: {userStats.monthly_active_days}</div>
                    <div style={styles.statsRow}>Prompts: {userStats.monthly_gpt_prompts.toLocaleString()} GPT | {userStats.monthly_sonnet_prompts.toLocaleString()} Sonnet | {userStats.monthly_prompts.toLocaleString()} Total</div>
                    <div style={styles.statsRow}>
                      Tokens: I:{userStats.monthly_input_tokens.toLocaleString()} C:{userStats.monthly_cached_tokens.toLocaleString()} O:{userStats.monthly_output_tokens.toLocaleString()} R:{userStats.monthly_reasoning_tokens.toLocaleString()}
                    </div>
                    <div style={styles.statsRow}>Cost: ${userStats.monthly_cost.toFixed(4)}</div>
                  </div>
                  <div style={styles.statsSeparator} />
                  <div style={styles.statsSection}>
                    <div style={styles.statsSectionTitle}>Today</div>
                    <div style={styles.statsRow}>Prompts: {userStats.today_gpt_prompts.toLocaleString()} GPT | {userStats.today_sonnet_prompts.toLocaleString()} Sonnet | {userStats.today_prompts.toLocaleString()} Total</div>
                    <div style={styles.statsRow}>
                      Tokens: I:{userStats.today_input_tokens.toLocaleString()} C:{userStats.today_cached_tokens.toLocaleString()} O:{userStats.today_output_tokens.toLocaleString()} R:{userStats.today_reasoning_tokens.toLocaleString()}
                    </div>
                    <div style={styles.statsRow}>Cost: ${userStats.today_cost.toFixed(4)}</div>
                  </div>
                  <div style={styles.statsSeparator} />
                  <div style={styles.statsSection}>
                    <div style={styles.statsSectionTitle}>Daily Averages ({userStats.days_since_first} days)</div>
                    <div style={styles.statsRow}>Prompts/day: {userStats.avg_gpt_prompts_per_day.toFixed(1)} GPT | {userStats.avg_sonnet_prompts_per_day.toFixed(1)} Sonnet | {userStats.avg_prompts_per_day.toFixed(1)} Total</div>
                    <div style={styles.statsRow}>
                      TPD: I:{userStats.avg_input_per_day.toFixed(0)} C:{userStats.avg_cached_per_day.toFixed(0)} O:{userStats.avg_output_per_day.toFixed(0)} R:{userStats.avg_reasoning_per_day.toFixed(0)}
                    </div>
                    <div style={styles.statsRow}>Cost/day: ${userStats.avg_cost_per_day.toFixed(4)}</div>
                  </div>
                  <div style={styles.statsSeparator} />
                  <div style={styles.statsSection}>
                    <div style={styles.statsSectionTitle}>Avg Context Growth</div>
                    <div style={styles.statsRow}>GPT: {Math.round(userStats.avg_gpt_context_growth).toLocaleString()} tokens</div>
                    <div style={styles.statsRow}>Sonnet: {Math.round(userStats.avg_sonnet_context_growth).toLocaleString()} tokens</div>
                  </div>
                </div>
              )}
            </p>
          </div>

          {/* Projects Section */}
          <div style={styles.section}>
            <div style={styles.sectionHeader}>
              <span 
                style={styles.sectionHeaderClickable}
                onClick={() => setProjectsExpanded(!projectsExpanded)}
              >
                <span style={styles.expandIcon}>{projectsExpanded ? '▼' : '▶'}</span>
                <span>Projects</span>
              </span>
              <button 
                onClick={startCreateProject}
                style={styles.addButton}
                title="New project"
              >
                +
              </button>
            </div>
            {projectsExpanded && (
              <div style={styles.sectionList}>
                {creatingProject && (
                  <div style={styles.listItem}>
                    <input
                      type="text"
                      value={newItemName}
                      onChange={(e) => setNewItemName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') saveNewProject();
                        if (e.key === 'Escape') cancelCreate();
                      }}
                      autoFocus
                      placeholder="Project name"
                      style={styles.editInput}
                    />
                    <div style={styles.chatActions}>
                      <button onClick={saveNewProject} style={styles.iconButtonCheck} title="Save">✓</button>
                      <button onClick={cancelCreate} style={styles.iconButtonX} title="Cancel">✕</button>
                    </div>
                  </div>
                )}
                {projects.slice(0, projects.length > 6 ? 5 : 6).map(project => (
                  <div
                    key={project}
                    style={{
                      ...styles.listItem,
                      backgroundColor: project === currentProject ? '#3a3a5e' : 'transparent'
                    }}
                  >
                    {editingProject === project ? (
                      <>
                        <input
                          type="text"
                          value={editingProjectName}
                          onChange={(e) => setEditingProjectName(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') saveRenameProject();
                            if (e.key === 'Escape') cancelRenameProject();
                          }}
                          autoFocus
                          style={styles.editInput}
                        />
                        <div style={styles.chatActions}>
                          <button onClick={saveRenameProject} style={styles.iconButtonCheck} title="Save">✓</button>
                          <button onClick={cancelRenameProject} style={styles.iconButtonX} title="Cancel">✕</button>
                        </div>
                      </>
                    ) : (
                      <>
                        <span 
                          onClick={() => enterProject(project)}
                          style={styles.chatName}
                        >
                          📁 {project}
                        </span>
                        <div style={styles.chatActions}>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              startRenameProject(project);
                            }}
                            style={styles.iconButton}
                            title="Rename"
                          >
                            ✏️
                          </button>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              handleDeleteProject(project);
                            }}
                            style={styles.iconButton}
                            title="Delete"
                          >
                            🗑️
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                ))}
                {projects.length > 6 && (
                  <div 
                    style={styles.seeMoreLink}
                    onClick={() => setViewMode('projectList')}
                  >
                    See More... ({projects.length - 5} more)
                  </div>
                )}
                {projects.length === 0 && !creatingProject && <p style={styles.mutedSmall}>No projects yet</p>}
              </div>
            )}
          </div>

          {/* Chats Section */}
          <div style={styles.section}>
            <div style={styles.sectionHeader}>
              <span 
                style={styles.sectionHeaderClickable}
                onClick={() => setChatsExpanded(!chatsExpanded)}
              >
                <span style={styles.expandIcon}>{chatsExpanded ? '▼' : '▶'}</span>
                <span>{currentProject ? `Chats in ${currentProject}` : 'Chats'}</span>
              </span>
              {currentProject && (
                <button 
                  onClick={(e) => { e.stopPropagation(); exitProject(); }}
                  style={styles.exitProjectButton}
                  title="Exit project"
                >
                  ✕
                </button>
              )}
              <button 
                onClick={startCreateChat}
                style={styles.addButton}
                title="New chat"
              >
                +
              </button>
            </div>
            {chatsExpanded && (
              <div style={styles.sectionList}>
                {creatingChat && (
                  <div style={styles.listItem}>
                    <input
                      type="text"
                      value={newItemName}
                      onChange={(e) => setNewItemName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') saveNewChat();
                        if (e.key === 'Escape') cancelCreate();
                      }}
                      autoFocus
                      placeholder="Chat name"
                      style={styles.editInput}
                    />
                    <div style={styles.chatActions}>
                      <button onClick={saveNewChat} style={styles.iconButtonCheck} title="Save">✓</button>
                      <button onClick={cancelCreate} style={styles.iconButtonX} title="Cancel">✕</button>
                    </div>
                  </div>
                )}
                {chats.slice(0, chats.length > 6 ? 5 : 6).map(chat => (
                  <div
                    key={chat}
                    style={{
                      ...styles.listItem,
                      backgroundColor: chat === currentChat ? '#3a3a5e' : 'transparent'
                    }}
                  >
                    {editingChat === chat ? (
                      <>
                        <input
                          type="text"
                          value={editingName}
                          onChange={(e) => setEditingName(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') saveRename();
                            if (e.key === 'Escape') cancelRename();
                          }}
                          autoFocus
                          style={styles.editInput}
                        />
                        <div style={styles.chatActions}>
                          <button onClick={saveRename} style={styles.iconButtonCheck} title="Save">✓</button>
                          <button onClick={cancelRename} style={styles.iconButtonX} title="Cancel">✕</button>
                        </div>
                      </>
                    ) : (
                      <>
                        <span 
                          onClick={() => openChat(chat)}
                          style={styles.chatName}
                        >
                          {chat}
                        </span>
                        <div style={styles.chatActions}>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              startRenameChat(chat);
                            }}
                            style={styles.iconButton}
                            title="Rename"
                          >
                            ✏️
                          </button>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              handleDeleteChat(chat);
                            }}
                            style={styles.iconButton}
                            title="Delete"
                          >
                            🗑️
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                ))}
                {chats.length > 6 && (
                  <div 
                    style={styles.seeMoreLink}
                    onClick={() => setViewMode('chatList')}
                  >
                    See More... ({chats.length - 5} more)
                  </div>
                )}
                {chats.length === 0 && !creatingChat && <p style={styles.mutedSmall}>No chats yet</p>}
              </div>
            )}
          </div>

          {stats && (
            <div style={styles.statsBox}>
              <p style={styles.statsText}>Prompts: {stats.gpt_prompts ?? 0} GPT | {stats.sonnet_prompts ?? 0} Sonnet | {stats.total_prompts} Total</p>
              <p style={styles.statsText}>Cost: ${stats.total_cost.toFixed(4)}</p>
              {stats.first_prompt_date && (() => {
                const firstDate = new Date(stats.first_prompt_date);
                const today = new Date();
                const daysSinceStart = Math.max(1, Math.floor((today.getTime() - firstDate.getTime()) / (1000 * 60 * 60 * 24)));
                const totalTokens = (stats.total_input_tokens || 0) + (stats.total_cached_tokens || 0) + (stats.total_output_tokens || 0);
                const avgTPD = totalTokens / daysSinceStart;
                const cacheMisses = stats.total_input_tokens || 0;
                const totalInputToAPI = cacheMisses + (stats.total_cached_tokens || 0);
                const missPercent = totalInputToAPI > 0 ? (cacheMisses / totalInputToAPI) * 100 : 0;

                return (
                  <>
                    <p style={styles.statsText}>Cache Misses: {cacheMisses.toLocaleString()} ({missPercent.toFixed(1)}%)</p>
                    <p style={styles.statsText}>Avg TPD: {avgTPD.toFixed(0)}</p>
                    <p style={styles.statsText}>Days: {daysSinceStart}</p>
                    <p style={styles.statsText}>Avg Context: {Math.round(stats.avg_gpt_context_growth ?? 0).toLocaleString()} GPT | {Math.round(stats.avg_sonnet_context_growth ?? 0).toLocaleString()} Sonnet</p>
                  </>
                );
              })()}
            </div>
          )}
        </div>
        
        {/* Main content area - shows chat OR list view */}
        <div style={{
          ...styles.chatArea,
          ...(isMobile ? {
            position: 'absolute' as const,
            top: '50px',
            left: 0,
            right: 0,
            bottom: '80px', // Leave room for fixed input area
          } : {})
        }}>
          {viewMode === 'chat' ? (
            // Normal chat interface
            currentChat ? (
              <>
                <div style={styles.chatHeader}>
                  <h2 style={styles.chatTitle}>{currentChat}</h2>
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
                    // Determine if this message is in context
                    // Map display index to actual backend index
                    // totalMessages includes system message, messages.length does not
                    // So first displayed message is at backend index (totalMessages - messages.length)
                    const firstDisplayedBackendIndex = totalMessages - messages.length;
                    const actualBackendIndex = firstDisplayedBackendIndex + i;
                    const isInContext = actualBackendIndex >= contextStartIndex;
                    
                    // Choose background color based on context and role
                    let backgroundColor;
                    if (!isInContext) {
                      // Out of context: grayed out versions
                      backgroundColor = msg.role === 'user' ? '#1f1f35' : '#171728';
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
                        <div style={styles.messageRole}>
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
                        </div>
                        
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
                            <div style={styles.messageContent} className="messageContent">
                              <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>{convertMathDelimiters(msg.content)}</ReactMarkdown>
                            </div>
                            <div style={styles.messageFooter}>
                              {msg.tokens && (
                                <span style={styles.messageTokens}>
                                  {msg.tokens} | {msg.cost}
                                  {msg.service_tier && ` (${msg.service_tier === 'flex' ? 'Flex' : 'Standard'})`}
                                  {msg.model && ` | ${msg.model === 'gpt-5.2' ? 'GPT' : msg.model === 'claude-sonnet-4.5' ? 'Sonnet' : msg.model === 'claude-opus-4.5' ? 'Opus' : msg.model}`}
                                </span>
                              )}
                              {/* Branch navigation - show only for user messages with siblings */}
                              {msg.role === 'user' && (() => {
                                // Debug: log message info
                                console.log('User message:', { id: msg.id, parent_id: msg.parent_id, timestamp: msg.timestamp, allMessagesLength: allMessages.length });
                                if (!msg.id) {
                                  console.log('No msg.id - skipping branch nav');
                                  return null;
                                }
                                const siblings = getSiblings(allMessages, msg.id);
                                console.log('Siblings for', msg.id, ':', siblings.map(s => ({ id: s.id, parent_id: s.parent_id })));
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
                                // Debug: always log timestamp status
                                if (!msg.timestamp) console.log('No timestamp for message:', msg.role, msg.id?.slice(0, 8) || 'no-id');
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
                  {isLoading.has(currentChat) && (
                    <div style={styles.message}>
                      <div style={styles.messageRole}>Assistant</div>
                      <div style={styles.messageContent}>Thinking...</div>
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
                        accept=".txt,.md"
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
                        onClick={() => setShowUpdatesModal(true)}
                        style={{
                          ...styles.updateButton,
                          backgroundColor: updatesText.trim() ? '#2d6a4f' : '#2a2a4e'
                        }}
                        title={updatesText.trim() ? 'Updates active (click to edit)' : 'Add updates'}
                      >
                        Update
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
              </>
            ) : currentProject ? (
              // Project landing page
              <div style={styles.projectLanding}>
                <div style={styles.projectLandingHeader}>
                  <h2 style={styles.projectLandingTitle}>📁 {currentProject}</h2>
                </div>
                
                <div style={styles.projectLandingContent}>
                  {/* Center column: Chats */}
                  <div style={styles.projectChatsColumn}>
                    <div style={styles.projectSectionHeader}>
                      <h3 style={styles.projectSectionTitle}>Chats</h3>
                      <button onClick={startCreateChat} style={styles.projectAddButton}>+ New Chat</button>
                    </div>
                    
                    <input
                      type="text"
                      placeholder="Search chats..."
                      value={chatSearchQuery}
                      onChange={(e) => setChatSearchQuery(e.target.value)}
                      style={styles.chatSearchInput}
                    />
                    
                    <div style={styles.projectChatsList}>
                      {(() => {
                        // Filter and group chats by date
                        const filtered = projectChatsDetailed.filter(chat =>
                          chat.name.toLowerCase().includes(chatSearchQuery.toLowerCase())
                        );
                        
                        if (filtered.length === 0) {
                          return <p style={styles.mutedSmall}>No chats found</p>;
                        }
                        
                        // Group by date
                        const now = new Date();
                        const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
                        const yesterday = new Date(today.getTime() - 24 * 60 * 60 * 1000);
                        const weekAgo = new Date(today.getTime() - 7 * 24 * 60 * 60 * 1000);
                        
                        const groups: { [key: string]: ChatCardInfo[] } = {};
                        
                        filtered.forEach(chat => {
                          const chatDate = chat.lastActive ? new Date(chat.lastActive) : new Date(0);
                          const chatDay = new Date(chatDate.getFullYear(), chatDate.getMonth(), chatDate.getDate());
                          
                          let groupKey: string;
                          if (chatDay.getTime() === today.getTime()) {
                            groupKey = 'Today';
                          } else if (chatDay.getTime() === yesterday.getTime()) {
                            groupKey = 'Yesterday';
                          } else if (chatDay.getTime() > weekAgo.getTime()) {
                            groupKey = chatDay.toLocaleDateString('en-US', { weekday: 'long' });
                          } else {
                            groupKey = chatDay.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
                          }
                          
                          if (!groups[groupKey]) {
                            groups[groupKey] = [];
                          }
                          groups[groupKey].push(chat);
                        });
                        
                        // Sort group keys by date (most recent first)
                        // Build dynamic sort order based on today's day
                        const weekdays = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
                        const todayDayIndex = now.getDay(); // 0 = Sunday, 1 = Monday, etc.
                        // Create order: today's weekday going backwards
                        // If today is Wednesday (3), order should be: Wednesday, Tuesday, Monday, Sunday, Saturday, Friday, Thursday
                        const dynamicWeekdays: string[] = [];
                        for (let i = 0; i < 7; i++) {
                          const dayIndex = (todayDayIndex - i + 7) % 7;
                          dynamicWeekdays.push(weekdays[dayIndex]);
                        }
                        const sortOrder = ['Today', 'Yesterday', ...dynamicWeekdays.slice(2)]; // Skip first two (Today/Yesterday cover those)
                        
                        const sortedKeys = Object.keys(groups).sort((a, b) => {
                          const aIdx = sortOrder.indexOf(a);
                          const bIdx = sortOrder.indexOf(b);
                          if (aIdx !== -1 && bIdx !== -1) return aIdx - bIdx;
                          if (aIdx !== -1) return -1;
                          if (bIdx !== -1) return 1;
                          // For month groups, sort by date descending
                          return b.localeCompare(a);
                        });
                        
                        return sortedKeys.map(groupKey => (
                          <div key={groupKey}>
                            <div style={styles.chatGroupHeader}>{groupKey}</div>
                            {groups[groupKey].map(chat => (
                              <div
                                key={chat.name}
                                style={styles.chatCard}
                                onClick={() => openChat(chat.name)}
                              >
                                <div style={styles.chatCardName}>{chat.name}</div>
                                <div style={styles.chatCardPreview}>
                                  {chat.lastMessage?.substring(0, 80) || 'No messages yet'}
                                  {chat.lastMessage && chat.lastMessage.length > 80 ? '...' : ''}
                                </div>
                                <div style={styles.chatCardMeta}>
                                  <span>{chat.lastActive ? formatTimestamp(chat.lastActive).split('  ')[1] || 'Unknown' : 'Never'}</span>
                                  <span>{chat.messageCount} msgs</span>
                                  <span>${chat.cost.toFixed(2)}</span>
                                </div>
                              </div>
                            ))}
                          </div>
                        ));
                      })()}
                    </div>
                  </div>
                  
                  {/* Right column: Model + Instructions + Files */}
                  <div style={styles.projectConfigColumn}>
                    {/* Default Model section */}
                    <div style={styles.projectSection}>
                      <div style={styles.projectSectionHeader}>
                        <h3 style={styles.projectSectionTitle}>Default Model</h3>
                      </div>
                      <select
                        value={projectModel || 'gpt-5.2'}
                        onChange={(e) => handleProjectModelChange(e.target.value)}
                        style={styles.projectModelSelector}
                      >
                        {availableModels.map(model => (
                          <option key={model.id} value={model.id}>{model.name}</option>
                        ))}
                      </select>
                      <p style={styles.projectModelHelperText}>New chats will use this model by default</p>
                    </div>

                    {/* Instructions section */}
                    <div style={styles.projectSection}>
                      <div style={styles.projectSectionHeader}>
                        <h3 style={styles.projectSectionTitle}>Instructions</h3>
                        <span style={styles.tokenBadge}>{projectInstructionsTokens.toLocaleString()} tokens</span>
                      </div>
                      <div style={styles.instructionsPreview}>
                        {projectInstructions.substring(0, 200)}
                        {projectInstructions.length > 200 ? '...' : ''}
                      </div>
                      <button
                        onClick={() => {
                          setEditingInstructions(projectInstructions);
                          setShowInstructionsModal(true);
                        }}
                        style={styles.editInstructionsButton}
                      >
                        Edit Instructions
                      </button>
                    </div>
                    
                    {/* Files section */}
                    <div style={styles.projectSection}>
                      <div style={styles.projectSectionHeader}>
                        <h3 style={styles.projectSectionTitle}>Project Files</h3>
                        <span style={styles.tokenBadge}>{projectFilesTotalTokens.toLocaleString()} tokens</span>
                      </div>
                      
                      <div style={styles.filesList}>
                        {projectFiles.length === 0 ? (
                          <p style={styles.mutedSmall}>No files uploaded</p>
                        ) : (
                          projectFiles.map(file => (
                            <div key={file.filename} style={{
                              ...styles.fileItem,
                              opacity: file.staged ? 1 : 0.5
                            }}>
                              <input
                                type="checkbox"
                                checked={file.staged}
                                onChange={() => handleToggleFileStaged(file.filename, file.staged)}
                                style={styles.fileCheckbox}
                                title={file.staged ? "Uncheck to exclude from chats" : "Check to include in chats"}
                              />
                              <span
                                style={{
                                  ...styles.fileName,
                                  textDecoration: file.staged ? 'none' : 'line-through'
                                }}
                                onMouseEnter={(e) => {
                                  const rect = e.currentTarget.getBoundingClientRect();
                                  setFilenameTooltipPos({x: rect.left, y: rect.bottom + 4});
                                  filenameTooltipTimeoutRef.current = setTimeout(() => {
                                    setHoveredFilename(file.filename);
                                  }, 200);
                                }}
                                onMouseLeave={() => {
                                  if (filenameTooltipTimeoutRef.current) {
                                    clearTimeout(filenameTooltipTimeoutRef.current);
                                    filenameTooltipTimeoutRef.current = null;
                                  }
                                  setHoveredFilename(null);
                                }}
                              >{file.filename}</span>
                              {hoveredFilename === file.filename && (
                                <div style={{...styles.filenameTooltip, left: filenameTooltipPos.x, top: filenameTooltipPos.y}}>
                                  {file.filename}
                                </div>
                              )}
                              <span style={styles.fileTokens}>{file.tokens.toLocaleString()}</span>
                              <button
                                onClick={() => handleDeleteProjectFile(file.filename)}
                                style={styles.fileDeleteButton}
                                title="Delete file"
                              >
                                🗑️
                              </button>
                            </div>
                          ))
                        )}
                      </div>
                      
                      <input
                        type="file"
                        ref={fileInputRef}
                        onChange={handleFileUpload}
                        multiple
                        accept=".txt,.md"
                        style={{ display: 'none' }}
                      />
                      <button
                        onClick={() => fileInputRef.current?.click()}
                        disabled={filesUploading}
                        style={styles.uploadButton}
                      >
                        {filesUploading ? 'Uploading...' : '+ Add Files'}
                      </button>
                    </div>
                  </div>
                </div>
                
                {/* Instructions edit modal */}
                {showInstructionsModal && (
                  <div style={styles.modalOverlay} onClick={() => setShowInstructionsModal(false)}>
                    <div style={styles.modal} onClick={e => e.stopPropagation()}>
                      <h3 style={styles.modalTitle}>Edit Instructions</h3>
                      <p style={styles.modalDescription}>
                        These instructions are added to the system prompt for all chats in this project.
                      </p>
                      <textarea
                        value={editingInstructions}
                        onChange={(e) => setEditingInstructions(e.target.value)}
                        style={styles.updatesTextarea}
                        rows={12}
                      />
                      <div style={styles.modalFooter}>
                        <span style={styles.tokenCount}>
                          ~{editingInstructions ? Math.ceil(editingInstructions.length / 4) : 0} tokens (estimate)
                        </span>
                        <div style={styles.modalActions}>
                          <button onClick={() => setShowInstructionsModal(false)} style={styles.modalCancelButton}>
                            Cancel
                          </button>
                          <button
                            onClick={updateProjectInstructions}
                            disabled={instructionsSaving}
                            style={styles.modalSaveButton}
                          >
                            {instructionsSaving ? 'Saving...' : 'Save'}
                          </button>
                        </div>
                      </div>
                    </div>
                  </div>
                )}

              </div>
            ) : (
              <div style={styles.noChat}>
                <p>Select or create a chat to start</p>
              </div>
            )
          ) : viewMode === 'projectList' ? (
            // Full project list view
            <>
              <div style={styles.listViewHeader}>
                <button 
                  onClick={() => setViewMode('chat')}
                  style={styles.backButton}
                >
                  ← Back
                </button>
                <h2 style={styles.listViewTitle}>All Projects</h2>
                <button 
                  onClick={startCreateProject}
                  style={styles.listViewAddButton}
                >
                  + New Project
                </button>
              </div>
              <div style={styles.listViewContent}>
                {creatingProject && (
                  <div style={styles.listViewItem}>
                    <input
                      type="text"
                      value={newItemName}
                      onChange={(e) => setNewItemName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') saveNewProject();
                        if (e.key === 'Escape') cancelCreate();
                      }}
                      autoFocus
                      placeholder="Project name"
                      style={styles.editInput}
                    />
                    <div style={styles.chatActions}>
                      <button onClick={saveNewProject} style={styles.iconButtonCheck} title="Save">✓</button>
                      <button onClick={cancelCreate} style={styles.iconButtonX} title="Cancel">✕</button>
                    </div>
                  </div>
                )}
                {projects.map(project => (
                  <div
                    key={project}
                    style={{
                      ...styles.listViewItem,
                      backgroundColor: project === currentProject ? '#3a3a5e' : 'transparent'
                    }}
                  >
                    {editingProject === project ? (
                      <>
                        <input
                          type="text"
                          value={editingProjectName}
                          onChange={(e) => setEditingProjectName(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') saveRenameProject();
                            if (e.key === 'Escape') cancelRenameProject();
                          }}
                          autoFocus
                          style={styles.editInput}
                        />
                        <div style={styles.chatActions}>
                          <button onClick={saveRenameProject} style={styles.iconButtonCheck} title="Save">✓</button>
                          <button onClick={cancelRenameProject} style={styles.iconButtonX} title="Cancel">✕</button>
                        </div>
                      </>
                    ) : (
                      <>
                        <span 
                          onClick={() => {
                            enterProject(project);
                            setViewMode('chat');
                          }}
                          style={styles.listViewItemName}
                        >
                          📁 {project}
                        </span>
                        <div style={styles.chatActions}>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              startRenameProject(project);
                            }}
                            style={styles.iconButton}
                            title="Rename"
                          >
                            ✏️
                          </button>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              handleDeleteProject(project);
                            }}
                            style={styles.iconButton}
                            title="Delete"
                          >
                            🗑️
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                ))}
                {projects.length === 0 && !creatingProject && (
                  <div style={styles.listViewEmpty}>
                    <p>No projects yet</p>
                    <p style={styles.mutedSmall}>Create your first project to get started</p>
                  </div>
                )}
              </div>
            </>
          ) : (
            // Full chat list view
            <>
              <div style={styles.listViewHeader}>
                <button 
                  onClick={() => setViewMode('chat')}
                  style={styles.backButton}
                >
                  ← Back
                </button>
                <h2 style={styles.listViewTitle}>
                  {currentProject ? `All Chats in ${currentProject}` : 'All Chats'}
                </h2>
                <button 
                  onClick={startCreateChat}
                  style={styles.listViewAddButton}
                >
                  + New Chat
                </button>
              </div>
              <div style={styles.listViewContent}>
                {creatingChat && (
                  <div style={styles.listViewItem}>
                    <input
                      type="text"
                      value={newItemName}
                      onChange={(e) => setNewItemName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') saveNewChat();
                        if (e.key === 'Escape') cancelCreate();
                      }}
                      autoFocus
                      placeholder="Chat name"
                      style={styles.editInput}
                    />
                    <div style={styles.chatActions}>
                      <button onClick={saveNewChat} style={styles.iconButtonCheck} title="Save">✓</button>
                      <button onClick={cancelCreate} style={styles.iconButtonX} title="Cancel">✕</button>
                    </div>
                  </div>
                )}
                {chats.map(chat => (
                  <div
                    key={chat}
                    style={{
                      ...styles.listViewItem,
                      backgroundColor: chat === currentChat ? '#3a3a5e' : 'transparent'
                    }}
                  >
                    {editingChat === chat ? (
                      <>
                        <input
                          type="text"
                          value={editingName}
                          onChange={(e) => setEditingName(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') saveRename();
                            if (e.key === 'Escape') cancelRename();
                          }}
                          autoFocus
                          style={styles.editInput}
                        />
                        <div style={styles.chatActions}>
                          <button onClick={saveRename} style={styles.iconButtonCheck} title="Save">✓</button>
                          <button onClick={cancelRename} style={styles.iconButtonX} title="Cancel">✕</button>
                        </div>
                      </>
                    ) : (
                      <>
                        <span 
                          onClick={() => {
                            openChat(chat);
                            setViewMode('chat');
                          }}
                          style={styles.listViewItemName}
                        >
                          {chat}
                        </span>
                        <div style={styles.chatActions}>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              startRenameChat(chat);
                            }}
                            style={styles.iconButton}
                            title="Rename"
                          >
                            ✏️
                          </button>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              handleDeleteChat(chat);
                            }}
                            style={styles.iconButton}
                            title="Delete"
                          >
                            🗑️
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                ))}
                {chats.length === 0 && !creatingChat && (
                  <div style={styles.listViewEmpty}>
                    <p>No chats yet</p>
                    <p style={styles.mutedSmall}>Create your first chat to get started</p>
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>
      
      {/* Updates Modal */}
      {showUpdatesModal && (
        <div style={styles.modalOverlay} onClick={() => setShowUpdatesModal(false)}>
          <div style={styles.modal} onClick={(e) => e.stopPropagation()}>
            <h3 style={styles.modalTitle}>Updates</h3>
            <p style={styles.modalDescription}>
              Text here is prepended to each message you send. Use it for mid-conversation context updates.
            </p>
            <textarea
              value={updatesText}
              onChange={(e) => updateUpdatesText(e.target.value)}
              placeholder="e.g., Character sheet updates, session notes, reminders..."
              style={styles.updatesTextarea}
            />
            <div style={styles.modalFooter}>
              <span style={{
                ...styles.tokenCount,
                color: updatesTokenCount > 1000 ? '#ff6b6b' : updatesTokenCount > 800 ? '#ffa94d' : '#888'
              }}>
                {updatesTokenCount} tokens {updatesTokenCount > 1000 ? '(high)' : updatesTokenCount > 800 ? '(moderate)' : ''}
              </span>
              <div style={styles.modalActions}>
                <button 
                  onClick={() => setShowUpdatesModal(false)} 
                  style={styles.modalCancelButton}
                >
                  Cancel
                </button>
                <button 
                  onClick={saveUpdates}
                  disabled={updatesLoading}
                  style={{
                    ...styles.modalSaveButton,
                    opacity: updatesLoading ? 0.5 : 1
                  }}
                >
                  {updatesLoading ? 'Saving...' : 'Save'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
      
      {/* API Key modal for model switching - at root level so it's always visible */}
      {showApiKeyModal && pendingModelSwitch && (
        <div style={styles.modalOverlay} onClick={handleApiKeyModalCancel}>
          <div style={{...styles.modal, maxWidth: '400px'}} onClick={e => e.stopPropagation()}>
            <h3 style={styles.modalTitle}>
              {pendingModelSwitch.startsWith('claude') ? 'Anthropic' : 'OpenAI'} API Key Required
            </h3>
            <p style={styles.modalDescription}>
              To use {availableModels.find(m => m.id === pendingModelSwitch)?.name || pendingModelSwitch},
              please enter your {pendingModelSwitch.startsWith('claude') ? 'Anthropic' : 'OpenAI'} API key.
            </p>
            <input
              type="password"
              placeholder={pendingModelSwitch.startsWith('claude') ? 'sk-ant-...' : 'sk-...'}
              value={modalApiKey}
              onChange={(e) => setModalApiKey(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleApiKeyModalSave()}
              style={{...styles.input, width: '100%', marginBottom: '16px'}}
              autoFocus
            />
            <div style={styles.modalActions}>
              <button onClick={handleApiKeyModalCancel} style={styles.modalCancelButton}>
                Cancel
              </button>
              <button
                onClick={handleApiKeyModalSave}
                disabled={savingApiKey || !modalApiKey.trim()}
                style={styles.modalSaveButton}
              >
                {savingApiKey ? 'Saving...' : 'Save & Switch'}
              </button>
            </div>
          </div>
        </div>
      )}

      {error && (
        <div style={styles.errorBanner}>
          {error}
          <button onClick={() => setError('')} style={styles.errorClose}>×</button>
        </div>
      )}
    </div>
  );
}

const styles: { [key: string]: React.CSSProperties } = {
  container: {
    height: '100dvh', // Dynamic viewport height for mobile
    overflow: 'hidden',
    backgroundColor: '#1a1a2e',
    color: '#eee',
    fontFamily: 'system-ui, sans-serif',
  },
  loginBox: {
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    minHeight: '100vh',
    gap: '16px',
  },
  title: {
    margin: 0,
    fontSize: '2rem',
  },
  subtitle: {
    margin: 0,
    color: '#aaa',
  },
  input: {
    padding: '12px 16px',
    fontSize: '1rem',
    borderRadius: '8px',
    border: '1px solid #444',
    backgroundColor: '#2a2a4e',
    color: '#eee',
    width: '280px',
  },
  button: {
    padding: '12px 32px',
    fontSize: '1rem',
    borderRadius: '8px',
    border: 'none',
    backgroundColor: '#4a4ae8',
    color: '#fff',
    cursor: 'pointer',
  },
  error: {
    color: '#ff6b6b',
    margin: 0,
  },
  muted: {
    color: '#888',
    margin: 0,
    fontSize: '0.9rem',
    position: 'relative',
  },
  statsIcon: {
    marginLeft: '8px',
    cursor: 'help',
    color: '#6b6bff',
    fontSize: '1rem',
    fontWeight: 'bold',
  },
  statsTooltip: {
    position: 'fixed',
    top: '100px',
    left: '20px',
    backgroundColor: '#2a2a4e',
    border: '1px solid #4a4ae8',
    borderRadius: '8px',
    padding: '12px',
    zIndex: 1001,
    minWidth: '320px',
    boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
  },
  statsSection: {
    marginBottom: '8px',
  },
  statsSectionTitle: {
    fontWeight: 'bold',
    color: '#6b6bff',
    marginBottom: '6px',
    fontSize: '0.85rem',
  },
  statsRow: {
    fontSize: '0.8rem',
    marginBottom: '4px',
    color: '#ddd',
  },
  statsSeparator: {
    height: '1px',
    backgroundColor: '#444',
    margin: '10px 0',
  },
  mainLayout: {
    display: 'flex',
    height: '100dvh', // Dynamic viewport height for mobile (falls back to 100vh)
    overflow: 'hidden',
  },
  sidebar: {
    width: '280px',
    backgroundColor: '#16162a',
    display: 'flex',
    flexDirection: 'column',
    borderRight: '1px solid #333',
    overflow: 'hidden',
  },
  sidebarMobile: {
    position: 'fixed' as const,
    top: 0,
    left: 0,
    height: '100vh',
    zIndex: 1000,
    transition: 'transform 0.3s ease',
  },
  sidebarHidden: {
    transform: 'translateX(-100%)',
  },
  sidebarOverlay: {
    position: 'fixed' as const,
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    backgroundColor: 'rgba(0, 0, 0, 0.5)',
    zIndex: 999,
  },
  hamburgerButton: {
    position: 'fixed' as const,
    top: '12px',
    left: '12px',
    zIndex: 998,
    backgroundColor: '#2a2a4e',
    border: 'none',
    borderRadius: '8px',
    padding: '8px 12px',
    fontSize: '1.5rem',
    color: '#eee',
    cursor: 'pointer',
  },
  closeSidebarButton: {
    background: 'none',
    border: 'none',
    fontSize: '1.2rem',
    color: '#888',
    cursor: 'pointer',
    marginLeft: '8px',
  },
  sidebarHeader: {
    padding: '20px',
    borderBottom: '1px solid #333',
  },
  sidebarHeaderRow: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  sidebarTitle: {
    margin: 0,
    fontSize: '1.2rem',
  },
  exitProjectButton: {
    background: 'none',
    border: 'none',
    cursor: 'pointer',
    color: '#888',
    marginLeft: 'auto',
    fontSize: '0.9rem',
  },
  logoutButton: {
    background: 'none',
    border: 'none',
    cursor: 'pointer',
    fontSize: '1.2rem',
    color: '#888',
  },
  editInput: {
    flex: 1,
    padding: '4px 8px',
    fontSize: '0.95rem',
    borderRadius: '4px',
    border: '1px solid #4a4ae8',
    backgroundColor: '#2a2a4e',
    color: '#eee',
    marginRight: '8px',
    minWidth: 0,
    overflow: 'visible',
  },
  statsBox: {
    padding: '16px',
    borderTop: '1px solid #333',
  },
  statsText: {
    margin: '4px 0',
    fontSize: '0.85rem',
    color: '#aaa',
  },
  chatArea: {
    flex: 1,
    display: 'flex',
    flexDirection: 'column',
    minHeight: 0, // Important for flex children to allow shrinking
    maxHeight: '100%',
    overflow: 'hidden',
  },
  chatHeader: {
    padding: '16px 20px',
    borderBottom: '1px solid #333',
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
    flexShrink: 0,
  },
  chatTitle: {
    margin: 0,
    fontSize: '1.1rem',
    flex: 1,
  },
  modelSelector: {
    background: '#2a2a4e',
    color: '#fff',
    border: '1px solid #4a4a6e',
    borderRadius: '6px',
    padding: '6px 10px',
    fontSize: '0.9rem',
    cursor: 'pointer',
    marginRight: '8px',
  },
  reloadButton: {
    background: 'none',
    border: 'none',
    cursor: 'pointer',
    fontSize: '1.2rem',
    padding: '4px 8px',
    borderRadius: '4px',
    transition: 'background-color 0.2s',
  } as React.CSSProperties & { ':hover'?: React.CSSProperties },
  messagesContainer: {
    flex: 1,
    overflowY: 'auto',
    padding: '20px',
    minHeight: 0, // Important for flex children to allow shrinking
  },
  message: {
    padding: '16px',
    borderRadius: '8px',
    marginBottom: '12px',
  },
  messageRole: {
    fontWeight: 'bold',
    marginBottom: '8px',
    fontSize: '0.85rem',
    color: '#aaa',
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
  },
  editMessageButton: {
    background: 'none',
    border: 'none',
    cursor: 'pointer',
    fontSize: '0.9rem',
    padding: '2px 6px',
    borderRadius: '4px',
    opacity: 1,  // Fully visible always
    transition: 'background-color 0.2s',
  },
  editMessageTextarea: {
    width: '100%',
    minHeight: '100px',
    padding: '12px',
    fontSize: '1rem',
    borderRadius: '8px',
    border: '1px solid #555',
    backgroundColor: '#1a1a2e',
    color: '#eee',
    resize: 'vertical',
    fontFamily: 'inherit',
    lineHeight: 1.5,
    marginBottom: '8px',
  } as React.CSSProperties,
  editMessageActions: {
    display: 'flex',
    gap: '8px',
    justifyContent: 'flex-end',
  },
  messageContent: {
    lineHeight: 1.5,
  },
  messageTokens: {
    fontSize: '0.75rem',
    color: '#666',
  },
  messageFooter: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginTop: '8px',
  },
  messageTimestamp: {
    fontSize: '0.75rem',
    color: '#666',
    marginLeft: 'auto',
  },
  branchNav: {
    display: 'flex',
    alignItems: 'center',
    gap: '4px',
    fontSize: '0.75rem',
    color: '#888',
  },
  branchNavButton: {
    background: 'none',
    border: 'none',
    color: '#888',
    cursor: 'pointer',
    padding: '2px 4px',
    fontSize: '0.7rem',
    borderRadius: '3px',
    transition: 'background-color 0.2s, color 0.2s',
  },
  branchNavText: {
    minWidth: '30px',
    textAlign: 'center' as const,
  },
  reasoningContainer: {
    backgroundColor: '#252540',
    borderRadius: '6px',
    marginBottom: '12px',
    cursor: 'pointer',
    border: '1px solid #3a3a5e',
  },
  reasoningHeader: {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
    padding: '8px 12px',
    fontSize: '0.85rem',
    color: '#888',
  },
  reasoningLabel: {
    fontStyle: 'italic',
  },
  reasoningContent: {
    padding: '0 12px 12px 12px',
    fontSize: '0.9rem',
    color: '#aaa',
    lineHeight: 1.5,
    whiteSpace: 'pre-wrap' as const,
    borderTop: '1px solid #3a3a5e',
    paddingTop: '12px',
  },
  inputArea: {
    padding: '16px',
    borderTop: '1px solid #333',
    display: 'flex',
    flexDirection: 'column' as const,
    gap: '8px',
    flexShrink: 0, // Prevent input area from shrinking
    backgroundColor: '#1a1a2e', // Ensure background is visible
  },
  messageInput: {
    width: '100%',
    padding: '12px 30px 12px 12px',
    fontSize: '1rem',
    borderRadius: '8px',
    border: '1px solid #444',
    backgroundColor: '#2a2a4e',
    color: '#eee',
    resize: 'none',
    fontFamily: 'inherit',
    boxSizing: 'border-box',
    lineHeight: '1.5',
  },
  sendButton: {
    padding: '12px 24px',
    fontSize: '1rem',
    borderRadius: '8px',
    border: 'none',
    backgroundColor: '#4a4ae8',
    color: '#fff',
    cursor: 'pointer',
    alignSelf: 'flex-end',
  },
  buttonColumn: {
    display: 'flex',
    flexDirection: 'column' as const,
    gap: '8px',
    alignSelf: 'flex-end',
  },
  updateButton: {
    padding: '8px 16px',
    fontSize: '0.85rem',
    borderRadius: '8px',
    border: '1px solid #444',
    color: '#ccc',
    cursor: 'pointer',
  },
  modalOverlay: {
    position: 'fixed' as const,
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    backgroundColor: 'rgba(0, 0, 0, 0.7)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    zIndex: 2000,
  },
  modal: {
    backgroundColor: '#1e1e3a',
    borderRadius: '12px',
    padding: '24px',
    width: '90%',
    maxWidth: '600px',
    maxHeight: '80vh',
    display: 'flex',
    flexDirection: 'column' as const,
    border: '1px solid #4a4ae8',
  },
  modalTitle: {
    margin: '0 0 8px 0',
    fontSize: '1.3rem',
    color: '#eee',
  },
  modalDescription: {
    margin: '0 0 16px 0',
    fontSize: '0.9rem',
    color: '#888',
  },
  updatesTextarea: {
    width: '100%',
    minHeight: '200px',
    padding: '12px',
    fontSize: '1rem',
    borderRadius: '8px',
    border: '1px solid #444',
    backgroundColor: '#2a2a4e',
    color: '#eee',
    resize: 'vertical',
    fontFamily: 'inherit',
    lineHeight: 1.5,
    marginBottom: '16px',
    boxSizing: 'border-box',
  } as React.CSSProperties,
  modalFooter: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  tokenCount: {
    fontSize: '0.85rem',
  },
  modalActions: {
    display: 'flex',
    gap: '12px',
    justifyContent: 'flex-end',
  },
  modalCancelButton: {
    padding: '10px 20px',
    fontSize: '1rem',
    borderRadius: '8px',
    border: '1px solid #444',
    backgroundColor: 'transparent',
    color: '#ccc',
    cursor: 'pointer',
  },
  modalSaveButton: {
    padding: '10px 20px',
    fontSize: '1rem',
    borderRadius: '8px',
    border: 'none',
    backgroundColor: '#4a4ae8',
    color: '#fff',
    cursor: 'pointer',
  },
  resizeHandle: {
    position: 'absolute',
    top: '0px',
    right: '0px',
    width: '24px',
    height: '24px',
    cursor: 'ns-resize',
    color: '#666',
    fontSize: '12px',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    userSelect: 'none',
    zIndex: 10,
    fontWeight: 'bold',
    letterSpacing: '-2px',
    backgroundColor: '#2a2a4e',
    borderTopRightRadius: '8px',
  },
  noChat: {
    flex: 1,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    color: '#666',
  },
  errorBanner: {
    position: 'fixed',
    bottom: '20px',
    left: '50%',
    transform: 'translateX(-50%)',
    backgroundColor: '#ff6b6b',
    color: '#fff',
    padding: '12px 20px',
    borderRadius: '8px',
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
  },
  errorClose: {
    background: 'none',
    border: 'none',
    color: '#fff',
    fontSize: '1.2rem',
    cursor: 'pointer',
  },
  chatName: {
    flex: 1,
    cursor: 'pointer',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  chatActions: {
    display: 'flex',
    gap: '4px',
  },
  iconButton: {
    background: 'none',
    border: 'none',
    cursor: 'pointer',
    padding: '4px',
    fontSize: '0.9rem',
    opacity: 0.6,
  },
  iconButtonCheck: {
    background: 'none',
    border: 'none',
    cursor: 'pointer',
    padding: '4px',
    fontSize: '0.9rem',
    color: '#4ade80',
  },
  iconButtonX: {
    background: 'none',
    border: 'none',
    cursor: 'pointer',
    padding: '4px',
    fontSize: '0.9rem',
    color: 'hsl(0, 98%, 75%)',
  },
  section: {
    borderBottom: '1px solid #333',
  },
  sectionHeader: {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
    padding: '12px 16px',
    cursor: 'pointer',
    fontWeight: 'bold',
    fontSize: '0.9rem',
    color: '#aaa',
  },
  sectionHeaderClickable: {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
    cursor: 'pointer',
  },
  addButton: {
    background: 'none',
    border: 'none',
    cursor: 'pointer',
    fontSize: '1.2rem',
    color: '#4a4ae8',
    marginLeft: 'auto',
    padding: '0 4px',
  },
  expandIcon: {
    fontSize: '0.7rem',
  },
  sectionList: {
    padding: '0 8px 8px 8px',
  },
  listItem: {
    padding: '10px 12px',
    borderRadius: '6px',
    cursor: 'pointer',
    marginBottom: '2px',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  mutedSmall: {
    color: '#666',
    fontSize: '0.85rem',
    margin: '8px 12px',
  },
  seeMoreLink: {
    padding: '8px 12px',
    color: '#4a4ae8',
    fontSize: '0.9rem',
    cursor: 'pointer',
    textAlign: 'center' as const,
    marginTop: '4px',
  },
  listViewHeader: {
    padding: '20px',
    borderBottom: '1px solid #333',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: '16px',
  },
  listViewTitle: {
    margin: 0,
    fontSize: '1.5rem',
    flex: 1,
    textAlign: 'center' as const,
  },
  backButton: {
    padding: '8px 16px',
    fontSize: '1rem',
    borderRadius: '6px',
    border: 'none',
    backgroundColor: '#2a2a4e',
    color: '#eee',
    cursor: 'pointer',
  },
  listViewAddButton: {
    padding: '8px 16px',
    fontSize: '1rem',
    borderRadius: '6px',
    border: 'none',
    backgroundColor: '#4a4ae8',
    color: '#fff',
    cursor: 'pointer',
  },
  listViewContent: {
    flex: 1,
    overflowY: 'auto' as const,
    padding: '20px',
  },
  listViewItem: {
    padding: '16px 20px',
    borderRadius: '8px',
    marginBottom: '8px',
    cursor: 'pointer',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    border: '1px solid #333',
  },
  listViewItemName: {
    flex: 1,
    fontSize: '1.1rem',
    cursor: 'pointer',
  },
  listViewEmpty: {
    textAlign: 'center' as const,
    marginTop: '60px',
    color: '#666',
  },
  loadingMore: {
    textAlign: 'center' as const,
    padding: '12px',
    color: '#888',
    fontSize: '0.9rem',
    fontStyle: 'italic',
  },
  loadingMoreMessages: {
    textAlign: 'center' as const,
    padding: '12px',
    color: '#888',
    fontSize: '0.9rem',
    fontStyle: 'italic',
    backgroundColor: '#1a1a2e',
  },
  
  // Project Landing Page Styles
  projectLanding: {
    display: 'flex',
    flexDirection: 'column' as const,
    height: '100%',
    overflow: 'hidden',
  },
  projectLandingHeader: {
    padding: '20px',
    borderBottom: '1px solid #333',
    flexShrink: 0,
  },
  projectLandingTitle: {
    margin: 0,
    fontSize: '1.5rem',
    color: '#eee',
  },
  projectLandingContent: {
    display: 'flex',
    flex: 1,
    overflow: 'hidden',
    gap: '1px',
    backgroundColor: '#333',
  },
  projectChatsColumn: {
    flex: 1,
    display: 'flex',
    flexDirection: 'column' as const,
    backgroundColor: '#1a1a2e',
    overflow: 'hidden',
    minWidth: 0,
  },
  projectConfigColumn: {
    width: '380px',
    flexShrink: 0,
    display: 'flex',
    flexDirection: 'column' as const,
    backgroundColor: '#1a1a2e',
    overflow: 'auto',
  },
  projectSection: {
    padding: '16px',
    borderBottom: '1px solid #333',
  },
  projectSectionHeader: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: '12px',
  },
  projectSectionTitle: {
    margin: 0,
    fontSize: '1rem',
    color: '#aaa',
    fontWeight: 600,
  },
  projectAddButton: {
    padding: '6px 12px',
    fontSize: '0.85rem',
    borderRadius: '6px',
    border: 'none',
    backgroundColor: '#4a4ae8',
    color: '#fff',
    cursor: 'pointer',
  },
  chatSearchInput: {
    margin: '0 16px 12px 16px',
    padding: '10px 12px',
    fontSize: '0.95rem',
    borderRadius: '8px',
    border: '1px solid #444',
    backgroundColor: '#2a2a4e',
    color: '#eee',
    outline: 'none',
  },
  projectChatsList: {
    flex: 1,
    overflow: 'auto',
    padding: '0 16px 16px 16px',
  },
  chatGroupHeader: {
    fontSize: '0.8rem',
    fontWeight: 600,
    color: '#666',
    textTransform: 'uppercase' as const,
    letterSpacing: '0.5px',
    marginTop: '16px',
    marginBottom: '8px',
  },
  chatCard: {
    padding: '12px 14px',
    marginBottom: '8px',
    borderRadius: '8px',
    backgroundColor: '#252542',
    border: '1px solid #333',
    cursor: 'pointer',
    transition: 'background-color 0.15s',
  },
  chatCardName: {
    fontSize: '1rem',
    fontWeight: 500,
    color: '#eee',
    marginBottom: '4px',
  },
  chatCardPreview: {
    fontSize: '0.85rem',
    color: '#888',
    marginBottom: '8px',
    lineHeight: 1.4,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap' as const,
  },
  chatCardMeta: {
    display: 'flex',
    gap: '12px',
    fontSize: '0.75rem',
    color: '#666',
  },
  tokenBadge: {
    fontSize: '0.8rem',
    color: '#888',
    backgroundColor: '#2a2a4e',
    padding: '2px 8px',
    borderRadius: '4px',
  },
  instructionsPreview: {
    fontSize: '0.9rem',
    color: '#aaa',
    lineHeight: 1.5,
    marginBottom: '12px',
    whiteSpace: 'pre-wrap' as const,
    wordBreak: 'break-word' as const,
  },
  editInstructionsButton: {
    padding: '8px 16px',
    fontSize: '0.9rem',
    borderRadius: '6px',
    border: '1px solid #4a4ae8',
    backgroundColor: 'transparent',
    color: '#4a4ae8',
    cursor: 'pointer',
    width: '100%',
  },
  projectModelSelector: {
    padding: '8px 12px',
    backgroundColor: '#2a2a4e',
    border: '1px solid #3a3a5e',
    borderRadius: '4px',
    color: '#e0e0e0',
    fontSize: '14px',
    width: '100%',
    marginBottom: '8px',
    cursor: 'pointer',
  },
  projectModelHelperText: {
    fontSize: '0.8rem',
    color: '#888',
    margin: 0,
  },
  filesList: {
    marginBottom: '12px',
  },
  fileItem: {
    display: 'flex',
    alignItems: 'center',
    padding: '8px 0',
    borderBottom: '1px solid #333',
    gap: '8px',
  },
  fileName: {
    flex: 1,
    fontSize: '0.9rem',
    color: '#ccc',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap' as const,
  },
  filenameTooltip: {
    position: 'fixed' as const,
    backgroundColor: '#2a2a4e',
    border: '1px solid #4a4ae8',
    borderRadius: '6px',
    padding: '6px 10px',
    zIndex: 1001,
    fontSize: '0.85rem',
    color: '#eee',
    boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
    maxWidth: '400px',
    wordBreak: 'break-all' as const,
  },
  fileTokens: {
    fontSize: '0.8rem',
    color: '#888',
    flexShrink: 0,
  },
  fileDeleteButton: {
    background: 'none',
    border: 'none',
    cursor: 'pointer',
    padding: '4px',
    fontSize: '0.85rem',
    opacity: 0.6,
    flexShrink: 0,
  },
  fileCheckbox: {
    width: '16px',
    height: '16px',
    cursor: 'pointer',
    flexShrink: 0,
    accentColor: '#4a4ae8',
  },
  uploadButton: {
    padding: '8px 16px',
    fontSize: '0.9rem',
    borderRadius: '6px',
    border: '1px solid #444',
    backgroundColor: '#2a2a4e',
    color: '#ccc',
    cursor: 'pointer',
    width: '100%',
  },

  // Chat file attachment styles
  inputRow: {
    display: 'flex',
    gap: '8px',
    alignItems: 'flex-end',
    width: '100%',
  },
  attachButtonContainer: {
    position: 'relative' as const,
    flexShrink: 0,
  },
  attachButton: {
    width: '40px',
    height: '40px',
    borderRadius: '8px',
    border: '1px solid #444',
    backgroundColor: '#2a2a4e',
    color: '#ccc',
    fontSize: '1.5rem',
    cursor: 'pointer',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
  },
  attachMenu: {
    position: 'absolute' as const,
    bottom: '48px',
    left: 0,
    backgroundColor: '#2a2a4e',
    border: '1px solid #444',
    borderRadius: '8px',
    padding: '4px',
    minWidth: '140px',
    zIndex: 100,
    boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
  },
  attachMenuItem: {
    display: 'block',
    width: '100%',
    padding: '10px 12px',
    border: 'none',
    backgroundColor: 'transparent',
    color: '#ccc',
    fontSize: '0.9rem',
    textAlign: 'left' as const,
    cursor: 'pointer',
    borderRadius: '6px',
  },
  stagedFilesBar: {
    display: 'flex',
    flexWrap: 'wrap' as const,
    gap: '8px',
    padding: '8px',
    backgroundColor: '#252542',
    borderRadius: '8px',
    width: '100%',
    boxSizing: 'border-box' as const,
  },
  stagedFileChip: {
    display: 'flex',
    alignItems: 'center',
    gap: '6px',
    padding: '4px 8px',
    backgroundColor: '#3a3a5e',
    borderRadius: '6px',
    fontSize: '0.85rem',
  },
  stagedFileName: {
    color: '#ccc',
  },
  stagedFileRemove: {
    background: 'none',
    border: 'none',
    color: '#888',
    cursor: 'pointer',
    padding: '2px',
    fontSize: '0.8rem',
    lineHeight: 1,
  },
  attachedFilesDisplay: {
    marginBottom: '8px',
    padding: '6px 10px',
    backgroundColor: 'rgba(74, 74, 232, 0.15)',
    borderRadius: '6px',
    fontSize: '0.85rem',
  },
  attachedFilesSingle: {
    color: '#aaa',
  },
  attachedFilesDetails: {
    cursor: 'pointer',
  },
  attachedFilesSummary: {
    color: '#aaa',
    outline: 'none',
  },
  attachedFilesExpanded: {
    marginTop: '6px',
    paddingLeft: '8px',
  },
  attachedFileItem: {
    color: '#888',
    fontSize: '0.8rem',
    padding: '2px 0',
  },
  editModeAttachedFiles: {
    marginBottom: '8px',
    padding: '8px 10px',
    backgroundColor: 'rgba(74, 74, 232, 0.1)',
    borderRadius: '6px',
    fontSize: '0.85rem',
    display: 'flex',
    flexWrap: 'wrap' as const,
    gap: '8px',
    alignItems: 'center',
  },
  editModeFilesLabel: {
    color: '#888',
    marginRight: '4px',
  },
  editModeFileName: {
    color: '#aaa',
    backgroundColor: 'rgba(74, 74, 232, 0.2)',
    padding: '2px 8px',
    borderRadius: '4px',
  },
};

export default App;