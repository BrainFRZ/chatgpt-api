import React from 'react';
import { styles } from '../styles';
import { ChatStats, UserStats, FreeTokens } from '../types';

interface SidebarProps {
  isMobile: boolean;
  sidebarOpen: boolean;
  setSidebarOpen: (v: boolean) => void;
  user: { username: string };
  handleLogout: () => void;
  showStatsTooltip: boolean;
  setShowStatsTooltip: (v: boolean) => void;
  userStats: UserStats | null;
  freeTokens: FreeTokens | null;
  projectsExpanded: boolean;
  setProjectsExpanded: (v: boolean) => void;
  projects: string[];
  currentProject: string | null;
  editingProject: string | null;
  editingProjectName: string;
  setEditingProjectName: (v: string) => void;
  startRenameProject: (name: string) => void;
  saveRenameProject: () => void;
  cancelRenameProject: () => void;
  handleDeleteProject: (name: string) => void;
  enterProject: (name: string) => void;
  creatingProject: boolean;
  startCreateProject: () => void;
  newItemName: string;
  setNewItemName: (v: string) => void;
  saveNewProject: () => void;
  cancelCreate: () => void;
  setViewMode: (v: 'chat' | 'projectList' | 'chatList') => void;
  chatsExpanded: boolean;
  setChatsExpanded: (v: boolean) => void;
  chats: string[];
  currentChat: string | null;
  editingChat: string | null;
  editingName: string;
  setEditingName: (v: string) => void;
  startRenameChat: (name: string) => void;
  saveRename: () => void;
  cancelRename: () => void;
  handleDeleteChat: (name: string) => void;
  openChat: (name: string) => void;
  creatingChat: boolean;
  startCreateChat: () => void;
  saveNewChat: () => void;
  exitProject: () => void;
  pipelineState: any;
  stats: ChatStats | null;
}

export default function Sidebar(props: SidebarProps) {
  const {
    isMobile,
    sidebarOpen,
    setSidebarOpen,
    user,
    handleLogout,
    showStatsTooltip,
    setShowStatsTooltip,
    userStats,
    freeTokens,
    projectsExpanded,
    setProjectsExpanded,
    projects,
    currentProject,
    editingProject,
    editingProjectName,
    setEditingProjectName,
    startRenameProject,
    saveRenameProject,
    cancelRenameProject,
    handleDeleteProject,
    enterProject,
    creatingProject,
    startCreateProject,
    newItemName,
    setNewItemName,
    saveNewProject,
    cancelCreate,
    setViewMode,
    chatsExpanded,
    setChatsExpanded,
    chats,
    currentChat,
    editingChat,
    editingName,
    setEditingName,
    startRenameChat,
    saveRename,
    cancelRename,
    handleDeleteChat,
    openChat,
    creatingChat,
    startCreateChat,
    saveNewChat,
    exitProject,
    pipelineState,
    stats,
  } = props;

  return (
    <>
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

      {/* Collapsed sidebar strip (desktop only) */}
      {!isMobile && !sidebarOpen && (
        <div style={styles.collapsedSidebarStrip}>
          <button
            onClick={() => setSidebarOpen(true)}
            style={styles.expandSidebarButton}
            title="Expand sidebar (Ctrl+\)"
          >
            »
          </button>
        </div>
      )}

      {/* Sidebar */}
      <div style={{
        ...styles.sidebar,
        ...(isMobile ? styles.sidebarMobile : {}),
        ...(isMobile && !sidebarOpen ? styles.sidebarHidden : {}),
        ...(!isMobile && !sidebarOpen ? styles.sidebarCollapsed : {}),
      }}>
        <div style={styles.sidebarHeader}>
          <div style={styles.sidebarHeaderRow}>
            <h2 style={styles.sidebarTitle}>Chorus AI</h2>
            {isMobile && (
              <button
                onClick={() => setSidebarOpen(false)}
                style={styles.closeSidebarButton}
              >
                ✕
              </button>
            )}
            <button onClick={handleLogout} style={styles.logoutButton} title="Logout">↩</button>
            {!isMobile && (
              <button
                onClick={() => setSidebarOpen(false)}
                style={styles.collapseSidebarButton}
                title="Collapse sidebar (Ctrl+\)"
              >
                «
              </button>
            )}
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
                  <div style={styles.statsRow}>Prompts: {userStats.lifetime_gpt_prompts.toLocaleString()} GPT | {userStats.lifetime_sonnet_prompts.toLocaleString()} Opus | {userStats.lifetime_prompts.toLocaleString()} Total</div>
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
                  <div style={styles.statsRow}>Prompts: {userStats.monthly_gpt_prompts.toLocaleString()} GPT | {userStats.monthly_sonnet_prompts.toLocaleString()} Opus | {userStats.monthly_prompts.toLocaleString()} Total</div>
                  <div style={styles.statsRow}>
                    Tokens: I:{userStats.monthly_input_tokens.toLocaleString()} C:{userStats.monthly_cached_tokens.toLocaleString()} O:{userStats.monthly_output_tokens.toLocaleString()} R:{userStats.monthly_reasoning_tokens.toLocaleString()}
                  </div>
                  <div style={styles.statsRow}>Cost: ${userStats.monthly_cost.toFixed(4)}</div>
                </div>
                <div style={styles.statsSeparator} />
                <div style={styles.statsSection}>
                  <div style={styles.statsSectionTitle}>Today</div>
                  <div style={styles.statsRow}>Prompts: {userStats.today_gpt_prompts.toLocaleString()} GPT | {userStats.today_sonnet_prompts.toLocaleString()} Opus | {userStats.today_prompts.toLocaleString()} Total</div>
                  <div style={styles.statsRow}>
                    Tokens: I:{userStats.today_input_tokens.toLocaleString()} C:{userStats.today_cached_tokens.toLocaleString()} O:{userStats.today_output_tokens.toLocaleString()} R:{userStats.today_reasoning_tokens.toLocaleString()}
                  </div>
                  <div style={styles.statsRow}>Cost: ${userStats.today_cost.toFixed(4)}</div>
                </div>
                <div style={styles.statsSeparator} />
                <div style={styles.statsSection}>
                  <div style={styles.statsSectionTitle}>Daily Averages ({userStats.days_since_first} days)</div>
                  <div style={styles.statsRow}>Prompts/day: {userStats.avg_gpt_prompts_per_day.toFixed(1)} GPT | {userStats.avg_sonnet_prompts_per_day.toFixed(1)} Opus | {userStats.avg_prompts_per_day.toFixed(1)} Total</div>
                  <div style={styles.statsRow}>
                    TPD: I:{userStats.avg_input_per_day.toFixed(0)} C:{userStats.avg_cached_per_day.toFixed(0)} O:{userStats.avg_output_per_day.toFixed(0)} R:{userStats.avg_reasoning_per_day.toFixed(0)}
                  </div>
                  <div style={styles.statsRow}>Cost/day: ${userStats.avg_cost_per_day.toFixed(4)}</div>
                </div>
                <div style={styles.statsSeparator} />
                <div style={styles.statsSection}>
                  <div style={styles.statsSectionTitle}>Avg Context Growth</div>
                  <div style={styles.statsRow}>GPT: {Math.round(userStats.avg_gpt_context_growth).toLocaleString()} tokens</div>
                  <div style={styles.statsRow}>Opus: {Math.round(userStats.avg_sonnet_context_growth).toLocaleString()} tokens</div>
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

        {pipelineState?.pacing && (() => {
          const hud = pipelineState?.hud_state || {};
          const rawTime = typeof hud.time === 'string' ? hud.time.trim() : '';
          let prettyTime = '';
          if (rawTime && /^\d{3,4}$/.test(rawTime)) {
            const h = rawTime.length === 4 ? rawTime.slice(0, 2) : rawTime.slice(0, 1);
            const m = rawTime.slice(-2);
            prettyTime = `${h.padStart(2, '0')}:${m}`;
          }
          const dateStr = typeof hud.date === 'string' ? hud.date.trim() : '';
          const dateLine = [dateStr, prettyTime].filter(Boolean).join(' ');
          return (
            <div style={styles.statsBox}>
              <p style={styles.statsText}>{pipelineState.pacing.episode}</p>
              <p style={styles.statsText}>Beat: {pipelineState.pacing.beat}</p>
              <p style={styles.statsText}>
                Responses: {pipelineState.pacing.responses}
                {pipelineState.beat_state?.session_responses != null &&
                  ` (${pipelineState.beat_state.session_responses} total)`}
              </p>
              {dateLine && <p style={styles.statsText}>Date: {dateLine}</p>}
              {hud.location && <p style={styles.statsText}>Loc: {hud.location}</p>}
            </div>
          );
        })()}

        {stats && (
          <div style={styles.statsBox}>
            <p style={styles.statsText}>Prompts: {stats.gpt_prompts ?? 0} GPT | {stats.sonnet_prompts ?? 0} Opus | {stats.total_prompts} Total</p>
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
                  <p style={styles.statsText}>Avg Context: {Math.round(stats.avg_gpt_context_growth ?? 0).toLocaleString()} GPT | {Math.round(stats.avg_sonnet_context_growth ?? 0).toLocaleString()} Opus</p>
                </>
              );
            })()}
          </div>
        )}
      </div>
    </>
  );
}
