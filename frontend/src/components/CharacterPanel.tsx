import React, { useState, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import { styles } from '../styles';
import NetMapPopup from './NetMapPopup';

export interface CharacterPanelProps {
  isMobile: boolean;
  pipelineState: any;
  chatGameSystem: string | null;
  rightPanelOpen: boolean;
  setRightPanelOpen: (v: boolean) => void;
  selectedCharacter: string | null;
  setSelectedCharacter: (v: string | null) => void;
  showCharacterSheet: boolean;
  setShowCharacterSheet: (v: boolean) => void;
  showAllCharactersModal: boolean;
  setShowAllCharactersModal: (v: boolean) => void;
  showNpcMemories: string | null;
  setShowNpcMemories: (v: string | null) => void;
  mobileBottomSheetOpen: boolean;
  setMobileBottomSheetOpen: (v: boolean) => void;
  characterSheetFiles: {name: string, content: string}[];
  hackState: any;
}

// ─── VS Code Dark+ YAML Syntax Highlighting ───

const yaml = {
  key: '#9CDCFE',       // light blue — mapping keys
  string: '#CE9178',    // orange-brown — quoted & unquoted string values
  bool: '#569CD6',      // blue — true/false/null
  number: '#B5CEA8',    // light green — numeric literals
  comment: '#6A9955',   // green — comments
  punct: '#D4D4D4',     // gray — colons, dashes, brackets
  text: '#D4D4D4',      // gray — default text
  anchor: '#DCDCAA',    // yellow — anchors & aliases
};

const yamlBoolRe = /^(true|false|yes|no|on|off|null|~)$/i;
const yamlNumRe = /^[+-]?(\d[\d_]*(\.\d[\d_]*)?([eE][+-]?\d+)?|0x[0-9a-fA-F]+|0o[0-7]+|\.inf|\.nan)$/;

/** Find inline comment (# preceded by space, outside quotes). */
function findInlineComment(s: string): number {
  let inSingle = false, inDouble = false;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (c === "'" && !inDouble) inSingle = !inSingle;
    else if (c === '"' && !inSingle) inDouble = !inDouble;
    else if (c === '#' && !inSingle && !inDouble && i > 0 && s[i - 1] === ' ') return i;
  }
  return -1;
}

/** Find key-separating colon (skips colons inside quotes). */
function findKeyColon(s: string): number {
  let inSingle = false, inDouble = false;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (c === "'" && !inDouble) inSingle = !inSingle;
    else if (c === '"' && !inSingle) inDouble = !inDouble;
    else if (c === ':' && !inSingle && !inDouble && (i + 1 >= s.length || s[i + 1] === ' ')) return i;
  }
  return -1;
}

/** Render a YAML value span with appropriate coloring. */
function renderYamlValue(val: string): React.ReactNode {
  const cIdx = findInlineComment(val);
  const main = cIdx >= 0 ? val.slice(0, cIdx) : val;
  const comment = cIdx >= 0 ? val.slice(cIdx) : null;
  const trimmed = main.trim();

  let color = yaml.string; // default: unquoted string
  if (trimmed.startsWith('"') || trimmed.startsWith("'")) color = yaml.string;
  else if (trimmed.startsWith('&') || trimmed.startsWith('*')) color = yaml.anchor;
  else if (yamlBoolRe.test(trimmed)) color = yaml.bool;
  else if (yamlNumRe.test(trimmed)) color = yaml.number;
  else if (trimmed.startsWith('|') || trimmed.startsWith('>')) color = yaml.punct;
  else if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
    // Flow collection — color brackets/commas as punctuation
    return (
      <>
        {main.split(/([[\]{},:])/g).map((part, j) =>
          /^[[\]{},:]$/.test(part)
            ? <span key={j} style={{ color: yaml.punct }}>{part}</span>
            : <span key={j} style={{ color: yaml.text }}>{part}</span>
        )}
        {comment && <span style={{ color: yaml.comment }}>{comment}</span>}
      </>
    );
  }

  return (
    <>
      <span style={{ color }}>{main}</span>
      {comment && <span style={{ color: yaml.comment }}>{comment}</span>}
    </>
  );
}

/** YAML content with VS Code Dark+ syntax highlighting. */
function YamlHighlighted({ content }: { content: string }) {
  return (
    <div style={{
      fontSize: '0.75rem', color: yaml.text, lineHeight: 1.6, marginTop: '8px',
      fontFamily: "'Consolas', 'Monaco', 'Courier New', monospace",
      whiteSpace: 'pre-wrap', wordBreak: 'break-word',
      background: '#16162a', borderRadius: '4px', padding: '8px',
    }}>
      {content.split('\n').map((line, i) => {
        if (!line.trim()) return <div key={i}>{'\n'}</div>;

        const trimmed = line.trimStart();
        const indent = line.length - trimmed.length;
        const indentSpan = indent > 0 ? <span>{line.slice(0, indent)}</span> : null;

        // Full-line comment
        if (trimmed.startsWith('#')) {
          return <div key={i}>{indentSpan}<span style={{ color: yaml.comment }}>{trimmed}</span></div>;
        }
        // Document markers
        if (trimmed === '---' || trimmed === '...') {
          return <div key={i}>{indentSpan}<span style={{ color: yaml.punct }}>{trimmed}</span></div>;
        }

        // After indent, check for list dash
        let pos = indent;
        const parts: React.ReactNode[] = [];
        if (indentSpan) parts.push(<span key="ind">{line.slice(0, indent)}</span>);

        if (trimmed.startsWith('- ') || (trimmed === '-')) {
          parts.push(<span key="dash" style={{ color: yaml.punct }}>-</span>);
          pos += 1;
          if (pos < line.length && line[pos] === ' ') {
            parts.push(<span key="dashsp"> </span>);
            pos += 1;
          }
          if (pos >= line.length) return <div key={i}>{parts}</div>;
        }

        // Key: value detection
        const remaining = line.slice(pos);
        const colonIdx = findKeyColon(remaining);
        if (colonIdx >= 0) {
          const key = remaining.slice(0, colonIdx);
          const keyColor = (key.startsWith('&') || key.startsWith('*')) ? yaml.anchor : yaml.key;
          parts.push(<span key="key" style={{ color: keyColor }}>{key}</span>);
          parts.push(<span key="colon" style={{ color: yaml.punct }}>:</span>);
          const afterColon = remaining.slice(colonIdx + 1);
          if (afterColon) {
            const valueStart = afterColon.search(/\S/);
            if (valueStart < 0) {
              parts.push(<span key="trail">{afterColon}</span>);
            } else {
              parts.push(<span key="sp">{afterColon.slice(0, valueStart)}</span>);
              parts.push(<React.Fragment key="val">{renderYamlValue(afterColon.slice(valueStart))}</React.Fragment>);
            }
          }
        } else {
          // No key — treat as value (list item value, etc.)
          parts.push(<React.Fragment key="val">{renderYamlValue(remaining)}</React.Fragment>);
        }

        return <div key={i}>{parts}</div>;
      })}
    </div>
  );
}

export default function CharacterPanel({
  isMobile,
  pipelineState,
  chatGameSystem,
  rightPanelOpen,
  setRightPanelOpen,
  selectedCharacter,
  setSelectedCharacter,
  showCharacterSheet,
  setShowCharacterSheet,
  showAllCharactersModal,
  setShowAllCharactersModal,
  showNpcMemories,
  setShowNpcMemories,
  mobileBottomSheetOpen,
  setMobileBottomSheetOpen,
  characterSheetFiles,
  hackState,
}: CharacterPanelProps) {

  const [showCallbacksModal, setShowCallbacksModal] = useState(false);
  const [showNetMap, setShowNetMap] = useState(false);
  // Toggle between rendered markdown and raw syntax-highlighted YAML source view.
  // Works for both .md and .yaml sheets — flipping to "raw" renders the source
  // through YamlHighlighted regardless of file extension.
  const [sheetRawView, setSheetRawView] = useState(false);

  // Vital bar color by percentage
  const vitalColor = (cur: number, max: number, label?: string) => {
    const pct = max > 0 ? cur / max : 1;
    const l = (label || '').toLowerCase();
    if (l.includes('san')) return pct > 0.6 ? '#60a5fa' : pct > 0.3 ? '#a78bfa' : '#7c3aed';
    if (l.includes('human')) return pct > 0.6 ? '#2dd4bf' : pct > 0.3 ? '#fbbf24' : '#ef4444';
    if (l.includes('hull')) return pct > 0.6 ? '#94a3b8' : pct > 0.3 ? '#fb923c' : '#ef4444';
    if (l.includes('shield')) return pct > 0.6 ? '#38bdf8' : pct > 0.3 ? '#fb923c' : '#ef4444';
    return pct > 0.6 ? '#4ade80' : pct > 0.3 ? '#fbbf24' : '#ef4444';
  };

  // Condition pill color
  const condColor = (c: string) => {
    const cl = c.toLowerCase();
    if (/poison|bleed|burn|wound|dying|dead|critical/.test(cl)) return '#ef4444';
    if (/exhaust|fright|stun|prone|restrain|shock|disabl|immobi/.test(cl)) return '#fb923c';
    if (/bless|haste|shield|invis|inspir|protect|rage|enhance/.test(cl)) return '#4ade80';
    return '#94a3b8';
  };

  // Relationship tier derivation (mirrors backend _rs_tier / _roms_tier)
  const rsTier = (score: number): [string, string] => {
    if (score >= 95) return ['T7: Ride-or-Die', '+5 all checks; fight together; share all'];
    if (score >= 85) return ['T6: Ally', '+4 CHA checks; auto-success Persuasion; backup'];
    if (score >= 70) return ['T5: Close', '+3 CHA checks; Adv Persuasion/Deception'];
    if (score >= 55) return ['T4: Good', '+2 CHA checks; Adv Persuasion'];
    if (score >= 40) return ['T3: Friend', '+2 Persuasion/Insight; request favors no roll'];
    if (score >= 25) return ['T2: Friendly', '+1 Persuasion'];
    if (score >= 10) return ['T1: Acquaintance', 'no social penalties'];
    if (score >= -9) return ['Neutral', ''];
    if (score >= -24) return ['-T1: Annoyed', 'Disadv Persuasion'];
    if (score >= -39) return ['-T2: Disliked', '-2 CHA checks'];
    if (score >= -54) return ['-T3: Enemy', '-3 CHA checks; passive sabotage'];
    if (score >= -69) return ['-T4: Adversary', '-4 all checks; 1 obstacle/episode'];
    if (score >= -84) return ['-T5: Nemesis', '-5 all checks; 2 complications/episode'];
    if (score >= -94) return ['-T6: Sworn Enemy', '-6 all checks; ambushes'];
    return ['-T7: Hatred', '-7 all checks; attack regardless'];
  };
  const romsTier = (score: number): [string, string] => {
    if (score >= 95) return ['T6: Unbreakable', '+6 all checks; redirect dmg 1/LR; telepathy'];
    if (score >= 85) return ['T5: Married', '+5 all checks; shared HP pool; Adv Fear/Charm'];
    if (score >= 65) return ['T4: Engaged', '+4 all checks; gain 1 NPC skill; 1 Inspiration/ep'];
    if (score >= 45) return ['T3: Partner', '+3 CHA; fight together; reroll 1 save/LR'];
    if (score >= 25) return ['T2: Dating', '+2 Persuasion; Adv Insight; +1 Death saves'];
    if (score >= 10) return ['T1: Flirting', '+1 Persuasion; receptive to advances'];
    return ['None', ''];
  };

  // Type badge color
  const typeBadgeColor = (t: string) => {
    const tl = (t || '').toLowerCase();
    if (tl === 'pc') return '#3b82f6';
    if (tl === 'enemy') return '#ef4444';
    if (tl === 'ship') return '#94a3b8';
    return '#f59e0b';
  };

  // Resource label color
  const resourceColor = (label: string) => {
    const l = label.toLowerCase();
    if (/spell/.test(l)) return '#a78bfa';
    if (/tech|cyber/.test(l)) return '#22d3ee';
    if (/ki|channel|rage|wild/.test(l)) return '#f472b6';
    if (/ammo|round/.test(l)) return '#fb923c';
    if (/edge|luck/.test(l)) return '#fbbf24';
    return '#60a5fa';
  };

  const getCharData = (name: string) => {
    const cs = state.character_states || {};
    const entry = cs[name];
    if (!entry) {
      const pcs = (state.scene_state || {}).pcs_present || [];
      return { type: pcs.includes(name) ? 'pc' : 'npc' };
    }
    return entry.data || entry || {};
  };

  // Alert level labels and colors for hack mode HUD (matches rulebook thresholds)
  const alertLevelInfo = (level: number): [string, string] => {
    if (level <= 0) return ['Dormant', '#4ade80'];
    if (level <= 2) return ['Elevated', '#fbbf24'];
    if (level <= 4) return ['Active Search', '#fb923c'];
    if (level <= 6) return ['Lockdown', '#ef4444'];
    return ['Convergence', '#dc2626'];
  };

  // Hack mode HUD section
  const renderHackHud = (condensed?: boolean, hackStateOverride?: any) => {
    const hs = hackStateOverride || hackState;
    if (!hs?.active && !hackStateOverride) return null;
    const isCpred = hs.cycles_remaining !== undefined;
    const [alertLabel, alertColor] = alertLevelInfo(hs.alert_level || 0);
    const resLabel = isCpred ? 'Cycles' : 'Processes';
    const resCur = isCpred ? (hs.cycles_remaining ?? 0) : (hs.processes_remaining ?? 0);
    const resMax = isCpred ? (hs.cycles_max ?? 0) : (hs.processes_max ?? 0);
    const resPct = resMax > 0 ? Math.max(0, Math.min(100, (resCur / resMax) * 100)) : 0;
    const iceEntries = Object.entries(hs.ice_status || {});

    const categoryColor = (cat: string) => {
      switch (cat) { case 'booster': return '#4ade80'; case 'defender': return '#38bdf8'; case 'attacker': return '#ef4444'; case 'black_ice': return '#a855f7'; default: return '#00ff41'; }
    };

    return (
      <div style={{ padding: condensed ? '6px 8px' : '8px', borderBottom: '1px solid #0a3a0a', backgroundColor: '#0a1a0a' }}>
        {/* Alert Level */}
        <div style={{ marginBottom: '6px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.68rem', color: '#999', marginBottom: '2px' }}>
            <span>Alert Level</span>
            <span style={{ color: alertColor, fontWeight: 600 }}>{hs.alert_level} — {alertLabel}</span>
          </div>
          <div style={{ height: '4px', backgroundColor: '#1a2a1a', borderRadius: '2px', overflow: 'hidden' }}>
            <div style={{ height: '100%', width: `${Math.min(100, ((hs.alert_level || 0) / 7) * 100)}%`, backgroundColor: alertColor, borderRadius: '2px', transition: 'width 0.3s, background-color 0.3s' }} />
          </div>
        </div>

        {/* Resources (Cycles or Processes) */}
        <div style={{ marginBottom: '6px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.68rem', color: '#999', marginBottom: '2px' }}>
            <span>{resLabel}</span>
            <span>{resCur}/{resMax}</span>
          </div>
          <div style={{ height: '4px', backgroundColor: '#1a2a1a', borderRadius: '2px', overflow: 'hidden' }}>
            <div style={{ height: '100%', width: `${resPct}%`, backgroundColor: '#00ff41', borderRadius: '2px', transition: 'width 0.3s' }} />
          </div>
        </div>

        {/* NET Actions (CPRED only) */}
        {isCpred && hs.net_actions_per_turn && (
          <div style={{ fontSize: '0.68rem', color: '#999', marginBottom: '4px' }}>
            NET Actions: <span style={{ color: (hs.net_actions_remaining ?? hs.net_actions_per_turn) > 0 ? '#00ff41' : '#555', fontWeight: 600 }}>{hs.net_actions_remaining ?? hs.net_actions_per_turn}/{hs.net_actions_per_turn}</span>
            {hs.meatspace_due && <span style={{ color: '#f59e0b', marginLeft: '6px', fontSize: '0.6rem' }}>MEATSPACE DUE</span>}
          </div>
        )}

        {/* Stealth (Going Quiet DLC, CPRED only) */}
        {isCpred && (hs.stealth_active || hs.stealth_broken_round != null) && (
          <div style={{ fontSize: '0.68rem', color: '#999', marginBottom: '4px' }}>
            Stealth: {hs.stealth_active ? (
              <span style={{ color: '#00ff41', fontWeight: 600 }}>ACTIVE</span>
            ) : (
              <span style={{ color: '#ef4444', fontWeight: 600 }}>BROKEN — Jack Out to retry</span>
            )}
          </div>
        )}

        {/* Current Node */}
        {hs.current_node && (
          <div style={{ fontSize: '0.68rem', color: '#999', marginBottom: '4px' }}>
            <span>Node: </span>
            <span style={{ color: '#00ff41', fontWeight: 600 }}>{hs.current_node}</span>
            {hs.nodes_visited?.length > 0 && (
              <span style={{ color: '#555', marginLeft: '6px' }}>
                ({(() => { const prior = hs.nodes_visited.filter((n: string) => n !== hs.current_node); return prior.length > 0 ? prior.join(' \u2192 ') + ' \u2192 ' + hs.current_node : hs.current_node; })()})
              </span>
            )}
          </div>
        )}

        {!condensed && (
          <>
            {/* Active Programs — CPRED structured */}
            {hs.active_programs && hs.active_programs.length > 0 && (
              <div style={{ marginBottom: '4px' }}>
                <div style={{ fontSize: '0.65rem', color: '#666', marginBottom: '2px' }}>Programs</div>
                <div style={{ display: 'flex', gap: '3px', flexWrap: 'wrap' }}>
                  {hs.active_programs.map((p: { name: string; category: string; rez: number; status: string }, i: number) => {
                    // Status visual encoding (per Hacking Rulebook program lifecycle):
                    //   active      → lit (full color, no decoration)
                    //   deactivated → grayed (dimmed, no decoration; just stored, recoverable in 1 NA)
                    //   derezzed    → grayed + double strikethrough (recoverable in 2 NA: deactivate → activate)
                    //   destroyed   → very dim + double strikethrough (permanent loss unless Backup Drive saved)
                    const status = String(p.status || '').toLowerCase();
                    let color = categoryColor(p.category);
                    let opacity = 1;
                    let textDecorationLine: 'none' | 'line-through' = 'none';
                    let textDecorationStyle: 'solid' | 'double' = 'solid';
                    let textDecorationThickness: string | undefined = undefined;
                    let bgColor = `${categoryColor(p.category)}18`;
                    if (status === 'deactivated') {
                      // Grayed out, no decoration
                      color = '#666';
                      opacity = 0.55;
                      bgColor = '#1a1a1a';
                    } else if (status === 'derezzed') {
                      // Grayed + double strikethrough — distinct from deactivated
                      color = '#666';
                      opacity = 0.55;
                      bgColor = '#1a1a1a';
                      textDecorationLine = 'line-through';
                      textDecorationStyle = 'double';
                      textDecorationThickness = '2px';
                    } else if (status === 'destroyed') {
                      // Very dim + double strikethrough — most extreme
                      color = '#444';
                      opacity = 0.35;
                      bgColor = '#0d0d0d';
                      textDecorationLine = 'line-through';
                      textDecorationStyle = 'double';
                      textDecorationThickness = '2px';
                    }
                    // active falls through with default lit styling
                    return (
                      <span
                        key={i}
                        title={`${p.name} (${p.category}, ${status}${p.rez > 0 ? `, REZ ${p.rez}` : ''})`}
                        style={{
                          fontSize: '0.6rem',
                          padding: '1px 5px',
                          borderRadius: '3px',
                          backgroundColor: bgColor,
                          color,
                          fontWeight: 500,
                          opacity,
                          textDecorationLine,
                          textDecorationStyle,
                          textDecorationThickness,
                        }}
                      >
                        {p.name} {p.rez > 0 ? `R${p.rez}` : ''}
                      </span>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Active Programs — dnd5e_cyber string list */}
            {!hs.active_programs?.length && hs.program_slots_used?.length > 0 && (
              <div style={{ marginBottom: '4px' }}>
                <div style={{ fontSize: '0.65rem', color: '#666', marginBottom: '2px' }}>Programs</div>
                <div style={{ display: 'flex', gap: '3px', flexWrap: 'wrap' }}>
                  {hs.program_slots_used.map((p: string, i: number) => (
                    <span key={i} style={{ fontSize: '0.6rem', padding: '1px 5px', borderRadius: '3px', backgroundColor: '#00ff4118', color: '#00ff41', fontWeight: 500 }}>{p}</span>
                  ))}
                </div>
              </div>
            )}

            {/* ICE Status */}
            {iceEntries.length > 0 && (
              <div style={{ marginBottom: '4px' }}>
                <div style={{ fontSize: '0.65rem', color: '#666', marginBottom: '2px' }}>ICE</div>
                {iceEntries.map(([node, status]: [string, any]) => {
                  if (status && typeof status === 'object' && status.behavior) {
                    // CPRED structured ICE
                    const iceColor = status.status === 'active' ? '#ef4444' : status.status === 'bypassed' ? '#fbbf24' : '#666';
                    const rezPct = status.rez_max > 0 ? Math.min(100, (status.rez_current / status.rez_max) * 100) : 0;
                    return (
                      <div key={node} style={{ marginBottom: '3px' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.65rem', padding: '1px 0' }}>
                          <span style={{ color: '#999' }}>{node}: <span style={{ color: iceColor }}>{status.name} ({status.behavior})</span></span>
                          <span style={{ color: iceColor }}>{status.status === 'active' ? `${status.rez_current}/${status.rez_max}` : status.status}</span>
                        </div>
                        {status.status === 'active' && status.rez_max > 0 && (
                          <div style={{ height: '2px', backgroundColor: '#1a2a1a', borderRadius: '1px', overflow: 'hidden', marginTop: '1px' }}>
                            <div style={{ height: '100%', width: `${rezPct}%`, backgroundColor: iceColor, borderRadius: '1px', transition: 'width 0.3s' }} />
                          </div>
                        )}
                      </div>
                    );
                  }
                  // dnd5e_cyber string ICE
                  const statusStr = String(status).toLowerCase();
                  const iceColor = statusStr.includes('destroyed') || statusStr.includes('defeated') ? '#666' : statusStr.includes('active') ? '#ef4444' : '#fb923c';
                  return (
                    <div key={node} style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.65rem', padding: '1px 0' }}>
                      <span style={{ color: '#999' }}>{node}</span>
                      <span style={{ color: iceColor }}>{String(status)}</span>
                    </div>
                  );
                })}
              </div>
            )}

            {/* Trace Progress */}
            {hs.trace_progress != null && hs.trace_progress > 0 && (
              <div style={{ marginBottom: '4px' }}>
                {(() => { const traceMax = isCpred ? Math.max(1, 6 - (hs.sr || 3)) : (hs.sr || 3) * 2; const tracePct = traceMax > 0 ? Math.min(100, (hs.trace_progress / traceMax) * 100) : 0; return (<>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.68rem', color: '#999', marginBottom: '2px' }}>
                  <span style={{ color: '#fbbf24' }}>{'\u26A0'} Trace</span>
                  <span style={{ color: '#fbbf24', fontWeight: 600 }}>{hs.trace_progress}/{traceMax}</span>
                </div>
                <div style={{ height: '4px', backgroundColor: '#1a2a1a', borderRadius: '2px', overflow: 'hidden' }}>
                  <div style={{ height: '100%', width: `${tracePct}%`, backgroundColor: tracePct >= 75 ? '#ef4444' : '#fbbf24', borderRadius: '2px', transition: 'width 0.3s' }} />
                </div>
                </>); })()}
              </div>
            )}

            {/* Tar Stacks */}
            {hs.tar_stacks > 0 && (
              <div style={{ fontSize: '0.68rem', color: '#fb923c', marginBottom: isCpred && (hs.brain_damage ?? 0) > 0 ? '4px' : undefined }}>
                Tar Stacks: {hs.tar_stacks}
              </div>
            )}

            {/* Brain Damage (CPRED only) */}
            {isCpred && (hs.brain_damage ?? 0) > 0 && (
              <div style={{ fontSize: '0.68rem', color: '#ef4444' }}>
                Brain Damage: {hs.brain_damage}
              </div>
            )}

          </>
        )}

        {/* NET Map button — outside condensed block so it shows on mobile too */}
        {hs.system_map && (
          <button
            onClick={() => setShowNetMap(true)}
            style={{
              width: '100%', marginTop: '6px', padding: '4px',
              fontSize: '0.65rem', fontWeight: 700,
              color: '#00ff41', backgroundColor: '#00ff4112',
              border: '1px solid #00ff4130', borderRadius: '3px',
              cursor: 'pointer', fontFamily: 'monospace',
              letterSpacing: '2px',
            }}
          >
            MAP
          </button>
        )}
      </div>
    );
  };

  const renderShipCombatHud = (condensed?: boolean) => {
    const shipCombat = pipelineState?.ship_combat;
    if (!shipCombat) return null;

    const initOrder = shipCombat.initiative_order || [];
    const currentShip = shipCombat.current_ship;
    const currentRole = shipCombat.current_role;
    const env = shipCombat.environment || 'Open Space';
    const roleOrder = ['captain', 'sensors', 'pilot', 'gunner', 'engineer'];
    const roleIndex = currentRole ? roleOrder.indexOf(currentRole) : -1;

    return (
      <div style={{ padding: condensed ? '6px 8px' : '8px', borderBottom: '1px solid #1a1a3a', backgroundColor: '#0a0a2a' }}>
        <div style={{ fontSize: '0.65rem', color: '#64748b', marginBottom: '6px' }}>
          {env}
        </div>

        {initOrder.length > 0 && (
          <div style={{ marginBottom: '6px' }}>
            <div style={{ fontSize: '0.65rem', color: '#666', marginBottom: '3px' }}>Initiative</div>
            {initOrder.map((entry: any, i: number) => {
              const shipName = typeof entry === 'string' ? entry : entry.ship_name;
              const isActing = shipName === currentShip;
              const faction = typeof entry === 'object' ? entry.faction : null;
              const factionColor = faction === 'ally' ? '#38bdf8' : faction === 'enemy' ? '#ef4444' : '#94a3b8';
              const shipData = (pipelineState?.character_states || {})?.[shipName];
              const d = shipData?.data || shipData || {};
              let hullPct = 100, shieldPct = 100;
              for (const v of (d.vitals || [])) {
                if (v.label === 'Hull' && v.max > 0) hullPct = Math.round((v.current / v.max) * 100);
                if (v.label === 'Shields' && v.max > 0) shieldPct = Math.round((v.current / v.max) * 100);
              }
              return (
                <div key={i} style={{
                  display: 'flex', alignItems: 'center', gap: '6px',
                  padding: '3px 4px', marginBottom: '2px', borderRadius: '3px',
                  backgroundColor: isActing ? '#38bdf815' : 'transparent',
                  borderLeft: isActing ? '2px solid #38bdf8' : '2px solid transparent',
                }}>
                  <span style={{ fontSize: '0.68rem', color: factionColor, fontWeight: isActing ? 700 : 500, flex: 1 }}>
                    {shipName}
                  </span>
                  <div style={{ width: '30px', height: '3px', backgroundColor: '#1a1a3a', borderRadius: '1px', overflow: 'hidden' }}>
                    <div style={{ height: '100%', width: `${hullPct}%`, backgroundColor: hullPct > 50 ? '#94a3b8' : hullPct > 25 ? '#fb923c' : '#ef4444', borderRadius: '1px' }} />
                  </div>
                  <div style={{ width: '30px', height: '3px', backgroundColor: '#1a1a3a', borderRadius: '1px', overflow: 'hidden' }}>
                    <div style={{ height: '100%', width: `${shieldPct}%`, backgroundColor: '#38bdf8', borderRadius: '1px' }} />
                  </div>
                  {isActing && <span style={{ fontSize: '0.55rem', color: '#38bdf8', fontWeight: 700 }}>ACTING</span>}
                </div>
              );
            })}
          </div>
        )}

        {currentShip && (
          <div style={{ marginBottom: condensed ? '0' : '4px' }}>
            <div style={{ fontSize: '0.65rem', color: '#666', marginBottom: '3px' }}>Crew Phase</div>
            <div style={{ display: 'flex', gap: '2px' }}>
              {roleOrder.map((role, i) => {
                const isActive = role === currentRole;
                const isPast = roleIndex >= 0 && i < roleIndex;
                return (
                  <span key={role} style={{
                    fontSize: '0.55rem', padding: '1px 4px', borderRadius: '2px',
                    backgroundColor: isActive ? '#38bdf830' : isPast ? '#38bdf810' : '#1a1a3a',
                    color: isActive ? '#38bdf8' : isPast ? '#64748b' : '#333',
                    fontWeight: isActive ? 700 : 400,
                    textTransform: 'capitalize' as const,
                  }}>
                    {role.charAt(0).toUpperCase() + role.slice(1, 3)}
                  </span>
                );
              })}
            </div>
          </div>
        )}
      </div>
    );
  };

  // Render a single card
  const renderCard = (name: string, isActive?: boolean) => {
    const data = getCharData(name);
    const type = data.type || 'npc';
    const vitals = data.vitals || [];
    const conditions = data.conditions || [];
    const resources = (data.resources || []).slice(0, 2);
    const summary = data.summary || '';
    const charClass = data.subclass ? `${data.class || ''} (${data.subclass})`.trim() : (data.class || '');
    const level = data.level;
    const barVitals = vitals.filter((v: any) => 'current' in v && 'max' in v);
    const flatVitals = vitals.filter((v: any) => 'value' in v && !('current' in v && 'max' in v));
    const shipCombat = state?.ship_combat;
    return (
      <div
        key={name}
        onClick={() => { setSelectedCharacter(name); setShowCharacterSheet(true); }}
        style={{
          padding: '8px 10px', margin: '0 0 4px', borderRadius: '6px',
          backgroundColor: '#1e1e3a', cursor: 'pointer',
          border: isActive ? '1.5px solid #4a4ae8' : '1px solid #2a2a4e',
          transition: 'background 0.15s',
          ...(isActive ? { animation: 'pulse 2s ease-in-out infinite' } : {}),
        }}
        onMouseEnter={e => (e.currentTarget.style.backgroundColor = '#2a2a4e')}
        onMouseLeave={e => (e.currentTarget.style.backgroundColor = '#1e1e3a')}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: barVitals.length || charClass || level || flatVitals.length || summary ? '4px' : 0 }}>
          <span style={{ fontWeight: 600, fontSize: '0.82rem', color: '#e0e0e0' }}>{name}</span>
          <div style={{ display: 'flex', gap: '4px', alignItems: 'center' }}>
            {isActive && <span style={{ fontSize: '0.6rem', fontWeight: 700, color: '#4a4ae8', letterSpacing: '0.05em' }}>ACTING</span>}
            <span style={{
              fontSize: '0.6rem', padding: '1px 5px', borderRadius: '3px', fontWeight: 600,
              backgroundColor: typeBadgeColor(type) + '22', color: typeBadgeColor(type),
            }}>{type.toUpperCase()}</span>
          </div>
        </div>
        {(charClass || level || flatVitals.length > 0) && (
          flatVitals.length > 1 ? (
            <>
              {(charClass || level) && (
                <div style={{ marginBottom: '2px' }}>
                  <span style={{ fontSize: '0.68rem', color: '#888' }}>{[charClass, level != null ? `Lv ${level}` : ''].filter(Boolean).join(' · ')}</span>
                </div>
              )}
              <div style={{ display: 'flex', gap: '6px', marginBottom: '2px' }}>
                {flatVitals.map((v: any, i: number) => (
                  <div key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: '2px' }}>
                    <span style={{ fontSize: '0.68rem', color: '#999' }}>{v.label}: </span>
                    <span style={{ fontSize: '0.72rem', fontWeight: 600, color: '#94a3b8', backgroundColor: '#2a2a4e', padding: '0 4px', borderRadius: '3px' }}>{v.value}</span>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '2px' }}>
              <span style={{ fontSize: '0.68rem', color: '#888' }}>{[charClass, level != null ? `Lv ${level}` : ''].filter(Boolean).join(' · ') || ''}</span>
              <div style={{ display: 'flex', gap: '6px' }}>
                {flatVitals.map((v: any, i: number) => (
                  <div key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: '2px' }}>
                    <span style={{ fontSize: '0.68rem', color: '#999' }}>{v.label}: </span>
                    <span style={{ fontSize: '0.72rem', fontWeight: 600, color: '#94a3b8', backgroundColor: '#2a2a4e', padding: '0 4px', borderRadius: '3px' }}>{v.value}</span>
                  </div>
                ))}
              </div>
            </div>
          )
        )}
        {barVitals.map((v: any, i: number) => (
          <div key={i} style={{ marginBottom: '2px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.68rem', color: '#999', marginBottom: '1px' }}>
              <span>{v.label}</span><span>{v.current}/{v.max}</span>
            </div>
            <div style={{ height: '4px', backgroundColor: '#2a2a4e', borderRadius: '2px', overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${v.max > 0 ? Math.max(0, Math.min(100, (v.current / v.max) * 100)) : 0}%`, backgroundColor: vitalColor(v.current, v.max, v.label), borderRadius: '2px', transition: 'width 0.3s' }} />
            </div>
          </div>
        ))}
        {barVitals.length === 0 && !charClass && !level && flatVitals.length === 0 && summary && (
          <div style={{ fontSize: '0.7rem', color: '#888', lineHeight: 1.3, overflow: 'hidden', textOverflow: 'ellipsis', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' as any }}>{summary}</div>
        )}
        {resources.length > 0 && (
          <div style={{ display: 'flex', gap: '8px', marginTop: '3px', flexWrap: 'wrap' }}>
            {resources.map((r: any, i: number) => (
              <div key={i} style={{ fontSize: '0.65rem', color: '#999' }}>
                <span>{r.label}: </span>
                {Array.from({ length: r.max || 0 }, (_, pi) => (
                  <span key={pi} style={{ color: pi < (r.current || 0) ? resourceColor(r.label) : '#3a3a5e', fontSize: '0.75rem' }}>●</span>
                ))}
              </div>
            ))}
          </div>
        )}
        {chatGameSystem === 'cpred' && (() => {
          const er = state?.game_state?.edgerunners?.[name];
          if (!er?.armor) return null;
          const head = er.armor.head;
          const body = er.armor.body;
          if (head == null && body == null) return null;
          return (
            <div style={{ fontSize: '0.65rem', color: '#999', marginTop: '3px' }}>
              <span>Armor: </span>
              <span style={{ color: '#94a3b8', fontWeight: 500 }}>H {head ?? 0}</span>
              <span style={{ color: '#444', margin: '0 5px' }}>·</span>
              <span style={{ color: '#94a3b8', fontWeight: 500 }}>B {body ?? 0}</span>
            </div>
          );
        })()}
        {conditions.length > 0 && (
          <div style={{ display: 'flex', gap: '3px', marginTop: '3px', flexWrap: 'wrap' }}>
            {conditions
              .filter((c: string) => !(shipCombat && type === 'ship' && c.includes(':')))
              .map((c: string, i: number) => (
              <span key={i} style={{ fontSize: '0.6rem', padding: '1px 5px', borderRadius: '8px', backgroundColor: condColor(c) + '22', color: condColor(c), fontWeight: 500 }}>{c}</span>
            ))}
          </div>
        )}
        {shipCombat && type === 'ship' && conditions.length > 0 && (
          <div style={{ marginTop: '4px', display: 'flex', gap: '3px', flexWrap: 'wrap' }}>
            {conditions.filter((c: string) => c.includes(':')).map((c: string, i: number) => {
              const tier = c.toLowerCase();
              const color = tier.includes('destroyed') ? '#ef4444' : tier.includes('damaged') ? '#fb923c' : tier.includes('strained') ? '#fbbf24' : '#94a3b8';
              return (
                <span key={`sys-${i}`} style={{ fontSize: '0.55rem', padding: '1px 4px', borderRadius: '2px', backgroundColor: `${color}18`, color }}>
                  {c}
                </span>
              );
            })}
          </div>
        )}
      </div>
    );
  };

  // Use pipelineState if available, otherwise empty fallback for project chats
  const state = pipelineState || { character_states: {}, scene_state: {}, npc_memories: {}, combat: null, ship_combat: null };

  // --- Desktop Right Panel ---
  const renderDesktopPanel = () => {
    if (isMobile) return null;

    const cs = state.character_states || {};
    const scene = state.scene_state || {};
    const combat = state.combat;
    const shipCombat = state.ship_combat;
    const netCombat = state.net_combat;
    const isNetCombat = netCombat?.active;
    const sexScene = state.sex_scene;
    const ledger = state.callback_ledger;
    const pcsPresent = scene.pcs_present || [];
    const npcsPresent = scene.npcs_present || [];
    const charCount = new Set([...pcsPresent, ...npcsPresent]).size;

    if (!rightPanelOpen) {
      return (
        <div style={{
          width: '40px', minWidth: '40px', backgroundColor: '#16162a',
          borderLeft: '1px solid #333', display: 'flex', flexDirection: 'column' as const,
          alignItems: 'center', paddingTop: '16px',
        }}>
          <button
            onClick={() => setRightPanelOpen(true)}
            style={{ background: 'none', border: 'none', fontSize: '1.2rem', color: '#888', cursor: 'pointer', padding: '4px 8px' }}
            title="Open character panel (Ctrl+])"
          >{'\u00AB'}</button>
          {isNetCombat && (
            <span style={{ fontSize: '0.6rem', fontWeight: 700, color: '#f59e0b', marginTop: '8px', backgroundColor: '#f59e0b18', borderRadius: '8px', padding: '1px 5px' }}>NC</span>
          )}
          {hackState?.active && !isNetCombat && (
            <span style={{ fontSize: '0.65rem', fontWeight: 700, color: '#00ff41', marginTop: '8px', backgroundColor: '#00ff4118', borderRadius: '8px', padding: '1px 5px' }}>H</span>
          )}
          {sexScene?.npcs && !hackState?.active && !isNetCombat && !shipCombat && (
            <span style={{ fontSize: '0.6rem', fontWeight: 700, color: '#e88fa5', marginTop: '8px', backgroundColor: '#e88fa518', borderRadius: '8px', padding: '1px 5px' }}>XXX</span>
          )}
          {shipCombat && !hackState?.active && !isNetCombat && !sexScene?.npcs && (
            <span style={{ fontSize: '0.6rem', fontWeight: 700, color: '#38bdf8', marginTop: '8px', backgroundColor: '#38bdf818', borderRadius: '8px', padding: '1px 5px' }}>SC</span>
          )}
          {charCount > 0 && !hackState?.active && !isNetCombat && !shipCombat && !sexScene?.npcs && (
            <span style={{ fontSize: '0.65rem', color: '#888', marginTop: '8px', backgroundColor: '#2a2a4e', borderRadius: '8px', padding: '1px 5px' }}>{charCount}</span>
          )}
        </div>
      );
    }

    return (
      <div style={{
        width: '280px', minWidth: '280px', backgroundColor: '#16162a',
        borderLeft: '1px solid #333', display: 'flex', flexDirection: 'column' as const,
        overflow: 'hidden',
      }}>
        {/* Callbacks button */}
        {ledger && (ledger.open?.length > 0 || ledger.recently_resolved?.length > 0) && (
          <div style={{ padding: '8px 12px 0' }}>
            <button
              onClick={() => setShowCallbacksModal(true)}
              style={{ width: '100%', padding: '6px', fontSize: '0.75rem', color: '#888', backgroundColor: '#1e1e3a', border: '1px solid #2a2a4e', borderRadius: '4px', cursor: 'pointer' }}
              onMouseEnter={e => { e.currentTarget.style.backgroundColor = '#2a2a4e'; e.currentTarget.style.color = '#ccc'; }}
              onMouseLeave={e => { e.currentTarget.style.backgroundColor = '#1e1e3a'; e.currentTarget.style.color = '#888'; }}
            >
              Callbacks ({ledger.open?.length || 0} open)
            </button>
          </div>
        )}
        {/* Panel header */}
        <div style={{
          padding: '12px 12px 8px', borderBottom: `1px solid ${sexScene?.npcs ? '#3a0a2a' : isNetCombat ? '#3a2a0a' : hackState?.active ? '#0a3a0a' : '#333'}`, display: 'flex',
          justifyContent: 'space-between', alignItems: 'center',
          backgroundColor: sexScene?.npcs ? '#2a0a1a' : isNetCombat ? '#1a1a0a' : hackState?.active ? '#0a1f0a' : shipCombat ? '#0a0a2a' : combat ? '#2a1a1a' : 'transparent',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span style={{ fontWeight: 600, fontSize: '0.85rem', color: sexScene?.npcs ? '#e88fa5' : isNetCombat ? '#f59e0b' : hackState?.active ? '#00ff41' : shipCombat ? '#38bdf8' : '#ccc' }}>
              {sexScene?.npcs ? `${(sexScene.npcs as string[]).join(', ')}` : isNetCombat ? `NET Combat \u2014 Round ${combat?.round || '?'}` : hackState?.active ? `${hackState.cycles_remaining !== undefined ? 'NET' : 'Matrix'} \u2014 ${hackState.target_system || 'Unknown'}` : shipCombat ? `Ship Combat \u2014 Round ${shipCombat.round || '?'}` : combat ? `Combat \u2014 Round ${combat.round || '?'}` : 'Scene'}
            </span>
            {sexScene?.npcs && (
              <span style={{ fontSize: '0.6rem', fontWeight: 700, color: '#e88fa5', backgroundColor: '#e88fa518', padding: '1px 5px', borderRadius: '3px' }}>XXX</span>
            )}
            {isNetCombat && !sexScene?.npcs && (
              <span style={{ fontSize: '0.6rem', fontWeight: 700, color: '#f59e0b', backgroundColor: '#f59e0b18', padding: '1px 5px', borderRadius: '3px' }}>NET COMBAT</span>
            )}
            {hackState?.active && !isNetCombat && !sexScene?.npcs && (
              <span style={{ fontSize: '0.6rem', fontWeight: 700, color: '#00ff41', backgroundColor: '#00ff4118', padding: '1px 5px', borderRadius: '3px' }}>HACK</span>
            )}
            {!hackState?.active && !isNetCombat && !combat && shipCombat && !sexScene?.npcs && (
              <span style={{ fontSize: '0.6rem', fontWeight: 700, color: '#38bdf8', backgroundColor: '#38bdf818', padding: '1px 5px', borderRadius: '3px' }}>SHIP COMBAT</span>
            )}
            {!hackState?.active && !isNetCombat && combat && !sexScene?.npcs && (
              <span style={{ fontSize: '0.6rem', fontWeight: 700, color: '#ef4444', backgroundColor: '#ef444422', padding: '1px 5px', borderRadius: '3px' }}>COMBAT</span>
            )}
          </div>
          <button
            onClick={() => setRightPanelOpen(false)}
            style={{ background: 'none', border: 'none', fontSize: '1.2rem', color: '#888', cursor: 'pointer', padding: '2px 6px' }}
            title="Close character panel (Ctrl+])"
          >{'\u00BB'}</button>
        </div>

        {/* Hack HUD / Net Combat HUD */}
        {isNetCombat && !netCombat.net_complete ? renderHackHud(false, netCombat) : !isNetCombat ? renderHackHud() : null}
        {!isNetCombat && renderShipCombatHud()}

        {/* Panel body */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '8px', scrollbarWidth: 'thin' as any }}>
          {isNetCombat ? (
            // Net combat: show initiative order when combat theater still active
            !netCombat.combat_complete && combat ? (
              <>
                {(combat.initiative_order || []).map((name: string) => renderCard(name, combat.current_turn === name))}
                {(combat.initiative_order || []).length === 0 && (
                  <div style={{ fontSize: '0.75rem', color: '#666', textAlign: 'center', padding: '20px 0' }}>No initiative order set</div>
                )}
              </>
            ) : netCombat.combat_complete && !netCombat.net_complete ? (
              <div style={{ fontSize: '0.75rem', color: '#666', textAlign: 'center', padding: '20px 0' }}>Meatspace combat resolved — NET operations continue</div>
            ) : (
              <div style={{ fontSize: '0.75rem', color: '#666', textAlign: 'center', padding: '20px 0' }}>NET resolved — meatspace combat continues</div>
            )
          ) : shipCombat ? (
            <>
              {(shipCombat.initiative_order || []).map((entry: any, i: number) => {
                const shipName = typeof entry === 'string' ? entry : entry.ship_name;
                return shipName ? renderCard(shipName, shipName === shipCombat.current_ship) : <div key={`missing-${i}`} />;
              })}
              {(shipCombat.initiative_order || []).length === 0 && (
                <div style={{ fontSize: '0.75rem', color: '#666', textAlign: 'center', padding: '20px 0' }}>No ship initiative order set</div>
              )}
            </>
          ) : combat ? (
            // Combat mode: initiative order
            <>
              {(combat.initiative_order || []).map((name: string) => renderCard(name, combat.current_turn === name))}
              {(combat.initiative_order || []).length === 0 && (
                <div style={{ fontSize: '0.75rem', color: '#666', textAlign: 'center', padding: '20px 0' }}>No initiative order set</div>
              )}
            </>
          ) : (
            // Normal mode: PCs then NPCs
            <>
              {pcsPresent.length > 0 && (
                <>
                  <div style={{ fontSize: '0.65rem', fontWeight: 600, color: '#666', textTransform: 'uppercase' as const, letterSpacing: '0.08em', marginBottom: '4px', padding: '0 2px' }}>PCs</div>
                  {pcsPresent.map((name: string) => renderCard(name))}
                </>
              )}
              {npcsPresent.length > 0 && (
                <>
                  <div style={{ fontSize: '0.65rem', fontWeight: 600, color: '#666', textTransform: 'uppercase' as const, letterSpacing: '0.08em', marginBottom: '4px', marginTop: '8px', padding: '0 2px' }}>NPCs</div>
                  {npcsPresent.map((name: string) => renderCard(name))}
                </>
              )}
              {pcsPresent.length === 0 && npcsPresent.length === 0 && (
                <div style={{ fontSize: '0.75rem', color: '#666', textAlign: 'center', padding: '20px 0' }}>No characters in scene</div>
              )}
            </>
          )}
        </div>

        {/* Panel footer */}
        {Object.keys(cs).length + Object.keys(state.npc_memories || {}).length > 0 && (
          <div style={{ padding: '8px 12px', borderTop: '1px solid #333' }}>
            <button
              onClick={() => setShowAllCharactersModal(true)}
              style={{ width: '100%', padding: '6px', fontSize: '0.75rem', color: '#888', backgroundColor: '#1e1e3a', border: '1px solid #2a2a4e', borderRadius: '4px', cursor: 'pointer' }}
              onMouseEnter={e => { e.currentTarget.style.backgroundColor = '#2a2a4e'; e.currentTarget.style.color = '#ccc'; }}
              onMouseLeave={e => { e.currentTarget.style.backgroundColor = '#1e1e3a'; e.currentTarget.style.color = '#888'; }}
            >
              View All ({new Set([...Object.keys(cs), ...Object.keys(state.npc_memories || {})]).size})
            </button>
          </div>
        )}
      </div>
    );
  };

  // --- Mobile Bottom Sheet ---
  const renderMobileBottomSheet = () => {
    if (!isMobile) return null;

    const scene = state.scene_state || {};
    const pcsPresent = scene.pcs_present || [];
    const npcsPresent = scene.npcs_present || [];
    const allPresent = pcsPresent.concat(npcsPresent).filter((v: string, i: number, a: string[]) => a.indexOf(v) === i);
    const shipCombat = state.ship_combat;
    const netCombatM = state.net_combat;
    const isNetCombatM = netCombatM?.active;
    const sexSceneM = state.sex_scene;
    if (allPresent.length === 0 && !hackState?.active && !isNetCombatM && !shipCombat && !sexSceneM?.npcs) return null;

    return (
      <>
      {mobileBottomSheetOpen && (
        <div
          onClick={() => setMobileBottomSheetOpen(false)}
          style={{
            position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
            backgroundColor: 'rgba(0, 0, 0, 0.3)',
            zIndex: 1499,
          }}
        />
      )}
      <div style={{
        position: 'fixed', bottom: 0, left: 0, right: 0,
        height: mobileBottomSheetOpen ? '55vh' : '34px',
        backgroundColor: sexSceneM?.npcs ? '#2a0a1a' : isNetCombatM ? '#1a1a0a' : hackState?.active ? '#0a1a0a' : shipCombat ? '#0a0a2a' : '#16162a', borderTop: `1px solid ${sexSceneM?.npcs ? '#3a0a2a' : isNetCombatM ? '#3a2a0a' : hackState?.active ? '#0a3a0a' : shipCombat ? '#1a1a3a' : '#333'}`,
        zIndex: 1500, transition: 'height 0.25s ease',
        display: 'flex', flexDirection: 'column' as const,
      }}>
        {/* Tab handle */}
        <div
          onClick={() => setMobileBottomSheetOpen(!mobileBottomSheetOpen)}
          style={{ textAlign: 'center', padding: '8px', fontSize: '0.75rem', color: sexSceneM?.npcs ? '#e88fa5' : isNetCombatM ? '#f59e0b' : hackState?.active ? '#00ff41' : shipCombat ? '#38bdf8' : '#888', cursor: 'pointer', flexShrink: 0, borderBottom: mobileBottomSheetOpen ? `1px solid ${sexSceneM?.npcs ? '#3a0a2a' : isNetCombatM ? '#3a2a0a' : hackState?.active ? '#0a3a0a' : shipCombat ? '#1a1a3a' : '#333'}` : 'none' }}
        >
          <div style={{ width: '32px', height: '3px', backgroundColor: sexSceneM?.npcs ? '#e88fa5' : isNetCombatM ? '#f59e0b' : hackState?.active ? '#00ff41' : shipCombat ? '#38bdf8' : '#555', borderRadius: '2px', margin: '0 auto 4px' }} />
          {sexSceneM?.npcs && <span style={{ fontSize: '0.6rem', fontWeight: 700, color: '#e88fa5', backgroundColor: '#e88fa518', padding: '1px 5px', borderRadius: '3px', marginRight: '6px' }}>XXX</span>}
          {isNetCombatM && !sexSceneM?.npcs && <span style={{ fontSize: '0.6rem', fontWeight: 700, color: '#f59e0b', backgroundColor: '#f59e0b18', padding: '1px 5px', borderRadius: '3px', marginRight: '6px' }}>NET COMBAT</span>}
          {hackState?.active && !isNetCombatM && !sexSceneM?.npcs && <span style={{ fontSize: '0.6rem', fontWeight: 700, color: '#00ff41', backgroundColor: '#00ff4118', padding: '1px 5px', borderRadius: '3px', marginRight: '6px' }}>HACK</span>}
          {!hackState?.active && !isNetCombatM && shipCombat && !sexSceneM?.npcs && <span style={{ fontSize: '0.6rem', fontWeight: 700, color: '#38bdf8', backgroundColor: '#38bdf818', padding: '1px 5px', borderRadius: '3px', marginRight: '6px' }}>SHIP COMBAT</span>}
          {mobileBottomSheetOpen ? '\u25BC' : '\u25B2'} {sexSceneM?.npcs ? `${(sexSceneM.npcs as string[]).join(', ')}` : isNetCombatM ? `NET Combat \u2014 Round ${state.combat?.round || '?'}` : hackState?.active ? `${hackState.cycles_remaining !== undefined ? 'NET' : 'Matrix'} \u2014 ${hackState.target_system || 'Unknown'}` : shipCombat ? `Ship Combat \u2014 Round ${shipCombat.round || '?'}` : `${allPresent.length} characters in scene`}
        </div>
        {/* Scrollable card list */}
        {mobileBottomSheetOpen && (
          <>
            {isNetCombatM && !netCombatM.net_complete ? renderHackHud(true, netCombatM) : !isNetCombatM ? renderHackHud(true) : null}
            {!isNetCombatM && renderShipCombatHud(true)}
            <div style={{ flex: 1, overflowY: 'auto', padding: '8px' }}>
              {isNetCombatM ? (
                !netCombatM.combat_complete && state.combat ? (
                  (state.combat.initiative_order || []).map((name: string) => renderCard(name, state.combat.current_turn === name))
                ) : (
                  <div style={{ fontSize: '0.75rem', color: '#666', textAlign: 'center', padding: '20px 0' }}>
                    {netCombatM.combat_complete && !netCombatM.net_complete
                      ? 'Meatspace combat resolved — NET operations continue'
                      : 'NET resolved — meatspace combat continues'}
                  </div>
                )
              ) : shipCombat ? (
                (shipCombat.initiative_order || []).map((entry: any, i: number) => {
                  const shipName = typeof entry === 'string' ? entry : entry.ship_name;
                  return shipName ? renderCard(shipName, shipName === shipCombat.current_ship) : <div key={`mship-${i}`} />;
                })
              ) : (
                allPresent.map((name: string) => renderCard(name))
              )}
            </div>
            <div style={{ padding: '8px 12px', borderTop: '1px solid #333', flexShrink: 0, display: 'flex', flexDirection: 'column' as const, gap: '6px' }}>
              {(() => {
                const ledger = state.callback_ledger;
                return ledger && (ledger.open?.length > 0 || ledger.recently_resolved?.length > 0) ? (
                  <button
                    onClick={() => setShowCallbacksModal(true)}
                    style={{ width: '100%', padding: '6px', fontSize: '0.75rem', color: '#888', backgroundColor: '#1e1e3a', border: '1px solid #2a2a4e', borderRadius: '4px', cursor: 'pointer' }}
                  >
                    Callbacks ({ledger.open?.length || 0} open)
                  </button>
                ) : null;
              })()}
              {Object.keys(state.character_states || {}).length + Object.keys(state.npc_memories || {}).length > 0 && (
                <button
                  onClick={() => setShowAllCharactersModal(true)}
                  style={{ width: '100%', padding: '6px', fontSize: '0.75rem', color: '#888', backgroundColor: '#1e1e3a', border: '1px solid #2a2a4e', borderRadius: '4px', cursor: 'pointer' }}
                >
                  View All ({new Set([...Object.keys(state.character_states || {}), ...Object.keys(state.npc_memories || {})]).size})
                </button>
              )}
            </div>
          </>
        )}
      </div>
      </>
    );
  };

  // --- Character Sheet Modal ---
  const renderCharacterSheetModal = () => {
    if (!(showCharacterSheet && selectedCharacter)) return null;

    const cs = state.character_states || {};
    const entry = cs[selectedCharacter];
    const data = entry?.data || entry || {};
    const vitals = data.vitals || [];
    const resources = data.resources || [];
    const conditions = data.conditions || [];
    const type = data.type || 'npc';
    const summary = data.summary || '';
    const gameState = state.game_state || {};
    const hudFunds = state.hud_state?.funds?.[selectedCharacter];
    const memories = (state.npc_memories || {})[selectedCharacter];

    // Find character section in .md or .yaml sheet, tracking source file extension
    const { sheetSection, sheetIsYaml } = (() => {
      if (!characterSheetFiles.length) return { sheetSection: '', sheetIsYaml: false };
      const charLower = selectedCharacter.toLowerCase();
      for (const file of characterSheetFiles) {
        const ext = file.name.split('.').pop()?.toLowerCase() || '';
        const isYaml = ext === 'yaml' || ext === 'yml';
        if (isYaml) {
          // YAML: split on banner blocks (# ===...title...# ===)
          const yamlPattern = /^# ={3,}\n#\s+(.+)\n# ={3,}/gm;
          const yamlSections: { name: string; start: number; }[] = [];
          let m;
          while ((m = yamlPattern.exec(file.content)) !== null) {
            yamlSections.push({ name: m[1].trim(), start: m.index });
          }
          for (let i = 0; i < yamlSections.length; i++) {
            if (yamlSections[i].name.toLowerCase().includes(charLower)) {
              const start = yamlSections[i].start;
              const end = i + 1 < yamlSections.length ? yamlSections[i + 1].start : file.content.length;
              return { sheetSection: file.content.slice(start, end).trim(), sheetIsYaml: true };
            }
          }
        } else {
          // Markdown: split on ## or ### headers (sheets often group under
          // level-2 headers like "## Party NPCs" with each character at
          // level 3 — `### Delphi — Fixer 4`). Match either level.
          const sections = file.content.split(/(?=^#{2,3} )/m);
          if (sections.length > 1) {
            const match = sections.find(s => {
              const heading = s.split('\n')[0].replace(/^#{2,3} /, '').trim();
              const headingLower = heading.toLowerCase();
              // Heading text often includes role/title after the name —
              // e.g. "Delphi — Fixer 4". Match the leading name token first
              // so we don't accidentally hit a section like "Party NPCs"
              // when the character name appears inside its body text.
              const leadName = heading.split(/[\s—\-,(]/)[0].trim().toLowerCase();
              return leadName === charLower || headingLower === charLower || headingLower.includes(charLower);
            });
            if (match) return { sheetSection: match, sheetIsYaml: false };
          }
        }
      }
      return { sheetSection: '', sheetIsYaml: false };
    })();

    // Game-specific state sections
    const renderGameState = () => {
      const gs = chatGameSystem || 'dnd5e';
      const parts: React.ReactNode[] = [];

      // D&D 5E / D&D 5E Cyber: Relationships
      if ((gs === 'dnd5e' || gs === 'dnd5e_cyber') && gameState.relationships && selectedCharacter) {
        const rel = gameState.relationships[selectedCharacter];
        if (type === 'pc') {
          // PC view: show all NPC relationships with tier info
          const rels = Object.entries(gameState.relationships).filter(([_, r]: [string, any]) => r && typeof r === 'object');
          if (rels.length > 0) {
            parts.push(
              <div key="rels" style={{ marginTop: '12px' }}>
                <div style={{ fontSize: '0.75rem', fontWeight: 600, color: '#888', marginBottom: '6px', textTransform: 'uppercase' as const, letterSpacing: '0.05em' }}>Relationships</div>
                {rels.map(([npc, r]: [string, any]) => {
                  const rs = r.rs || 0;
                  const [rsLabel, rsBonus] = rsTier(rs);
                  const tierColor = rs >= 40 ? '#a78bfa' : rs >= 10 ? '#4ade80' : rs >= -9 ? '#94a3b8' : '#ef4444';
                  const romsVal = r.roms || 0;
                  const [romsLabel, romsBonus] = romsTier(romsVal);
                  const allBonuses = [rsBonus, romsBonus].filter(Boolean).join(' · ');
                  return (
                    <div key={npc} style={{ padding: '4px 0', borderBottom: '1px solid #2a2a4e' }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.78rem' }}>
                        <span style={{ color: '#ccc' }}>{npc}</span>
                        <span style={{ color: tierColor, fontWeight: 500 }}>RS {rs} ({rsLabel}){romsVal > 0 ? ` | \u2665${romsVal} (${romsLabel})` : ''}</span>
                      </div>
                      {allBonuses && <div style={{ fontSize: '0.65rem', color: '#666', textAlign: 'right' as const }}>{allBonuses}</div>}
                    </div>
                  );
                })}
              </div>
            );
          }
        } else if (rel && typeof rel === 'object') {
          // NPC view: show only this NPC's scores with tier info
          const rs = rel.rs || 0;
          const [rsLabel, rsBonus] = rsTier(rs);
          const tierColor = rs >= 40 ? '#a78bfa' : rs >= 10 ? '#4ade80' : rs >= -9 ? '#94a3b8' : '#ef4444';
          const romsVal = rel.roms || 0;
          const [romsLabel, romsBonus] = romsTier(romsVal);
          const allBonuses = [rsBonus, romsBonus].filter(Boolean).join(' · ');
          parts.push(
            <div key="rels" style={{ marginTop: '12px' }}>
              <div style={{ fontSize: '0.75rem', fontWeight: 600, color: '#888', marginBottom: '6px', textTransform: 'uppercase' as const, letterSpacing: '0.05em' }}>Relationships</div>
              <div style={{ display: 'flex', justifyContent: 'space-between', padding: '3px 0', fontSize: '0.78rem' }}>
                <span style={{ color: '#ccc' }}>Relationship Score</span>
                <span style={{ color: tierColor, fontWeight: 500 }}>{rs} — {rsLabel}</span>
              </div>
              {romsVal > 0 && (
                <div style={{ display: 'flex', justifyContent: 'space-between', padding: '3px 0', fontSize: '0.78rem' }}>
                  <span style={{ color: '#ccc' }}>Romance Score</span>
                  <span style={{ color: '#f472b6', fontWeight: 500 }}>{romsVal} — {romsLabel}</span>
                </div>
              )}
              {allBonuses && <div style={{ fontSize: '0.65rem', color: '#666', marginTop: '2px' }}>{allBonuses}</div>}
              {/* Inter-NPC relationships nested under this NPC */}
              {rel.npc_relationships && typeof rel.npc_relationships === 'object' && Object.keys(rel.npc_relationships).length > 0 && (
                <>
                  <div style={{ fontSize: '0.68rem', fontWeight: 600, color: '#666', marginTop: '8px', marginBottom: '4px', textTransform: 'uppercase' as const, letterSpacing: '0.05em' }}>Inter-NPC Relationships</div>
                  {Object.entries(rel.npc_relationships).map(([npc, r]: [string, any]) => {
                    const npcRs = r?.rs || 0;
                    const [npcRsLabel, npcRsBonus] = rsTier(npcRs);
                    const npcColor = npcRs >= 40 ? '#a78bfa' : npcRs >= 10 ? '#4ade80' : npcRs >= -9 ? '#94a3b8' : '#ef4444';
                    const npcRoms = r?.roms || 0;
                    const [npcRomsLabel, npcRomsBonus] = romsTier(npcRoms);
                    const npcAllBonuses = [npcRsBonus, npcRomsBonus].filter(Boolean).join(' · ');
                    return (
                      <div key={npc} style={{ padding: '3px 0', borderBottom: '1px solid #2a2a4e' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.75rem' }}>
                          <span style={{ color: '#aaa' }}>{npc}</span>
                          <span style={{ color: npcColor, fontWeight: 500 }}>RS {npcRs} ({npcRsLabel}){npcRoms > 0 ? ` | \u2665${npcRoms} (${npcRomsLabel})` : ''}</span>
                        </div>
                        {npcAllBonuses && <div style={{ fontSize: '0.6rem', color: '#555', textAlign: 'right' as const }}>{npcAllBonuses}</div>}
                      </div>
                    );
                  })}
                </>
              )}
            </div>
          );
        }
        // Factions (PC only)
        if (type === 'pc') {
          const factions = Object.entries(gameState.factions || {});
          if (factions.length > 0) {
            parts.push(
              <div key="factions" style={{ marginTop: '12px' }}>
                <div style={{ fontSize: '0.75rem', fontWeight: 600, color: '#888', marginBottom: '6px', textTransform: 'uppercase' as const, letterSpacing: '0.05em' }}>Factions</div>
                {factions.map(([name, f]: [string, any]) => (
                  <div key={name} style={{ display: 'flex', justifyContent: 'space-between', padding: '3px 0', fontSize: '0.78rem', borderBottom: '1px solid #2a2a4e' }}>
                    <span style={{ color: '#ccc' }}>{name}</span>
                    <span style={{ color: '#94a3b8' }}>FR {f.fr || 0} — {f.tier || '?'}</span>
                  </div>
                ))}
              </div>
            );
          }
        }
      }

      // D&D 5E Cyber: Ship (only on ship card, only if hull/shields data exists)
      if (gs === 'dnd5e_cyber' && gameState.ship && type === 'ship' && (gameState.ship.hull || gameState.ship.shields)) {
        const ship = gameState.ship;
        const hull = ship.hull;
        const shields = ship.shields;
        parts.push(
          <div key="ship" style={{ marginTop: '12px' }}>
            <div style={{ fontSize: '0.75rem', fontWeight: 600, color: '#888', marginBottom: '6px', textTransform: 'uppercase' as const, letterSpacing: '0.05em' }}>Ship</div>
            <div style={{ display: 'flex', gap: '10px' }}>
              {hull && (
                <div style={{ flex: 1 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.72rem', color: '#999', marginBottom: '1px' }}><span>Hull</span><span>{hull.current}/{hull.max}</span></div>
                  <div style={{ height: '5px', backgroundColor: '#2a2a4e', borderRadius: '2px', overflow: 'hidden' }}>
                    <div style={{ height: '100%', width: `${hull.max > 0 ? (hull.current / hull.max) * 100 : 0}%`, backgroundColor: '#94a3b8', borderRadius: '2px' }} />
                  </div>
                </div>
              )}
              {shields && (
                <div style={{ flex: 1 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.72rem', color: '#999', marginBottom: '1px' }}><span>Shields</span><span>{shields.current}/{shields.max}</span></div>
                  <div style={{ height: '5px', backgroundColor: '#2a2a4e', borderRadius: '2px', overflow: 'hidden' }}>
                    <div style={{ height: '100%', width: `${shields.max > 0 ? (shields.current / shields.max) * 100 : 0}%`, backgroundColor: '#38bdf8', borderRadius: '2px' }} />
                  </div>
                </div>
              )}
            </div>
          </div>
        );
      }

      // CoC 7E: Investigator state
      if (gs === 'coc7e' && gameState.investigators) {
        const inv = gameState.investigators[selectedCharacter];
        if (inv) {
          parts.push(
            <div key="inv" style={{ marginTop: '12px' }}>
              <div style={{ fontSize: '0.75rem', fontWeight: 600, color: '#888', marginBottom: '6px', textTransform: 'uppercase' as const, letterSpacing: '0.05em' }}>Investigator State</div>
              {inv.san != null && inv.san_max != null && (
                <div style={{ marginBottom: '4px' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.72rem', color: '#999' }}><span>SAN</span><span>{inv.san}/{inv.san_max}</span></div>
                  <div style={{ height: '5px', backgroundColor: '#2a2a4e', borderRadius: '2px', overflow: 'hidden' }}>
                    <div style={{ height: '100%', width: `${inv.san_max > 0 ? (inv.san / inv.san_max) * 100 : 0}%`, backgroundColor: inv.san_max > 0 ? ((inv.san / inv.san_max) > 0.6 ? '#60a5fa' : (inv.san / inv.san_max) > 0.3 ? '#a78bfa' : '#7c3aed') : '#7c3aed', borderRadius: '2px' }} />
                  </div>
                </div>
              )}
              {inv.luck != null && <div style={{ fontSize: '0.72rem', color: '#fbbf24', marginTop: '2px' }}>Luck: {inv.luck}</div>}
              {inv.mythos_pct != null && <div style={{ fontSize: '0.72rem', color: '#a78bfa', marginTop: '2px' }}>Cthulhu Mythos: {inv.mythos_pct}%</div>}
              {inv.bonds && Object.keys(inv.bonds).length > 0 && (
                <div style={{ marginTop: '6px' }}>
                  <div style={{ fontSize: '0.68rem', color: '#666' }}>Bonds:</div>
                  {Object.entries(inv.bonds).map(([b, v]: [string, any]) => (
                    <div key={b} style={{ fontSize: '0.72rem', color: '#ccc', paddingLeft: '8px' }}>{b}: {v}</div>
                  ))}
                </div>
              )}
              {inv.phobias && inv.phobias.length > 0 && <div style={{ fontSize: '0.72rem', color: '#ef4444', marginTop: '4px' }}>Phobias: {inv.phobias.join(', ')}</div>}
              {inv.manias && inv.manias.length > 0 && <div style={{ fontSize: '0.72rem', color: '#fb923c', marginTop: '2px' }}>Manias: {inv.manias.join(', ')}</div>}
            </div>
          );
        }
      }

      // SR6E: Runner state
      if (gs === 'sr6e' && gameState.runners) {
        const runner = gameState.runners[selectedCharacter];
        if (runner) {
          parts.push(
            <div key="runner" style={{ marginTop: '12px' }}>
              <div style={{ fontSize: '0.75rem', fontWeight: 600, color: '#888', marginBottom: '6px', textTransform: 'uppercase' as const, letterSpacing: '0.05em' }}>Runner State</div>
              {runner.edge != null && runner.edge_max != null && (
                <div style={{ fontSize: '0.72rem', color: '#999', marginBottom: '4px' }}>
                  Edge: {Array.from({ length: runner.edge_max }, (_, i) => (
                    <span key={i} style={{ color: i < runner.edge ? '#fbbf24' : '#3a3a5e', fontSize: '0.85rem' }}>{i < runner.edge ? '\u25CF' : '\u25CB'}</span>
                  ))}
                </div>
              )}
              {runner.essence != null && <div style={{ fontSize: '0.72rem', color: '#a78bfa' }}>Essence: {runner.essence}</div>}
              {runner.nuyen != null && <div style={{ fontSize: '0.72rem', color: '#fbbf24' }}>Nuyen: \u00A5{runner.nuyen}</div>}
            </div>
          );
        }
      }

      // Cyberpunk RED: Edgerunner state
      if (gs === 'cpred' && gameState.edgerunners) {
        const er = gameState.edgerunners[selectedCharacter];
        if (er) {
          parts.push(
            <div key="er" style={{ marginTop: '12px' }}>
              <div style={{ fontSize: '0.75rem', fontWeight: 600, color: '#888', marginBottom: '6px', textTransform: 'uppercase' as const, letterSpacing: '0.05em' }}>Edgerunner State</div>
              {er.humanity != null && er.humanity_max != null && (
                <div style={{ marginBottom: '4px' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.72rem', color: '#999' }}><span>Humanity</span><span>{er.humanity}/{er.humanity_max}</span></div>
                  <div style={{ height: '5px', backgroundColor: '#2a2a4e', borderRadius: '2px', overflow: 'hidden' }}>
                    <div style={{ height: '100%', width: `${er.humanity_max > 0 ? (er.humanity / er.humanity_max) * 100 : 0}%`, backgroundColor: er.humanity_max > 0 ? ((er.humanity / er.humanity_max) > 0.6 ? '#2dd4bf' : (er.humanity / er.humanity_max) > 0.3 ? '#fbbf24' : '#ef4444') : '#ef4444', borderRadius: '2px' }} />
                  </div>
                </div>
              )}
              {er.luck != null && er.luck_max != null && (
                <div style={{ fontSize: '0.72rem', color: '#999', marginBottom: '2px' }}>
                  Luck: {Array.from({ length: er.luck_max }, (_, i) => (
                    <span key={i} style={{ color: i < er.luck ? '#fbbf24' : '#3a3a5e', fontSize: '0.85rem' }}>{i < er.luck ? '\u25CF' : '\u25CB'}</span>
                  ))}
                </div>
              )}
              {er.armor && (er.armor.head != null || er.armor.body != null) && (
                <div style={{ fontSize: '0.72rem', color: '#999', marginBottom: '2px' }}>
                  Armor: <span style={{ color: '#ccc' }}>Head SP {er.armor.head ?? 0}</span> {'·'} <span style={{ color: '#ccc' }}>Body SP {er.armor.body ?? 0}</span>
                </div>
              )}
              {er.eurobucks != null && <div style={{ fontSize: '0.72rem', color: '#fbbf24' }}>Eurobucks: €${er.eurobucks}</div>}
              {er.critical_injuries && er.critical_injuries.length > 0 && (
                <div style={{ marginTop: '4px' }}>
                  {er.critical_injuries.map((ci: string, i: number) => (
                    <div key={i} style={{ fontSize: '0.7rem', color: '#ef4444' }}>{'\u2022'} {ci}</div>
                  ))}
                </div>
              )}
              {/* Weapons */}
              {er.weapons && er.weapons.length > 0 && (
                <div style={{ marginTop: '6px' }}>
                  <div style={{ fontSize: '0.68rem', color: '#666', marginBottom: '2px' }}>Weapons</div>
                  {er.weapons.map((w: any, i: number) => {
                    const loaded = w.loaded_type;
                    const isMelee = w.type === 'melee';
                    const ammoStr = isMelee ? '' : `, ${w.current_ammo ?? '?'}/${w.max_ammo ?? '?'}`;
                    const dryTag = !isMelee && (w.current_ammo === 0)
                      ? <span style={{ color: '#ef4444', marginLeft: '4px' }}>[DRY]</span>
                      : null;
                    const typeTag = !isMelee && loaded && loaded !== 'basic'
                      ? <span style={{ color: '#fbbf24', marginLeft: '4px' }}>[{String(loaded).toUpperCase()}]</span>
                      : null;
                    return (
                      <div key={i} style={{ fontSize: '0.72rem', color: '#ccc', paddingLeft: '4px' }}>
                        {w.name || '?'} <span style={{ color: '#888' }}>({w.damage || '?'}{ammoStr})</span>{typeTag}{dryTag}
                      </div>
                    );
                  })}
                </div>
              )}
              {/* Ammo Reserves */}
              {er.ammo_pool && Object.keys(er.ammo_pool).length > 0 && (
                <div style={{ marginTop: '4px' }}>
                  <div style={{ fontSize: '0.68rem', color: '#666', marginBottom: '2px' }}>Ammo Reserves</div>
                  {Object.entries(er.ammo_pool).map(([caliber, types]: [string, any]) => (
                    <div key={caliber} style={{ fontSize: '0.72rem', color: '#ccc', paddingLeft: '4px' }}>
                      {caliber}: <span style={{ color: '#888' }}>
                        {Object.entries(types || {}).map(([t, n]) => `${t}:${n}`).join(', ')}
                      </span>
                    </div>
                  ))}
                </div>
              )}
              {/* Gear */}
              {er.gear && Object.keys(er.gear).length > 0 && (
                <div style={{ marginTop: '4px' }}>
                  <div style={{ fontSize: '0.68rem', color: '#666', marginBottom: '2px' }}>Gear</div>
                  <div style={{ fontSize: '0.72rem', color: '#ccc', paddingLeft: '4px' }}>
                    {Object.entries(er.gear).map(([item, n]) => `${n}× ${item}`).join(', ')}
                  </div>
                </div>
              )}
              {/* Outfit */}
              {er.outfit && (er.outfit.description || er.outfit.style_rating != null) && (
                <div style={{ marginTop: '4px' }}>
                  <div style={{ fontSize: '0.68rem', color: '#666', marginBottom: '2px' }}>Outfit</div>
                  <div style={{ fontSize: '0.72rem', color: '#ccc', paddingLeft: '4px' }}>
                    {er.outfit.description || '—'}
                    {er.outfit.style_rating != null && (
                      <span style={{ color: '#fbbf24', marginLeft: '4px' }}>
                        (Style {er.outfit.style_rating > 0 ? `+${er.outfit.style_rating}` : er.outfit.style_rating})
                      </span>
                    )}
                  </div>
                </div>
              )}
              {/* Cyberware */}
              {er.cyberware_effects && er.cyberware_effects.length > 0 && (
                <div style={{ marginTop: '4px' }}>
                  <div style={{ fontSize: '0.68rem', color: '#666', marginBottom: '2px' }}>Cyberware</div>
                  <div style={{ fontSize: '0.72rem', color: '#a78bfa' }}>{er.cyberware_effects.join(', ')}</div>
                </div>
              )}
              {/* Cyberdeck */}
              {er.cyberdeck && (
                <div style={{ marginTop: '4px' }}>
                  <div style={{ fontSize: '0.72rem', color: '#22d3ee' }}>
                    Cyberdeck: {er.cyberdeck.tier || '?'} | {((er.deck_slots || er.programs || []).filter((s: any) => s != null)).length}/{(er.deck_slots || er.programs || []).length || er.cyberdeck.slots || 0} slots | {er.cyberdeck.cycles ?? 0} cycles
                  </div>
                </div>
              )}
              {/* Programs */}
              {er.cyberdeck && (() => {
                const slots = er.deck_slots || er.programs || [];
                const programs = slots.filter((s: any) => s && typeof s === 'object' && (s.type === 'program' || (!s.type && !s._continuation_of)) );
                const hardware = slots.filter((s: any) => s && typeof s === 'object' && s.type === 'hardware');
                return (programs.length > 0 || hardware.length > 0) ? (
                  <div style={{ marginTop: '2px' }}>
                    {programs.length > 0 && (<>
                      <div style={{ fontSize: '0.68rem', color: '#666', marginBottom: '2px' }}>Programs</div>
                      {programs.map((p: any, i: number) => (
                        <div key={i} style={{ fontSize: '0.72rem', color: '#ccc', paddingLeft: '4px' }}>
                          {p.name || '?'} <span style={{ color: '#888' }}>({p.category || '?'})</span>
                          {p.status && p.status !== 'stored' && <span style={{ color: '#fbbf24', marginLeft: '4px' }}>[{p.status}]</span>}
                        </div>
                      ))}
                    </>)}
                    {hardware.length > 0 && (<>
                      <div style={{ fontSize: '0.68rem', color: '#666', marginBottom: '2px', marginTop: '2px' }}>Hardware</div>
                      {hardware.map((h: any, i: number) => (
                        <div key={i} style={{ fontSize: '0.72rem', color: '#ccc', paddingLeft: '4px' }}>
                          {h.name || '?'} <span style={{ color: '#888' }}>({h.slots_used || 1} slot{(h.slots_used || 1) > 1 ? 's' : ''})</span>
                        </div>
                      ))}
                    </>)}
                  </div>
                ) : null;
              })()}
            </div>
          );
        }
      }

      return parts.length > 0 ? <>{parts}</> : null;
    };

    return (
      <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: 'rgba(0,0,0,0.7)', zIndex: 2100, display: 'flex', justifyContent: 'center', alignItems: 'center' }} onClick={() => { setShowCharacterSheet(false); setSelectedCharacter(null); }}>
        <div style={{ backgroundColor: '#1a1a2e', borderRadius: '12px', maxWidth: '700px', width: '90%', maxHeight: '85vh', overflow: 'auto', padding: '24px', border: '1px solid #333', scrollbarWidth: 'thin' as any }} onClick={e => e.stopPropagation()}>
          {/* Header */}
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
              <h3 style={{ margin: 0, fontSize: '1.1rem', color: '#e0e0e0' }}>{selectedCharacter}</h3>
              <span style={{
                fontSize: '0.65rem', padding: '2px 7px', borderRadius: '3px', fontWeight: 600,
                backgroundColor: typeBadgeColor(type) + '22', color: typeBadgeColor(type),
              }}>{type.toUpperCase()}</span>
            </div>
            <button onClick={() => { setShowCharacterSheet(false); setSelectedCharacter(null); }} style={{ background: 'none', border: 'none', fontSize: '1.2rem', color: '#888', cursor: 'pointer' }}>{'\u2715'}</button>
          </div>

          {/* Class & Level */}
          {(data.class || data.subclass || data.level != null) && (
            <div style={{ fontSize: '0.85rem', color: '#999', marginBottom: '12px', marginTop: '-8px' }}>
              {[data.subclass ? `${data.class || ''} (${data.subclass})`.trim() : data.class, data.level != null ? `Level ${data.level}` : ''].filter(Boolean).join(' · ')}
            </div>
          )}

          {/* Live State */}
          {(vitals.length > 0 || resources.length > 0 || conditions.length > 0 || summary) && (
            <div style={{ marginBottom: '16px' }}>
              <div style={{ fontSize: '0.75rem', fontWeight: 600, color: '#888', marginBottom: '8px', textTransform: 'uppercase' as const, letterSpacing: '0.05em', borderBottom: '1px solid #2a2a4e', paddingBottom: '4px' }}>Live State</div>
              {vitals.map((v: any, i: number) => (
                'current' in v && 'max' in v ? (
                  <div key={i} style={{ marginBottom: '6px' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.78rem', color: '#bbb', marginBottom: '2px' }}>
                      <span>{v.label}</span><span style={{ fontWeight: 500 }}>{v.current}/{v.max}</span>
                    </div>
                    <div style={{ height: '6px', backgroundColor: '#2a2a4e', borderRadius: '3px', overflow: 'hidden' }}>
                      <div style={{ height: '100%', width: `${v.max > 0 ? Math.max(0, Math.min(100, (v.current / v.max) * 100)) : 0}%`, backgroundColor: vitalColor(v.current, v.max, v.label), borderRadius: '3px', transition: 'width 0.3s' }} />
                    </div>
                  </div>
                ) : 'value' in v ? (
                  <div key={i} style={{ marginBottom: '4px' }}>
                    <span style={{ fontSize: '0.78rem', color: '#bbb' }}>{v.label}: </span>
                    <span style={{ fontSize: '0.85rem', fontWeight: 600, color: '#94a3b8', backgroundColor: '#2a2a4e', padding: '1px 6px', borderRadius: '3px' }}>{v.value}</span>
                  </div>
                ) : null
              ))}
              {resources.map((r: any, i: number) => (
                <div key={i} style={{ marginBottom: '4px' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.75rem', color: '#999', marginBottom: '1px' }}>
                    <span>{r.label}</span><span>{r.current}/{r.max}</span>
                  </div>
                  <div style={{ height: '5px', backgroundColor: '#2a2a4e', borderRadius: '2px', overflow: 'hidden' }}>
                    <div style={{ height: '100%', width: `${Math.max(0, Math.min(100, ((r.current || 0) / (r.max || 1)) * 100))}%`, backgroundColor: resourceColor(r.label), borderRadius: '2px' }} />
                  </div>
                </div>
              ))}
              {conditions.length > 0 && (
                <div style={{ display: 'flex', gap: '4px', marginTop: '6px', flexWrap: 'wrap' }}>
                  {conditions.map((c: string, i: number) => (
                    <span key={i} style={{ fontSize: '0.7rem', padding: '2px 8px', borderRadius: '10px', backgroundColor: condColor(c) + '22', color: condColor(c), fontWeight: 500 }}>{c}</span>
                  ))}
                </div>
              )}
              {summary && chatGameSystem !== 'cpred' && <div style={{ fontSize: '0.78rem', color: '#999', marginTop: '6px', lineHeight: 1.4 }}>{summary}</div>}
            </div>
          )}

          {/* Game-specific state */}
          {renderGameState()}

          {/* Funds/Credits: Ship shows all, others show own */}
          {(() => {
            const allFunds = state.hud_state?.funds || {};
            const gs = chatGameSystem || (gameState.ship ? 'dnd5e_cyber' : 'dnd5e');
            const fundsLabel = ({ dnd5e_cyber: 'Credits', sr6e: 'Nuyen', cpred: 'Eurobucks' } as Record<string, string>)[gs] || 'Funds';
            if (type === 'ship') {
              const entries = Object.entries(allFunds);
              if (entries.length > 0) return (
                <div style={{ marginTop: '12px' }}>
                  <div style={{ fontSize: '0.75rem', fontWeight: 600, color: '#888', marginBottom: '4px', textTransform: 'uppercase' as const, letterSpacing: '0.05em', borderBottom: '1px solid #2a2a4e', paddingBottom: '4px' }}>{fundsLabel}</div>
                  {entries.map(([k, v]: [string, any]) => (
                    <div key={k} style={{ fontSize: '0.82rem', color: '#fbbf24', display: 'flex', justifyContent: 'space-between', padding: '2px 0' }}>
                      <span>{k}</span><span>{typeof v === 'string' ? v : v}</span>
                    </div>
                  ))}
                </div>
              );
            } else if (hudFunds) {
              return (
                <div style={{ marginTop: '12px' }}>
                  <div style={{ fontSize: '0.75rem', fontWeight: 600, color: '#888', marginBottom: '4px', textTransform: 'uppercase' as const, letterSpacing: '0.05em', borderBottom: '1px solid #2a2a4e', paddingBottom: '4px' }}>{fundsLabel}</div>
                  <div style={{ fontSize: '0.85rem', color: '#fbbf24' }}>{typeof hudFunds === 'string' ? hudFunds : hudFunds}</div>
                </div>
              );
            }
            return null;
          })()}

          {/* NPC Memories button */}
          {memories && memories.length > 0 && (
            <div style={{ marginTop: '12px' }}>
              <button
                onClick={() => setShowNpcMemories(selectedCharacter)}
                style={{ padding: '6px 12px', fontSize: '0.78rem', color: '#888', backgroundColor: '#1e1e3a', border: '1px solid #2a2a4e', borderRadius: '4px', cursor: 'pointer' }}
                onMouseEnter={e => { e.currentTarget.style.backgroundColor = '#2a2a4e'; e.currentTarget.style.color = '#ccc'; }}
                onMouseLeave={e => { e.currentTarget.style.backgroundColor = '#1e1e3a'; e.currentTarget.style.color = '#888'; }}
              >
                View Memories ({memories.length})
              </button>
            </div>
          )}

          {/* Character sheet (collapsed, rendered as markdown or styled YAML).
              "Raw" toggle shows the source through YamlHighlighted regardless of
              file extension — useful when the sheet is .md and the user wants to
              see/copy the underlying source with syntax coloring. */}
          {sheetSection && (
            <details style={{ marginTop: '16px', borderTop: '1px solid #2a2a4e', paddingTop: '8px' }}>
              <summary style={{ fontSize: '0.78rem', color: '#888', cursor: 'pointer', padding: '4px 0', userSelect: 'none' }}>Full Character Sheet</summary>
              {(() => {
                // Strip the header line(s) — ##/### heading for md, # === banner for yaml
                let body = sheetSection;
                if (/^#{2,3} /.test(body)) {
                  body = body.split('\n').slice(1).join('\n').trim();
                } else if (body.startsWith('# ===')) {
                  body = body.replace(/^# ={3,}\n#\s+.+\n# ={3,}\n*/, '').trim();
                }
                const showRaw = sheetRawView || sheetIsYaml;
                return (
                  <>
                    {!sheetIsYaml && (
                      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '6px' }}>
                        <button
                          onClick={(e) => { e.preventDefault(); setSheetRawView(v => !v); }}
                          style={{
                            fontSize: '0.7rem', padding: '2px 8px', borderRadius: '3px',
                            border: '1px solid #2a2a4e', backgroundColor: sheetRawView ? '#2a2a4e' : '#1e1e3a',
                            color: sheetRawView ? '#a78bfa' : '#888', cursor: 'pointer', userSelect: 'none',
                          }}
                          title={sheetRawView ? 'Show rendered markdown' : 'Show raw syntax-highlighted source'}
                        >
                          {sheetRawView ? 'Rendered' : 'Raw'}
                        </button>
                      </div>
                    )}
                    {showRaw ? (
                      <YamlHighlighted content={body} />
                    ) : (
                      <div className="messageContent" style={{ fontSize: '0.78rem', color: '#ccc', lineHeight: 1.5, marginTop: '8px' }}>
                        <ReactMarkdown>{body}</ReactMarkdown>
                      </div>
                    )}
                  </>
                );
              })()}
            </details>
          )}
        </div>
      </div>
    );
  };

  // --- All Characters Modal ---
  const renderAllCharactersModal = () => {
    if (!showAllCharactersModal) return null;

    const cs = state.character_states || {};
    const npcMem = state.npc_memories || {};
    const scene = state.scene_state || {};
    const inScene = new Set([...(scene.pcs_present || []), ...(scene.npcs_present || [])]);
    const allNamesArr = Object.keys(cs).concat(Object.keys(npcMem)).filter((v, i, a) => a.indexOf(v) === i);
    const inSceneNames = allNamesArr.filter(n => inScene.has(n));
    const otherNames = allNamesArr.filter(n => !inScene.has(n));

    const renderMiniCard = (name: string) => {
      const entry = cs[name];
      const data = entry?.data || entry || {};
      const type = data.type || 'npc';
      const memCount = npcMem[name]?.length || 0;
      return (
        <div
          key={name}
          onClick={() => { setSelectedCharacter(name); setShowCharacterSheet(true); }}
          style={{
            padding: '8px 12px', borderRadius: '6px', backgroundColor: '#1e1e3a',
            border: '1px solid #2a2a4e', cursor: 'pointer', display: 'flex',
            justifyContent: 'space-between', alignItems: 'center',
          }}
          onMouseEnter={e => (e.currentTarget.style.backgroundColor = '#2a2a4e')}
          onMouseLeave={e => (e.currentTarget.style.backgroundColor = '#1e1e3a')}
        >
          <span style={{ fontWeight: 500, fontSize: '0.82rem', color: '#e0e0e0' }}>{name}</span>
          <div style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
            {memCount > 0 && <span style={{ fontSize: '0.6rem', color: '#888' }}>{memCount} memories</span>}
            <span style={{
              fontSize: '0.6rem', padding: '1px 5px', borderRadius: '3px', fontWeight: 600,
              backgroundColor: typeBadgeColor(type) + '22', color: typeBadgeColor(type),
            }}>{type.toUpperCase()}</span>
          </div>
        </div>
      );
    };

    return (
      <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: 'rgba(0,0,0,0.7)', zIndex: 2000, display: 'flex', justifyContent: 'center', alignItems: 'center' }} onClick={() => setShowAllCharactersModal(false)}>
        <div style={{ backgroundColor: '#1a1a2e', borderRadius: '12px', maxWidth: '900px', width: '90%', maxHeight: '85vh', overflow: 'auto', padding: '24px', border: '1px solid #333', scrollbarWidth: 'thin' as any }} onClick={e => e.stopPropagation()}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h3 style={{ margin: 0, fontSize: '1.1rem', color: '#e0e0e0' }}>All Characters ({allNamesArr.length})</h3>
            <button onClick={() => setShowAllCharactersModal(false)} style={{ background: 'none', border: 'none', fontSize: '1.2rem', color: '#888', cursor: 'pointer' }}>{'\u2715'}</button>
          </div>
          {inSceneNames.length > 0 && (
            <>
              <div style={{ fontSize: '0.7rem', fontWeight: 600, color: '#666', textTransform: 'uppercase' as const, letterSpacing: '0.08em', marginBottom: '6px' }}>In Scene</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', marginBottom: '16px' }}>
                {inSceneNames.map(renderMiniCard)}
              </div>
            </>
          )}
          {otherNames.length > 0 && (
            <>
              <div style={{ fontSize: '0.7rem', fontWeight: 600, color: '#666', textTransform: 'uppercase' as const, letterSpacing: '0.08em', marginBottom: '6px' }}>Other Characters</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                {otherNames.map(renderMiniCard)}
              </div>
            </>
          )}
        </div>
      </div>
    );
  };

  // --- NPC Memories Modal ---
  const renderNpcMemoriesModal = () => {
    if (!showNpcMemories) return null;

    const memories = (state.npc_memories || {})[showNpcMemories] || [];
    const sorted = [...memories].sort((a: any, b: any) => (b.impact || 0) - (a.impact || 0) || (b.turn_created || 0) - (a.turn_created || 0));
    const memBorderColor = (impact: number) => impact >= 4 ? '#fbbf24' : impact >= 3 ? '#3b82f6' : '#4a4a6e';

    return (
      <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: 'rgba(0,0,0,0.5)', zIndex: 2100, display: 'flex', justifyContent: 'center', alignItems: 'center' }} onClick={() => setShowNpcMemories(null)}>
        <div style={{ backgroundColor: '#1a1a2e', borderRadius: '12px', maxWidth: '600px', width: '90%', maxHeight: '80vh', overflow: 'auto', padding: '24px', border: '1px solid #333', scrollbarWidth: 'thin' as any }} onClick={e => e.stopPropagation()}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h3 style={{ margin: 0, fontSize: '1.1rem', color: '#e0e0e0' }}>{showNpcMemories} — Memories ({memories.length})</h3>
            <button onClick={() => setShowNpcMemories(null)} style={{ background: 'none', border: 'none', fontSize: '1.2rem', color: '#888', cursor: 'pointer' }}>{'\u2715'}</button>
          </div>
          {sorted.map((m: any, i: number) => (
            <div key={i} style={{
              padding: '10px 12px', marginBottom: '8px', borderRadius: '6px',
              backgroundColor: '#1e1e3a', borderLeft: `3px solid ${memBorderColor(m.impact || 0)}`,
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
                <span style={{ fontSize: '0.7rem', color: '#888' }}>{m.date || `Turn ${m.turn_created || '?'}`}</span>
                <span style={{ fontSize: '0.7rem', color: '#fbbf24' }}>{'\u2605'.repeat(Math.min(5, m.impact || 0))}{'\u2606'.repeat(Math.max(0, 5 - Math.min(5, m.impact || 0)))}</span>
              </div>
              <div style={{ fontSize: '0.82rem', color: '#ccc', lineHeight: 1.4 }}>{m.text || m.memory || ''}</div>
              {m.quote && <div style={{ fontSize: '0.78rem', color: '#999', fontStyle: 'italic', marginTop: '4px' }}>"{m.quote}"</div>}
            </div>
          ))}
          {memories.length === 0 && (
            <div style={{ fontSize: '0.82rem', color: '#666', textAlign: 'center', padding: '20px 0' }}>No memories recorded</div>
          )}
        </div>
      </div>
    );
  };

  // --- Callbacks Modal ---
  const renderCallbacksModal = () => {
    if (!showCallbacksModal) return null;
    const ledger = state.callback_ledger || { open: [], recently_resolved: [] };
    const turnCounter = state.turn_counter || 0;
    const openCbs = ledger.open || [];
    const resolvedCbs = ledger.recently_resolved || [];

    const renderCallbackCard = (cb: any, isResolved: boolean, accent: string) => (
      <div key={cb.id} style={{
        padding: '10px 12px', marginBottom: '8px',
        backgroundColor: '#16162a', borderRadius: '6px',
        borderLeft: `3px solid ${accent}`,
      }}>
        {cb.source_npc && (
          <div style={{ fontSize: '0.7rem', color: '#888', marginBottom: '4px' }}>
            {cb.source_npc}
          </div>
        )}
        <div style={{ fontSize: '0.8rem', color: '#d0d0d0', lineHeight: 1.4 }}>
          {cb.original_text}
        </div>
        <div style={{ fontSize: '0.65rem', color: '#666', marginTop: '6px' }}>
          {isResolved ? `Resolved \u2014 turn ${cb.resolved_turn}` : `Turn ${cb.created_turn}`}
          {!isResolved && turnCounter - cb.created_turn >= 20 && ' \u26A0 aging'}
        </div>
      </div>
    );

    return (
      <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: 'rgba(0,0,0,0.5)', zIndex: 2100, display: 'flex', justifyContent: 'center', alignItems: 'center' }} onClick={() => setShowCallbacksModal(false)}>
        <div style={{ backgroundColor: '#1a1a2e', borderRadius: '12px', maxWidth: '600px', width: '90%', maxHeight: '80vh', overflow: 'auto', padding: '24px', border: '1px solid #333', scrollbarWidth: 'thin' as any }} onClick={e => e.stopPropagation()}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h3 style={{ margin: 0, fontSize: '1.1rem', color: '#e0e0e0' }}>Callbacks</h3>
            <button onClick={() => setShowCallbacksModal(false)} style={{ background: 'none', border: 'none', fontSize: '1.2rem', color: '#888', cursor: 'pointer' }}>{'\u2715'}</button>
          </div>
          {openCbs.length > 0 && (
            <>
              <div style={{ fontSize: '0.7rem', fontWeight: 600, color: '#666', textTransform: 'uppercase' as const, letterSpacing: '0.08em', marginBottom: '8px' }}>Open ({openCbs.length})</div>
              {openCbs.map((cb: any) => renderCallbackCard(cb, false, '#f59e0b'))}
            </>
          )}
          {resolvedCbs.length > 0 && (
            <>
              <div style={{ fontSize: '0.7rem', fontWeight: 600, color: '#666', textTransform: 'uppercase' as const, letterSpacing: '0.08em', marginBottom: '8px', marginTop: openCbs.length > 0 ? '12px' : '0' }}>Recently Resolved ({resolvedCbs.length})</div>
              {resolvedCbs.map((cb: any) => renderCallbackCard(cb, true, '#22c55e'))}
            </>
          )}
          {openCbs.length === 0 && resolvedCbs.length === 0 && (
            <div style={{ fontSize: '0.82rem', color: '#666', textAlign: 'center', padding: '20px 0' }}>No callbacks</div>
          )}
        </div>
      </div>
    );
  };

  // Derive net map source — hack mode or net combat
  const netMapSource = hackState?.system_map ? hackState : (pipelineState?.net_combat?.system_map ? pipelineState.net_combat : null);

  // Close net map when hack mode ends
  useEffect(() => {
    if (!netMapSource) setShowNetMap(false);
  }, [netMapSource]);

  return (
    <>
      {renderDesktopPanel()}
      {renderMobileBottomSheet()}
      {renderCharacterSheetModal()}
      {renderAllCharactersModal()}
      {renderNpcMemoriesModal()}
      {renderCallbacksModal()}
      {showNetMap && netMapSource?.system_map && (
        <NetMapPopup
          systemMap={netMapSource.system_map}
          revealedNodes={netMapSource.revealed_nodes || netMapSource.nodes_visited || []}
          currentNode={netMapSource.current_node || ''}
          nodesVisited={netMapSource.nodes_visited || []}
          iceStatus={netMapSource.ice_status || {}}
          sr={netMapSource.sr ?? netMapSource.system_map.sr}
          onClose={() => setShowNetMap(false)}
        />
      )}
    </>
  );
}
