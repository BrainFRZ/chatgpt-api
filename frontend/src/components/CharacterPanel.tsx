import React from 'react';
import ReactMarkdown from 'react-markdown';
import { styles } from '../styles';

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
  characterSheetMd: string;
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
  characterSheetMd,
}: CharacterPanelProps) {

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
    if (!entry) return { type: 'npc' };
    return entry.data || entry || {};
  };

  // Render a single card
  const renderCard = (name: string, isActive?: boolean) => {
    const data = getCharData(name);
    const type = data.type || 'npc';
    const vitals = data.vitals || [];
    const conditions = data.conditions || [];
    const resources = (data.resources || []).slice(0, 2);
    const summary = data.summary || '';
    const charClass = data.class || '';
    const level = data.level;
    const barVitals = vitals.filter((v: any) => 'current' in v && 'max' in v);
    const flatVitals = vitals.filter((v: any) => 'value' in v && !('current' in v && 'max' in v));
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
                  <span key={pi} style={{ color: pi < (r.current || 0) ? resourceColor(r.label) : '#3a3a5e', fontSize: '0.75rem' }}>{pi < (r.current || 0) ? '●' : '○'}</span>
                ))}
              </div>
            ))}
          </div>
        )}
        {conditions.length > 0 && (
          <div style={{ display: 'flex', gap: '3px', marginTop: '3px', flexWrap: 'wrap' }}>
            {conditions.map((c: string, i: number) => (
              <span key={i} style={{ fontSize: '0.6rem', padding: '1px 5px', borderRadius: '8px', backgroundColor: condColor(c) + '22', color: condColor(c), fontWeight: 500 }}>{c}</span>
            ))}
          </div>
        )}
      </div>
    );
  };

  // Use pipelineState if available, otherwise empty fallback for project chats
  const state = pipelineState || { character_states: {}, scene_state: {}, npc_memories: {}, combat: null };

  // --- Desktop Right Panel ---
  const renderDesktopPanel = () => {
    if (isMobile) return null;

    const cs = state.character_states || {};
    const scene = state.scene_state || {};
    const combat = state.combat;
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
          {charCount > 0 && (
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
        {/* Panel header */}
        <div style={{
          padding: '12px 12px 8px', borderBottom: '1px solid #333', display: 'flex',
          justifyContent: 'space-between', alignItems: 'center',
          backgroundColor: combat ? '#2a1a1a' : 'transparent',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span style={{ fontWeight: 600, fontSize: '0.85rem', color: '#ccc' }}>
              {combat ? `Combat \u2014 Round ${combat.round || '?'}` : 'Scene'}
            </span>
            {combat && (
              <span style={{ fontSize: '0.6rem', fontWeight: 700, color: '#ef4444', backgroundColor: '#ef444422', padding: '1px 5px', borderRadius: '3px' }}>COMBAT</span>
            )}
          </div>
          <button
            onClick={() => setRightPanelOpen(false)}
            style={{ background: 'none', border: 'none', fontSize: '1.2rem', color: '#888', cursor: 'pointer', padding: '2px 6px' }}
            title="Close character panel (Ctrl+])"
          >{'\u00BB'}</button>
        </div>

        {/* Panel body */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '8px', scrollbarWidth: 'thin' as any }}>
          {combat ? (
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
    if (allPresent.length === 0) return null;

    return (
      <div style={{
        position: 'fixed', bottom: 0, left: 0, right: 0,
        height: mobileBottomSheetOpen ? '55vh' : '34px',
        backgroundColor: '#16162a', borderTop: '1px solid #333',
        zIndex: 1500, transition: 'height 0.25s ease',
        display: 'flex', flexDirection: 'column' as const,
      }}>
        {/* Tab handle */}
        <div
          onClick={() => setMobileBottomSheetOpen(!mobileBottomSheetOpen)}
          style={{ textAlign: 'center', padding: '8px', fontSize: '0.75rem', color: '#888', cursor: 'pointer', flexShrink: 0, borderBottom: mobileBottomSheetOpen ? '1px solid #333' : 'none' }}
        >
          <div style={{ width: '32px', height: '3px', backgroundColor: '#555', borderRadius: '2px', margin: '0 auto 4px' }} />
          {mobileBottomSheetOpen ? '\u25BC' : '\u25B2'} {allPresent.length} characters in scene
        </div>
        {/* Scrollable card list */}
        {mobileBottomSheetOpen && (
          <div style={{ flex: 1, overflowY: 'auto', padding: '8px' }}>
            {allPresent.map((name: string) => renderCard(name))}
          </div>
        )}
      </div>
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

    // Find character section in .md or .yaml sheet
    const sheetSection = (() => {
      if (!characterSheetMd) return '';
      const charLower = selectedCharacter.toLowerCase();
      // Try markdown ## headers first
      let sections = characterSheetMd.split(/(?=^## )/m);
      if (sections.length > 1) {
        const match = sections.find(s => {
          const heading = s.split('\n')[0].replace(/^## /, '').trim();
          return heading.toLowerCase() === charLower || heading.toLowerCase().includes(charLower);
        });
        if (match) return match;
      }
      // Try YAML-style: split on banner blocks (# ===...title...# ===)
      const yamlPattern = /^# ={3,}\n#\s+(.+)\n# ={3,}/gm;
      const yamlSections: { name: string; start: number; }[] = [];
      let m;
      while ((m = yamlPattern.exec(characterSheetMd)) !== null) {
        yamlSections.push({ name: m[1].trim(), start: m.index });
      }
      if (yamlSections.length > 0) {
        for (let i = 0; i < yamlSections.length; i++) {
          if (yamlSections[i].name.toLowerCase().includes(charLower)) {
            const start = yamlSections[i].start;
            const end = i + 1 < yamlSections.length ? yamlSections[i + 1].start : characterSheetMd.length;
            return characterSheetMd.slice(start, end).trim();
          }
        }
      }
      return '';
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

      // D&D 5E Cyber: Ship (only on ship card)
      if (gs === 'dnd5e_cyber' && gameState.ship && type === 'ship') {
        const ship = gameState.ship;
        parts.push(
          <div key="ship" style={{ marginTop: '12px' }}>
            <div style={{ fontSize: '0.75rem', fontWeight: 600, color: '#888', marginBottom: '6px', textTransform: 'uppercase' as const, letterSpacing: '0.05em' }}>Ship — {ship.name || 'Unnamed'}</div>
            {ship.hull_hp != null && (
              <div style={{ marginBottom: '4px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.72rem', color: '#999' }}><span>Hull</span><span>{ship.hull_hp}/{ship.hull_max || ship.hull_hp}</span></div>
                <div style={{ height: '5px', backgroundColor: '#2a2a4e', borderRadius: '2px', overflow: 'hidden' }}>
                  <div style={{ height: '100%', width: `${(ship.hull_max || ship.hull_hp) ? (ship.hull_hp / (ship.hull_max || ship.hull_hp)) * 100 : 0}%`, backgroundColor: '#94a3b8', borderRadius: '2px' }} />
                </div>
              </div>
            )}
            {ship.shields != null && (
              <div style={{ marginBottom: '4px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.72rem', color: '#999' }}><span>Shields</span><span>{typeof ship.shields === 'object' ? `${ship.shields.current}/${ship.shields.max}` : ship.shields}</span></div>
                <div style={{ height: '5px', backgroundColor: '#2a2a4e', borderRadius: '2px', overflow: 'hidden' }}>
                  <div style={{ height: '100%', width: `${typeof ship.shields === 'object' && ship.shields.max > 0 ? (ship.shields.current / ship.shields.max) * 100 : 100}%`, backgroundColor: '#38bdf8', borderRadius: '2px' }} />
                </div>
              </div>
            )}
            {ship.credits != null && (
              typeof ship.credits === 'object' ? (
                <div style={{ marginTop: '4px' }}>
                  {Object.entries(ship.credits).map(([k, v]: [string, any]) => (
                    <div key={k} style={{ fontSize: '0.72rem', color: '#fbbf24', display: 'flex', justifyContent: 'space-between' }}>
                      <span>{k}</span><span>{typeof v === 'number' ? v.toLocaleString() : v}¤</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div style={{ fontSize: '0.72rem', color: '#fbbf24', marginTop: '4px' }}>Credits: {typeof ship.credits === 'number' ? ship.credits.toLocaleString() : ship.credits}¤</div>
              )
            )}
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
              {er.eurobucks != null && <div style={{ fontSize: '0.72rem', color: '#fbbf24' }}>Eurobucks: \u20AC${er.eurobucks}</div>}
              {er.critical_injuries && er.critical_injuries.length > 0 && (
                <div style={{ marginTop: '4px' }}>
                  {er.critical_injuries.map((ci: string, i: number) => (
                    <div key={i} style={{ fontSize: '0.7rem', color: '#ef4444' }}>{'\u2022'} {ci}</div>
                  ))}
                </div>
              )}
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
          {(data.class || data.level != null) && (
            <div style={{ fontSize: '0.85rem', color: '#999', marginBottom: '12px', marginTop: '-8px' }}>
              {[data.class, data.level != null ? `Level ${data.level}` : ''].filter(Boolean).join(' · ')}
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
              {summary && <div style={{ fontSize: '0.78rem', color: '#999', marginTop: '6px', lineHeight: 1.4 }}>{summary}</div>}
            </div>
          )}

          {/* Game-specific state */}
          {renderGameState()}

          {/* Funds: Ship shows all, PC shows own (or all if no ship in game), NPC hidden */}
          {(() => {
            const allFunds = state.hud_state?.funds || {};
            const hasShipInScene = Object.values(cs).some((e: any) => (e?.data || e)?.type === 'ship');
            if (type === 'ship') {
              const entries = Object.entries(allFunds);
              if (entries.length > 0) return (
                <div style={{ marginTop: '12px' }}>
                  <div style={{ fontSize: '0.75rem', fontWeight: 600, color: '#888', marginBottom: '4px', textTransform: 'uppercase' as const, letterSpacing: '0.05em', borderBottom: '1px solid #2a2a4e', paddingBottom: '4px' }}>Funds</div>
                  {entries.map(([k, v]: [string, any]) => (
                    <div key={k} style={{ fontSize: '0.82rem', color: '#fbbf24', display: 'flex', justifyContent: 'space-between', padding: '2px 0' }}>
                      <span>{k}</span><span>{typeof v === 'string' ? v : v}</span>
                    </div>
                  ))}
                </div>
              );
            } else if (type === 'pc') {
              if (!hasShipInScene) {
                // No ship — PC shows all funds
                const entries = Object.entries(allFunds);
                if (entries.length > 0) return (
                  <div style={{ marginTop: '12px' }}>
                    <div style={{ fontSize: '0.75rem', fontWeight: 600, color: '#888', marginBottom: '4px', textTransform: 'uppercase' as const, letterSpacing: '0.05em', borderBottom: '1px solid #2a2a4e', paddingBottom: '4px' }}>Funds</div>
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
                    <div style={{ fontSize: '0.75rem', fontWeight: 600, color: '#888', marginBottom: '4px', textTransform: 'uppercase' as const, letterSpacing: '0.05em', borderBottom: '1px solid #2a2a4e', paddingBottom: '4px' }}>Funds</div>
                    <div style={{ fontSize: '0.85rem', color: '#fbbf24' }}>{typeof hudFunds === 'string' ? hudFunds : hudFunds}</div>
                  </div>
                );
              }
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

          {/* Character sheet (collapsed, rendered as markdown or styled YAML) */}
          {sheetSection && (
            <details style={{ marginTop: '16px', borderTop: '1px solid #2a2a4e', paddingTop: '8px' }}>
              <summary style={{ fontSize: '0.78rem', color: '#888', cursor: 'pointer', padding: '4px 0', userSelect: 'none' }}>Full Character Sheet</summary>
              {(() => {
                // Strip the header line(s) — ## heading for md, # === banner for yaml
                let body = sheetSection;
                if (body.startsWith('## ')) {
                  body = body.split('\n').slice(1).join('\n').trim();
                } else if (body.startsWith('# ===')) {
                  // Strip the 3-line banner (# ===, # Title, # ===)
                  body = body.replace(/^# ={3,}\n#\s+.+\n# ={3,}\n*/, '').trim();
                }
                const isYaml = /^- \w+:|^\w+:/.test(body.trim());
                if (isYaml) {
                  return (
                    <div style={{ fontSize: '0.75rem', color: '#ccc', lineHeight: 1.6, marginTop: '8px', fontFamily: "'Consolas', 'Monaco', monospace", whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                      {body.split('\n').map((line, i) => {
                        const commentStyle = { color: '#5a8a6a', fontStyle: 'italic' as const };
                        // Render inline # comments in green
                        const renderWithComment = (text: string, baseStyle: React.CSSProperties) => {
                          const cIdx = text.indexOf('  #');
                          if (cIdx >= 0) return <><span style={baseStyle}>{text.slice(0, cIdx)}</span><span style={commentStyle}>{text.slice(cIdx)}</span></>;
                          return <span style={baseStyle}>{text}</span>;
                        };
                        // Full-line # comments
                        if (/^\s*#/.test(line)) return <div key={i} style={commentStyle}>{line}</div>;
                        // Style keys (- name:, class:, etc.) with inline comment support
                        const keyMatch = line.match(/^(\s*-?\s*)(\w[\w\s]*?)(:)(.*)/);
                        if (keyMatch) return (
                          <div key={i}>
                            <span>{keyMatch[1]}</span>
                            <span style={{ color: '#60a5fa', fontWeight: 500 }}>{keyMatch[2]}</span>
                            <span style={{ color: '#666' }}>{keyMatch[3]}</span>
                            {renderWithComment(keyMatch[4], { color: '#e0e0e0' })}
                          </div>
                        );
                        // List items with inline comment support
                        if (/^\s+-\s/.test(line)) {
                          const cIdx = line.indexOf('  #');
                          if (cIdx >= 0) return <div key={i}><span style={{ color: '#ccc' }}>{line.slice(0, cIdx)}</span><span style={commentStyle}>{line.slice(cIdx)}</span></div>;
                          return <div key={i} style={{ color: '#ccc' }}>{line}</div>;
                        }
                        return <div key={i}>{line}</div>;
                      })}
                    </div>
                  );
                }
                return (
                  <div className="messageContent" style={{ fontSize: '0.78rem', color: '#ccc', lineHeight: 1.5, marginTop: '8px' }}>
                    <ReactMarkdown>{body}</ReactMarkdown>
                  </div>
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

  return (
    <>
      {renderDesktopPanel()}
      {renderMobileBottomSheet()}
      {renderCharacterSheetModal()}
      {renderAllCharactersModal()}
      {renderNpcMemoriesModal()}
    </>
  );
}
