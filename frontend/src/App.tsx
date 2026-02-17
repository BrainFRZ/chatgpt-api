import React, { useState, useEffect, useRef, useCallback } from 'react';
import { styles } from './styles';
import {
  LoginResponse, ChatMessage, ChatStats, UserStats, ModelInfo,
  ApiKeysStatus, FreeTokens, ProjectFileInfo, ProjectFilesResponse,
  ProjectInstructions, ChatCardInfo
} from './types';
import { useMessaging } from './hooks/useMessaging';
import { useSync } from './hooks/useSync';
import Sidebar from './components/Sidebar';
import ChatView from './components/ChatView';
import CharacterPanel from './components/CharacterPanel';
import ProjectLanding from './components/ProjectLanding';
import Modals from './components/Modals';

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
        @keyframes pulse {
          0%, 100% { box-shadow: 0 0 0 0 rgba(74, 74, 232, 0.4); }
          50% { box-shadow: 0 0 0 4px rgba(74, 74, 232, 0); }
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
  const [docsRefreshed, setDocsRefreshed] = useState(false);
  const [availableModels, setAvailableModels] = useState<ModelInfo[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>('gpt-5.2');
  const [anthropicSync, setAnthropicSync] = useState<boolean>(true);
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
  const [updatesText, setUpdatesText] = useState('');
  const [updatesTokenCount, setUpdatesTokenCount] = useState(0);
  const [showUpdatesModal, setShowUpdatesModal] = useState(false);
  const [draftUpdatesText, setDraftUpdatesText] = useState('');
  const [updatesLoading, setUpdatesLoading] = useState(false);
  const [expandedReasoning, setExpandedReasoning] = useState<Set<number>>(new Set());
  const tokenCountTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const [isResizing, setIsResizing] = useState(false);
  const [resizeStartY, setResizeStartY] = useState(0);
  const [resizeStartHeight, setResizeStartHeight] = useState(0);
  const [isLoading, setIsLoading] = useState<Set<string>>(new Set());
  const isLoadingRef = useRef<Set<string>>(new Set());
  // Keep ref in sync with state for use in useCallback with [] deps
  useEffect(() => { isLoadingRef.current = isLoading; }, [isLoading]);
  const [pipelineStage, setPipelineStage] = useState<Map<string, {stage: string, status: string}>>(new Map());
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
  const [agentInstructions, setAgentInstructions] = useState<Record<string, {instructions: string, tokens: number}>>({});
  const [activeInstructionsTab, setActiveInstructionsTab] = useState('events');
  const [editingAgentInstructions, setEditingAgentInstructions] = useState<Record<string, string>>({});
  const [filesUploading, setFilesUploading] = useState(false);
  const [projectChatsDetailed, setProjectChatsDetailed] = useState<ChatCardInfo[]>([]);
  const [chatSearchQuery, setChatSearchQuery] = useState('');
  const [hoveredFilename, setHoveredFilename] = useState<string | null>(null);
  const [filenameTooltipPos, setFilenameTooltipPos] = useState<{x: number, y: number}>({x: 0, y: 0});
  const filenameTooltipTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Ref for attach menu (needed by ChatView)
  const attachMenuRef = useRef<HTMLDivElement>(null);

  // Right panel — character state
  const [rightPanelOpen, setRightPanelOpen] = useState(true);
  const [pipelineState, setPipelineState] = useState<any>(null);
  const [chatGameSystem, setChatGameSystem] = useState<string | null>(null);
  const [selectedCharacter, setSelectedCharacter] = useState<string | null>(null);
  const [showCharacterSheet, setShowCharacterSheet] = useState(false);
  const [showAllCharactersModal, setShowAllCharactersModal] = useState(false);
  const [showNpcMemories, setShowNpcMemories] = useState<string | null>(null);
  const [characterSheetMd, setCharacterSheetMd] = useState<string>('');
  const [mobileBottomSheetOpen, setMobileBottomSheetOpen] = useState(false);

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

  // Keyboard shortcuts: Ctrl+\ to toggle left sidebar, Ctrl+] to toggle right panel
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.key === '\\') {
        e.preventDefault();
        setSidebarOpen(prev => !prev);
      }
      if (e.ctrlKey && e.key === ']') {
        e.preventDefault();
        setRightPanelOpen(prev => !prev);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  const [projectsExpanded, setProjectsExpanded] = useState(true);
  const [chatsExpanded, setChatsExpanded] = useState(true);
  const [projects, setProjects] = useState<string[]>([]);
  const [viewMode, setViewMode] = useState<'chat' | 'projectList' | 'chatList'>('chat');
  const [projectChatsCache, setProjectChatsCache] = useState<{[key: string]: string[]}>({});
  const [rootChatsCache, setRootChatsCache] = useState<string[] | null>(null);
  const [projectModel, setProjectModel] = useState<string | null>(null);
  const isPipelineProject = projectModel === 'gpt-5.2';
  const [projectGameSystem, setProjectGameSystem] = useState<string | null>(null);
  const [availableGameSystems, setAvailableGameSystems] = useState<{id: string, name: string}[]>([]);

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
    setUpdatesText('');
    setDraftUpdatesText('');
    setUpdatesTokenCount(0);
    setShowUpdatesModal(false);
    setUpdatesLoading(false);
    setExpandedReasoning(new Set());
    setEditingMessageIndex(null);
    setEditingMessageContent('');
    setIsResizing(false);
    setPipelineState(null);
    setChatGameSystem(null);
    setSelectedCharacter(null);
    setShowCharacterSheet(false);
    setShowAllCharactersModal(false);
    setShowNpcMemories(null);
    setMobileBottomSheetOpen(false);

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
    setProjectGameSystem(null);
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

  // ============================================================================
  // BUSINESS LOGIC (auth, CRUD, file management, model switching, etc.)
  // ============================================================================

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

    // Fetch available game systems
    fetch('/api/game-systems')
      .then(res => res.json())
      .then(data => setAvailableGameSystems(data))
      .catch(() => {
        setAvailableGameSystems([
          { id: 'dnd5e', name: 'D&D 5E' },
          { id: 'dnd5e_cyber', name: 'D&D 5E (Cyberpunk)' },
          { id: 'coc7e', name: 'Call of Cthulhu 7E' },
          { id: 'sr6e', name: 'Shadowrun 6E' },
          { id: 'cpred', name: 'Cyberpunk RED' }
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
      setProjectGameSystem(null);
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
    let fetchedModel = 'gpt-5.2';
    try {
      const metadataResponse = await fetch(`/api/project-metadata/${user.username}/${projectName}`);
      if (currentProjectRef.current !== projectName) return;

      if (metadataResponse.ok) {
        const metadataData = await metadataResponse.json();
        if (currentProjectRef.current !== projectName) return;
        fetchedModel = metadataData.model || 'gpt-5.2';
        setProjectModel(fetchedModel);
        setProjectGameSystem(metadataData.game_system || 'dnd5e');
      }
    } catch (err) {
      console.error('Could not fetch project metadata:', err);
    }

    // Fetch project files and instructions (refresh in background)
    fetchProjectFiles(projectName);
    fetchProjectInstructions(projectName);
    // Fetch per-agent instructions for pipeline projects
    if (fetchedModel === 'gpt-5.2') {
      fetchAgentInstructions(projectName);
    }
    fetchProjectChatsDetailed(projectName);

    // Fetch character sheet for right panel
    fetch(`/api/character-sheet/${user.username}/${projectName}`)
      .then(r => r.ok ? r.json() : { content: '' })
      .then(d => { if (currentProjectRef.current === projectName) setCharacterSheetMd(d.content || ''); })
      .catch(() => setCharacterSheetMd(''));

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

    // Pass explicit model only if project has one set; otherwise let backend pick
    // based on user's available API keys
    const modelForNewChat = projectForNewChat ? (projectModel || undefined) : undefined;

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
        // Consider API key set if at least one key is configured
        if (data.has_openai || data.has_anthropic) {
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

  const handleAnthropicSyncToggle = async () => {
    if (!user || !currentChat) return;
    const newValue = !anthropicSync;
    setAnthropicSync(newValue);
    try {
      const response = await fetch('/api/set-anthropic-sync', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: user.username, chat_name: currentChat, project: currentProject, sync: newValue })
      });
      if (!response.ok) {
        setAnthropicSync(!newValue);
        console.error('Could not save anthropic sync preference:', response.status);
      }
    } catch (err) {
      setAnthropicSync(!newValue);
      console.error('Could not save anthropic sync preference:', err);
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
        // Fetch agent instructions if switching to pipeline model
        if (newModel === 'gpt-5.2') {
          fetchAgentInstructions(currentProject);
        }
      }
    } catch (err) {
      // Rollback on network error
      setProjectModel(previousModel);
      setError('Could not switch project model - network error');
      console.error('Could not save project model preference:', err);
    }
  };

  const handleProjectGameSystemChange = async (newGameSystem: string) => {
    if (!user || !currentProject) return;

    const previousGameSystem = projectGameSystem;
    setProjectGameSystem(newGameSystem);  // Optimistic update
    setChatGameSystem(newGameSystem);  // Sync right panel game-specific rendering

    try {
      const response = await fetch('/api/set-project-game-system', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: user.username,
          project: currentProject,
          game_system: newGameSystem
        })
      });

      if (!response.ok) {
        setProjectGameSystem(previousGameSystem);
        setChatGameSystem(previousGameSystem);
        const data = await response.json().catch(() => ({}));
        setError(data.detail || 'Could not switch game system');
      }
    } catch (err) {
      setProjectGameSystem(previousGameSystem);
      setChatGameSystem(previousGameSystem);
      setError('Could not switch game system - network error');
      console.error('Could not save game system preference:', err);
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

  const fetchAgentInstructions = async (projectName: string) => {
    if (!user) return;

    try {
      const response = await fetch(`/api/project-instructions/${user.username}/${projectName}/agents`);
      if (currentProjectRef.current !== projectName) return;

      if (response.ok) {
        const data = await response.json();
        if (currentProjectRef.current !== projectName) return;
        setAgentInstructions(data);
      }
    } catch (err) {
      console.error('Could not fetch agent instructions:', err);
    }
  };

  const updateAgentInstructions = async (agentName: string) => {
    const ctx = createContextGuard();
    if (!user || !ctx.project) return;

    const instructionsToSave = editingAgentInstructions[agentName] || '';

    try {
      const response = await fetch(`/api/project-instructions/${user.username}/${ctx.project}/agents/${agentName}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instructions: instructionsToSave })
      });

      if (ctx.isProjectStale()) return;

      if (response.ok) {
        const data = await response.json();
        setAgentInstructions(prev => ({
          ...prev,
          [agentName]: { instructions: instructionsToSave, tokens: data.tokens }
        }));
      }
    } catch (err) {
      console.error(`Could not save ${agentName} instructions:`, err);
    }
  };

  const handleSaveAllAgentInstructions = async () => {
    setInstructionsSaving(true);
    try {
      await Promise.all(
        ['events', 'mechanics', 'narration'].map(agent => updateAgentInstructions(agent))
      );
      setShowInstructionsModal(false);
    } catch (err) {
      setError('Could not save agent instructions');
    } finally {
      setInstructionsSaving(false);
    }
  };

  const handleToggleFileAgent = async (filename: string, agentName: string) => {
    const ctx = createContextGuard();
    if (!user || !ctx.project) return;

    const file = projectFiles.find(f => f.filename === filename);
    if (!file) return;

    const currentAgents = file.agents || ['events', 'mechanics', 'narration'];
    let newAgents: string[];

    if (currentAgents.includes(agentName)) {
      // Prevent removing the last agent
      if (currentAgents.length <= 1) return;
      newAgents = currentAgents.filter(a => a !== agentName);
    } else {
      newAgents = [...currentAgents, agentName];
    }

    // Optimistic update
    setProjectFiles(prev => prev.map(f =>
      f.filename === filename ? { ...f, agents: newAgents } : f
    ));

    try {
      const response = await fetch(`/api/project-files/${user.username}/${ctx.project}/agents/${encodeURIComponent(filename)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agents: newAgents })
      });

      if (ctx.isProjectStale()) return;

      if (!response.ok) {
        // Revert on failure
        setProjectFiles(prev => prev.map(f =>
          f.filename === filename ? { ...f, agents: currentAgents } : f
        ));
        const data = await response.json();
        setError(data.detail || 'Failed to update file agents');
      }
    } catch (err) {
      // Revert on error
      setProjectFiles(prev => prev.map(f =>
        f.filename === filename ? { ...f, agents: currentAgents } : f
      ));
      setError('Could not update file agents');
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
          messageCount: chat.message_count || 0
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
        // Reset ref since we failed to open
        currentChatRef.current = null;
        // For 404 (chat not found/deleted), clear localStorage so we don't keep retrying on refresh
        if (response.status === 404) {
          localStorage.removeItem('chatgpt-current-chat');
        }
        return;
      }

      const data = await response.json();

      // Check again after parsing
      if (currentChatRef.current !== chatName) return;

      // Set new chat state (ref already set above)
      setCurrentChat(chatName);
      messaging.setStagedFiles([]);
      messaging.setShowAttachMenu(false);
      setEditingMessageIndex(null);
      setEditingMessageContent('');
      messaging.setNewMessage('');
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
      setAnthropicSync(data.anthropic_sync !== false);

      // Load pipeline state and game system for right panel
      setPipelineState(data.pipeline_state || null);
      setChatGameSystem(data.game_system || null);

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
          updates: draftUpdatesText,
          project: ctx.project
        })
      });

      if (ctx.isChatStale()) return;

      if (response.ok) {
        setUpdatesText(draftUpdatesText);
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
    setDraftUpdatesText(text);

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

  const startEditMessage = (index: number) => {
    if (index < 0 || index >= messages.length) return;
    setEditingMessageIndex(index);
    setEditingMessageContent(messages[index].content);
  };

  const cancelEditMessage = () => {
    setEditingMessageIndex(null);
    setEditingMessageContent('');
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

  // ============================================================================
  // HOOK CALLS
  // ============================================================================

  const messaging = useMessaging({
    user, currentChat, currentProject,
    currentChatRef, currentProjectRef,
    messages, setMessages, allMessages, setAllMessages,
    currentLeafId, setCurrentLeafId,
    totalMessages, setTotalMessages, setHasMoreMessages, setMessageOffset,
    selectedModel, contextStartIndex, setContextStartIndex,
    stats, setStats,
    isLoading, setIsLoading,
    setPipelineStage, setPipelineState, setDocsRefreshed, setError,
    editingMessageIndex, editingMessageContent,
    setEditingMessageIndex, setEditingMessageContent,
    fetchUserStats, fetchFreeTokens,
  });

  const sync = useSync({
    user, currentChat, currentProject,
    currentChatRef, currentProjectRef,
    isLoadingRef,
    setMessages, setAllMessages, setTotalMessages,
    setCurrentLeafId, setStats, setContextStartIndex,
    setPipelineStage, setPipelineState,
    setSelectedModel, setAnthropicSync, setDocsRefreshed,
    setChats, setProjectChatsCache, setRootChatsCache,
    resetChatState,
  });

  // Close attach menu when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (messaging.showAttachMenu && attachMenuRef.current && !attachMenuRef.current.contains(event.target as Node)) {
        messaging.setShowAttachMenu(false);
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [messaging.showAttachMenu]);

  // Handle sync-triggered reload (when another client switches branches)
  // Just reload messages, don't rebuild system prompt
  useEffect(() => {
    if (sync.needsSyncReload && currentChat) {
      sync.setNeedsSyncReload(false);
      openChat(currentChat, currentProject);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sync.needsSyncReload]);

  // Textarea resize handlers
  const handleResizeStart = (e: React.MouseEvent) => {
    e.preventDefault();
    setIsResizing(true);
    setResizeStartY(e.clientY);
    setResizeStartHeight(messaging.textareaHeight);
  };

  const handleResizeMove = (e: MouseEvent) => {
    if (!isResizing) return;

    // Calculate new height (inverted: dragging up increases height)
    const deltaY = resizeStartY - e.clientY;
    const newHeight = Math.min(400, Math.max(44, resizeStartHeight + deltaY));
    messaging.setTextareaHeight(newHeight);
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

  // ============================================================================
  // RENDER
  // ============================================================================

  // Login screen
  if (!user) {
    return (
      <div style={styles.container}>
        <div style={styles.loginBox}>
          <h1 style={styles.title}>Chorus AI</h1>
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
          <p style={styles.subtitle}>Enter your API keys (at least one required):</p>

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
              Anthropic API Key {apiKeysStatus.has_anthropic && <span style={{ color: '#4ade80' }}>(configured)</span>}
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

  // Notes click handler - fetches latest notes from backend before opening modal
  const handleNotesClick = async () => {
    try {
      const url = currentProject
        ? `/api/updates/${user?.username}/${currentChat}?project=${currentProject}`
        : `/api/updates/${user?.username}/${currentChat}`;
      const resp = await fetch(url);
      if (resp.ok) {
        const data = await resp.json();
        const latest = data.updates || '';
        setUpdatesText(latest);
        setDraftUpdatesText(latest);
      } else {
        setDraftUpdatesText(updatesText);
      }
    } catch {
      setDraftUpdatesText(updatesText);
    }
    setShowUpdatesModal(true);
  };

  // Main chat interface
  return (
    <div style={styles.container}>
      <div style={styles.mainLayout}>
        {/* Sidebar (includes hamburger, overlay, collapsed strip) */}
        <Sidebar
          isMobile={isMobile} sidebarOpen={sidebarOpen} setSidebarOpen={setSidebarOpen}
          user={user!} handleLogout={handleLogout}
          showStatsTooltip={showStatsTooltip} setShowStatsTooltip={setShowStatsTooltip}
          userStats={userStats} freeTokens={freeTokens}
          projectsExpanded={projectsExpanded} setProjectsExpanded={setProjectsExpanded}
          projects={projects} currentProject={currentProject}
          editingProject={editingProject} editingProjectName={editingProjectName}
          setEditingProjectName={setEditingProjectName}
          startRenameProject={startRenameProject} saveRenameProject={saveRenameProject}
          cancelRenameProject={cancelRenameProject} handleDeleteProject={handleDeleteProject}
          enterProject={enterProject}
          creatingProject={creatingProject} startCreateProject={startCreateProject}
          newItemName={newItemName} setNewItemName={setNewItemName}
          saveNewProject={saveNewProject} cancelCreate={cancelCreate}
          setViewMode={setViewMode}
          chatsExpanded={chatsExpanded} setChatsExpanded={setChatsExpanded}
          chats={chats} currentChat={currentChat}
          editingChat={editingChat} editingName={editingName} setEditingName={setEditingName}
          startRenameChat={startRenameChat} saveRename={saveRename} cancelRename={cancelRename}
          handleDeleteChat={handleDeleteChat} openChat={openChat}
          creatingChat={creatingChat} startCreateChat={startCreateChat}
          saveNewChat={saveNewChat} exitProject={exitProject}
          stats={stats}
        />

        {/* Main content area - shows chat OR list view */}
        <div style={{
          ...styles.chatArea,
          ...(isMobile ? {
            position: 'absolute' as const,
            top: 0,
            left: 0,
            right: 0,
            bottom: '80px', // Leave room for fixed input area
          } : {})
        }}>
          {viewMode === 'chat' ? (
            // Normal chat interface
            currentChat ? (
              <ChatView
                isMobile={isMobile}
                currentChat={currentChat}
                currentProject={currentProject}
                viewerCount={sync.viewerCount}
                projectGameSystem={projectGameSystem}
                availableGameSystems={availableGameSystems}
                handleProjectGameSystemChange={handleProjectGameSystemChange}
                availableModels={availableModels}
                selectedModel={selectedModel}
                handleModelChange={handleModelChange}
                anthropicSync={anthropicSync}
                handleAnthropicSyncToggle={handleAnthropicSyncToggle}
                handleReloadChat={handleReloadChat}
                messagesContainerRef={messagesContainerRef}
                messagesEndRef={messagesEndRef}
                isLoadingMoreMessages={isLoadingMoreMessages}
                messages={messages}
                allMessages={allMessages}
                totalMessages={totalMessages}
                contextStartIndex={contextStartIndex}
                editingMessageIndex={editingMessageIndex}
                editingMessageContent={editingMessageContent}
                setEditingMessageContent={setEditingMessageContent}
                startEditMessage={startEditMessage}
                saveEditedMessage={messaging.saveEditedMessage}
                cancelEditMessage={cancelEditMessage}
                expandedReasoning={expandedReasoning}
                setExpandedReasoning={setExpandedReasoning}
                getSiblings={getSiblings}
                switchBranch={switchBranch}
                isLoading={isLoading}
                pipelineStage={pipelineStage}
                stagedFiles={messaging.stagedFiles}
                removeStagedFile={messaging.removeStagedFile}
                showAttachMenu={messaging.showAttachMenu}
                setShowAttachMenu={messaging.setShowAttachMenu}
                attachMenuRef={attachMenuRef}
                chatFileInputRef={messaging.chatFileInputRef}
                handleChatFileSelect={messaging.handleChatFileSelect}
                isDraggingFile={messaging.isDraggingFile}
                handleDragOver={messaging.handleDragOver}
                handleDragLeave={messaging.handleDragLeave}
                handleDrop={messaging.handleDrop}
                handleResizeStart={handleResizeStart}
                newMessage={messaging.newMessage}
                setNewMessage={messaging.setNewMessage}
                textareaHeight={messaging.textareaHeight}
                sendMessage={messaging.sendMessage}
                updatesText={updatesText}
                onNotesClick={handleNotesClick}
              />
            ) : currentProject ? (
              <ProjectLanding
                viewMode={viewMode} setViewMode={setViewMode}
                currentProject={currentProject} currentChat={currentChat}
                openChat={openChat} startCreateChat={startCreateChat} startCreateProject={startCreateProject}
                chatSearchQuery={chatSearchQuery} setChatSearchQuery={setChatSearchQuery}
                projectChatsDetailed={projectChatsDetailed}
                availableModels={availableModels}
                projectModel={projectModel}
                handleProjectModelChange={handleProjectModelChange}
                isPipelineProject={isPipelineProject}
                projectGameSystem={projectGameSystem}
                availableGameSystems={availableGameSystems}
                handleProjectGameSystemChange={handleProjectGameSystemChange}
                agentInstructions={agentInstructions}
                projectInstructions={projectInstructions}
                projectInstructionsTokens={projectInstructionsTokens}
                showInstructionsModal={showInstructionsModal}
                setShowInstructionsModal={setShowInstructionsModal}
                editingInstructions={editingInstructions}
                setEditingInstructions={setEditingInstructions}
                instructionsSaving={instructionsSaving}
                updateProjectInstructions={updateProjectInstructions}
                editingAgentInstructions={editingAgentInstructions}
                setEditingAgentInstructions={setEditingAgentInstructions}
                activeInstructionsTab={activeInstructionsTab}
                setActiveInstructionsTab={setActiveInstructionsTab}
                handleSaveAllAgentInstructions={handleSaveAllAgentInstructions}
                projectFiles={projectFiles}
                projectFilesTotalTokens={projectFilesTotalTokens}
                handleToggleFileStaged={handleToggleFileStaged}
                handleDeleteProjectFile={handleDeleteProjectFile}
                handleToggleFileAgent={handleToggleFileAgent}
                hoveredFilename={hoveredFilename}
                setHoveredFilename={setHoveredFilename}
                filenameTooltipPos={filenameTooltipPos}
                setFilenameTooltipPos={setFilenameTooltipPos}
                filenameTooltipTimeoutRef={filenameTooltipTimeoutRef}
                fileInputRef={fileInputRef}
                handleFileUpload={handleFileUpload}
                filesUploading={filesUploading}
                projects={projects}
                editingProject={editingProject}
                editingProjectName={editingProjectName}
                setEditingProjectName={setEditingProjectName}
                startRenameProject={startRenameProject}
                saveRenameProject={saveRenameProject}
                cancelRenameProject={cancelRenameProject}
                handleDeleteProject={handleDeleteProject}
                enterProject={enterProject}
                creatingProject={creatingProject}
                newItemName={newItemName}
                setNewItemName={setNewItemName}
                saveNewProject={saveNewProject}
                cancelCreate={cancelCreate}
                chats={chats}
                editingChat={editingChat}
                editingName={editingName}
                setEditingName={setEditingName}
                startRenameChat={startRenameChat}
                saveRename={saveRename}
                cancelRename={cancelRename}
                handleDeleteChat={handleDeleteChat}
                creatingChat={creatingChat}
                saveNewChat={saveNewChat}
              />
            ) : (
              <div style={styles.noChat}>
                <p>Select or create a chat to start</p>
              </div>
            )
          ) : (
            <ProjectLanding
              viewMode={viewMode} setViewMode={setViewMode}
              currentProject={currentProject} currentChat={currentChat}
              openChat={openChat} startCreateChat={startCreateChat} startCreateProject={startCreateProject}
              chatSearchQuery={chatSearchQuery} setChatSearchQuery={setChatSearchQuery}
              projectChatsDetailed={projectChatsDetailed}
              availableModels={availableModels}
              projectModel={projectModel}
              handleProjectModelChange={handleProjectModelChange}
              isPipelineProject={isPipelineProject}
              projectGameSystem={projectGameSystem}
              availableGameSystems={availableGameSystems}
              handleProjectGameSystemChange={handleProjectGameSystemChange}
              agentInstructions={agentInstructions}
              projectInstructions={projectInstructions}
              projectInstructionsTokens={projectInstructionsTokens}
              showInstructionsModal={showInstructionsModal}
              setShowInstructionsModal={setShowInstructionsModal}
              editingInstructions={editingInstructions}
              setEditingInstructions={setEditingInstructions}
              instructionsSaving={instructionsSaving}
              updateProjectInstructions={updateProjectInstructions}
              editingAgentInstructions={editingAgentInstructions}
              setEditingAgentInstructions={setEditingAgentInstructions}
              activeInstructionsTab={activeInstructionsTab}
              setActiveInstructionsTab={setActiveInstructionsTab}
              handleSaveAllAgentInstructions={handleSaveAllAgentInstructions}
              projectFiles={projectFiles}
              projectFilesTotalTokens={projectFilesTotalTokens}
              handleToggleFileStaged={handleToggleFileStaged}
              handleDeleteProjectFile={handleDeleteProjectFile}
              handleToggleFileAgent={handleToggleFileAgent}
              hoveredFilename={hoveredFilename}
              setHoveredFilename={setHoveredFilename}
              filenameTooltipPos={filenameTooltipPos}
              setFilenameTooltipPos={setFilenameTooltipPos}
              filenameTooltipTimeoutRef={filenameTooltipTimeoutRef}
              fileInputRef={fileInputRef}
              handleFileUpload={handleFileUpload}
              filesUploading={filesUploading}
              projects={projects}
              editingProject={editingProject}
              editingProjectName={editingProjectName}
              setEditingProjectName={setEditingProjectName}
              startRenameProject={startRenameProject}
              saveRenameProject={saveRenameProject}
              cancelRenameProject={cancelRenameProject}
              handleDeleteProject={handleDeleteProject}
              enterProject={enterProject}
              creatingProject={creatingProject}
              newItemName={newItemName}
              setNewItemName={setNewItemName}
              saveNewProject={saveNewProject}
              cancelCreate={cancelCreate}
              chats={chats}
              editingChat={editingChat}
              editingName={editingName}
              setEditingName={setEditingName}
              startRenameChat={startRenameChat}
              saveRename={saveRename}
              cancelRename={cancelRename}
              handleDeleteChat={handleDeleteChat}
              creatingChat={creatingChat}
              saveNewChat={saveNewChat}
            />
          )}
        </div>

        {/* Right Panel -- Character State */}
        <CharacterPanel
          isMobile={isMobile}
          pipelineState={pipelineState}
          chatGameSystem={chatGameSystem}
          rightPanelOpen={rightPanelOpen}
          setRightPanelOpen={setRightPanelOpen}
          selectedCharacter={selectedCharacter}
          setSelectedCharacter={setSelectedCharacter}
          showCharacterSheet={showCharacterSheet}
          setShowCharacterSheet={setShowCharacterSheet}
          showAllCharactersModal={showAllCharactersModal}
          setShowAllCharactersModal={setShowAllCharactersModal}
          showNpcMemories={showNpcMemories}
          setShowNpcMemories={setShowNpcMemories}
          mobileBottomSheetOpen={mobileBottomSheetOpen}
          setMobileBottomSheetOpen={setMobileBottomSheetOpen}
          characterSheetMd={characterSheetMd}
        />
      </div>

      {/* Modals */}
      <Modals
        showUpdatesModal={showUpdatesModal}
        setShowUpdatesModal={setShowUpdatesModal}
        draftUpdatesText={draftUpdatesText}
        updateUpdatesText={updateUpdatesText}
        updatesTokenCount={updatesTokenCount}
        updatesLoading={updatesLoading}
        saveUpdates={saveUpdates}
        showApiKeyModal={showApiKeyModal}
        pendingModelSwitch={pendingModelSwitch}
        modalApiKey={modalApiKey}
        setModalApiKey={setModalApiKey}
        handleApiKeyModalSave={handleApiKeyModalSave}
        handleApiKeyModalCancel={handleApiKeyModalCancel}
        savingApiKey={savingApiKey}
        availableModels={availableModels}
      />

      {docsRefreshed && (
        <div style={styles.docsRefreshedBanner}>
          Context trimmed. Instructions & project files refreshed.
          <button onClick={() => setDocsRefreshed(false)} style={styles.errorClose}>×</button>
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

export default App;
