import React, { useState } from 'react';
import { styles } from '../styles';
import { ChatCardInfo, ModelInfo, ProjectFileInfo, formatTimestamp } from '../types';

interface ProjectLandingProps {
  viewMode: 'chat' | 'projectList' | 'chatList';
  setViewMode: (v: 'chat' | 'projectList' | 'chatList') => void;
  currentProject: string | null;
  currentChat: string | null;
  openChat: (name: string) => void;
  startCreateChat: () => void;
  startCreateProject: () => void;
  chatSearchQuery: string;
  setChatSearchQuery: (v: string) => void;
  projectChatsDetailed: ChatCardInfo[];
  availableModels: ModelInfo[];
  projectModel: string | null;
  handleProjectModelChange: (v: string) => void;
  isPipelineProject: boolean;
  projectGameSystem: string | null;
  availableGameSystems: {id: string, name: string}[];
  handleProjectGameSystemChange: (v: string) => void;
  agentInstructions: Record<string, {instructions: string, tokens: number}>;
  projectInstructions: string;
  projectInstructionsTokens: number;
  showInstructionsModal: boolean;
  setShowInstructionsModal: (v: boolean) => void;
  editingInstructions: string;
  setEditingInstructions: (v: string) => void;
  instructionsSaving: boolean;
  updateProjectInstructions: () => void;
  editingAgentInstructions: Record<string, string>;
  setEditingAgentInstructions: (v: Record<string, string>) => void;
  activeInstructionsTab: string;
  setActiveInstructionsTab: (v: string) => void;
  handleSaveAllAgentInstructions: () => void;
  projectFiles: ProjectFileInfo[];
  projectFilesTotalTokens: number;
  handleToggleFileStaged: (filename: string, staged: boolean) => void;
  handleDeleteProjectFile: (filename: string) => void;
  handleToggleFileAgent: (filename: string, agent: string) => void;
  hoveredFilename: string | null;
  setHoveredFilename: (v: string | null) => void;
  filenameTooltipPos: {x: number, y: number};
  setFilenameTooltipPos: (v: {x: number, y: number}) => void;
  filenameTooltipTimeoutRef: React.MutableRefObject<NodeJS.Timeout | null>;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  handleFileUpload: (e: React.ChangeEvent<HTMLInputElement>) => void;
  filesUploading: boolean;
  projects: string[];
  editingProject: string | null;
  editingProjectName: string;
  setEditingProjectName: (v: string) => void;
  startRenameProject: (name: string) => void;
  saveRenameProject: () => void;
  cancelRenameProject: () => void;
  handleDeleteProject: (name: string) => void;
  enterProject: (name: string) => void;
  creatingProject: boolean;
  newItemName: string;
  setNewItemName: (v: string) => void;
  saveNewProject: () => void;
  cancelCreate: () => void;
  chats: string[];
  editingChat: string | null;
  editingName: string;
  setEditingName: (v: string) => void;
  startRenameChat: (name: string) => void;
  saveRename: () => void;
  cancelRename: () => void;
  handleDeleteChat: (name: string) => void;
  creatingChat: boolean;
  saveNewChat: () => void;
  isMobile?: boolean;
}

export default function ProjectLanding(props: ProjectLandingProps) {
  const {
    viewMode, setViewMode, currentProject, currentChat,
    openChat, startCreateChat, startCreateProject,
    chatSearchQuery, setChatSearchQuery, projectChatsDetailed,
    availableModels, projectModel, handleProjectModelChange,
    isPipelineProject, projectGameSystem, availableGameSystems, handleProjectGameSystemChange,
    agentInstructions, projectInstructions, projectInstructionsTokens,
    showInstructionsModal, setShowInstructionsModal,
    editingInstructions, setEditingInstructions,
    instructionsSaving, updateProjectInstructions,
    editingAgentInstructions, setEditingAgentInstructions,
    activeInstructionsTab, setActiveInstructionsTab,
    handleSaveAllAgentInstructions,
    projectFiles, projectFilesTotalTokens,
    handleToggleFileStaged, handleDeleteProjectFile, handleToggleFileAgent,
    hoveredFilename, setHoveredFilename,
    filenameTooltipPos, setFilenameTooltipPos,
    filenameTooltipTimeoutRef, fileInputRef,
    handleFileUpload, filesUploading,
    projects, editingProject, editingProjectName, setEditingProjectName,
    startRenameProject, saveRenameProject, cancelRenameProject,
    handleDeleteProject, enterProject,
    creatingProject, newItemName, setNewItemName, saveNewProject, cancelCreate,
    chats, editingChat, editingName, setEditingName,
    startRenameChat, saveRename, cancelRename,
    handleDeleteChat, creatingChat, saveNewChat,
    isMobile,
  } = props;

  const [configOpen, setConfigOpen] = useState(false);

  if (viewMode === 'chat' && currentProject) {
    // Project landing page
    return (
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
                                </div>
                              </div>
                            ))}
                          </div>
                        ));
                      })()}
                    </div>
                  </div>

                  {/* Mobile toggle button for config */}
                  {isMobile && !configOpen && (
                    <button
                      onClick={() => setConfigOpen(true)}
                      style={{
                        position: 'fixed',
                        bottom: 16,
                        right: 16,
                        zIndex: 1001,
                        width: 48,
                        height: 48,
                        borderRadius: '50%',
                        backgroundColor: '#4a4a8a',
                        color: '#fff',
                        border: 'none',
                        fontSize: '20px',
                        cursor: 'pointer',
                        boxShadow: '0 2px 8px rgba(0,0,0,0.4)',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                      }}
                      title="Project settings"
                    >
                      ⚙
                    </button>
                  )}

                  {/* Overlay for mobile config panel */}
                  {isMobile && configOpen && (
                    <div
                      onClick={() => setConfigOpen(false)}
                      style={{
                        position: 'fixed',
                        top: 0,
                        left: 0,
                        right: 0,
                        bottom: 0,
                        backgroundColor: 'rgba(0,0,0,0.5)',
                        zIndex: 999,
                      }}
                    />
                  )}

                  {/* Right column: Model + Instructions + Files */}
                  <div style={{
                    ...styles.projectConfigColumn,
                    ...(isMobile ? {
                      position: 'fixed' as const,
                      top: 0,
                      right: 0,
                      bottom: 0,
                      width: '85vw',
                      maxWidth: '380px',
                      zIndex: 1000,
                      transform: configOpen ? 'translateX(0)' : 'translateX(100%)',
                      transition: 'transform 0.3s ease',
                      boxShadow: configOpen ? '-4px 0 16px rgba(0,0,0,0.3)' : 'none',
                    } : {}),
                  }}>
                    {/* Mobile close button */}
                    {isMobile && (
                      <div style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        padding: '12px 16px',
                        borderBottom: '1px solid #333',
                      }}>
                        <h3 style={{ margin: 0, color: '#e0e0e0', fontSize: '1rem' }}>Settings</h3>
                        <button
                          onClick={() => setConfigOpen(false)}
                          style={{
                            background: 'none',
                            border: 'none',
                            color: '#aaa',
                            fontSize: '24px',
                            cursor: 'pointer',
                            padding: '0 4px',
                          }}
                        >×</button>
                      </div>
                    )}
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

                    {/* Game System section */}
                    <div style={styles.projectSection}>
                      <div style={styles.projectSectionHeader}>
                        <h3 style={styles.projectSectionTitle}>Game System</h3>
                      </div>
                      <select
                        value={projectGameSystem || 'dnd5e'}
                        onChange={(e) => handleProjectGameSystemChange(e.target.value)}
                        style={styles.projectModelSelector}
                      >
                        {availableGameSystems.map(gs => (
                          <option key={gs.id} value={gs.id}>{gs.name}</option>
                        ))}
                      </select>
                      <p style={styles.projectModelHelperText}>Determines dice mechanics, state tracking, and narrative style</p>
                    </div>

                    {/* Instructions section */}
                    <div style={styles.projectSection}>
                      <div style={styles.projectSectionHeader}>
                        <h3 style={styles.projectSectionTitle}>Instructions</h3>
                        {isPipelineProject ? (
                          <span style={styles.tokenBadge}>
                            {Object.values(agentInstructions).reduce((sum, a) => sum + (a.tokens || 0), 0).toLocaleString()} tokens (agents)
                          </span>
                        ) : (
                          <span style={styles.tokenBadge}>{projectInstructionsTokens.toLocaleString()} tokens</span>
                        )}
                      </div>
                      {isPipelineProject ? (
                        <>
                          <div style={styles.instructionsPreview}>
                            {['events', 'mechanics', 'narration'].map(agent => {
                              const ai = agentInstructions[agent];
                              const hasContent = ai && ai.instructions && ai.instructions.trim();
                              return (
                                <span key={agent} style={{ marginRight: 8, color: hasContent ? '#e0e0e0' : '#666' }}>
                                  {agent.charAt(0).toUpperCase() + agent.slice(1)}: {hasContent ? `${ai.tokens} tok` : 'shared'}
                                </span>
                              );
                            })}
                          </div>
                          <button
                            onClick={() => {
                              const editing: Record<string, string> = {};
                              for (const agent of ['events', 'mechanics', 'narration']) {
                                editing[agent] = agentInstructions[agent]?.instructions || '';
                              }
                              setEditingAgentInstructions(editing);
                              setActiveInstructionsTab('events');
                              setShowInstructionsModal(true);
                            }}
                            style={styles.editInstructionsButton}
                          >
                            Edit Agent Instructions
                          </button>
                        </>
                      ) : (
                        <>
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
                        </>
                      )}
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
                              {isPipelineProject && file.staged && (
                                <span style={styles.agentBadges}>
                                  {[{key: 'events', label: 'E'}, {key: 'mechanics', label: 'M'}, {key: 'narration', label: 'N'}].map(({key, label}) => {
                                    const active = (file.agents || ['events', 'mechanics', 'narration']).includes(key);
                                    return (
                                      <span
                                        key={key}
                                        onClick={() => handleToggleFileAgent(file.filename, key)}
                                        style={active ? styles.agentBadgeActive : styles.agentBadgeInactive}
                                        title={`${active ? 'Remove from' : 'Add to'} ${key} agent`}
                                      >
                                        {label}
                                      </span>
                                    );
                                  })}
                                </span>
                              )}
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
                        accept=".txt,.md,.yaml,.yml"
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
                      {isPipelineProject ? (
                        <>
                          <h3 style={styles.modalTitle}>Edit Agent Instructions</h3>
                          <p style={styles.modalDescription}>
                            Per-agent instructions. Leave empty to use the shared instructions.
                          </p>
                          <div style={styles.agentTabBar}>
                            {['events', 'mechanics', 'narration'].map(agent => (
                              <button
                                key={agent}
                                onClick={() => setActiveInstructionsTab(agent)}
                                style={activeInstructionsTab === agent ? {...styles.agentTab, ...styles.agentTabActive} : styles.agentTab}
                              >
                                {agent.charAt(0).toUpperCase() + agent.slice(1)}
                                <span style={styles.agentTabTokens}>
                                  ~{editingAgentInstructions[agent] ? Math.ceil(editingAgentInstructions[agent].length / 4) : 0}
                                </span>
                              </button>
                            ))}
                          </div>
                          <textarea
                            value={editingAgentInstructions[activeInstructionsTab] || ''}
                            onChange={(e) => setEditingAgentInstructions({
                              ...editingAgentInstructions,
                              [activeInstructionsTab]: e.target.value
                            })}
                            placeholder="Leave empty to use shared instructions"
                            style={styles.updatesTextarea}
                            rows={12}
                          />
                          <div style={styles.modalFooter}>
                            <span style={styles.tokenCount}>
                              Total: ~{Object.values(editingAgentInstructions).reduce((sum, t) => sum + (t ? Math.ceil(t.length / 4) : 0), 0)} tokens (estimate)
                            </span>
                            <div style={styles.modalActions}>
                              <button onClick={() => setShowInstructionsModal(false)} style={styles.modalCancelButton}>
                                Cancel
                              </button>
                              <button
                                onClick={handleSaveAllAgentInstructions}
                                disabled={instructionsSaving}
                                style={styles.modalSaveButton}
                              >
                                {instructionsSaving ? 'Saving...' : 'Save All'}
                              </button>
                            </div>
                          </div>
                        </>
                      ) : (
                        <>
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
                        </>
                      )}
                    </div>
                  </div>
                )}

              </div>
    );
  }

  if (viewMode === 'projectList') {
    // Full project list view
    return (
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
    );
  }

  if (viewMode === 'chatList') {
    // Full chat list view
    return (
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
    );
  }

  return null;
}
