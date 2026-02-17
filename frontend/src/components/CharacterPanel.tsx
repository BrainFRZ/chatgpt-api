import React from 'react';
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
    const cs = pipelineState.character_states || {};
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
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: vitals.length ? '4px' : 0 }}>
          <span style={{ fontWeight: 600, fontSize: '0.82rem', color: '#e0e0e0' }}>{name}</span>
          <div style={{ display: 'flex', gap: '4px', alignItems: 'center' }}>
            {isActive && <span style={{ fontSize: '0.6rem', fontWeight: 700, color: '#4a4ae8', letterSpacing: '0.05em' }}>ACTING</span>}
            <span style={{
              fontSize: '0.6rem', padding: '1px 5px', borderRadius: '3px', fontWeight: 600,
              backgroundColor: typeBadgeColor(type) + '22', color: typeBadgeColor(type),
            }}>{type.toUpperCase()}</span>
          </div>
        </div>
        {vitals.map((v: any, i: number) => (
          'current' in v && 'max' in v ? (
            <div key={i} style={{ marginBottom: '2px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.68rem', color: '#999', marginBottom: '1px' }}>
                <span>{v.label}</span><span>{v.current}/{v.max}</span>
              </div>
              <div style={{ height: '4px', backgroundColor: '#2a2a4e', borderRadius: '2px', overflow: 'hidden' }}>
                <div style={{ height: '100%', width: `${v.max > 0 ? Math.max(0, Math.min(100, (v.current / v.max) * 100)) : 0}%`, backgroundColor: vitalColor(v.current, v.max, v.label), borderRadius: '2px', transition: 'width 0.3s' }} />
              </div>
            </div>
          ) : 'value' in v ? (
            <div key={i} style={{ display: 'inline-block', marginRight: '6px', marginBottom: '2px' }}>
              <span style={{ fontSize: '0.68rem', color: '#999' }}>{v.label}: </span>
              <span style={{ fontSize: '0.72rem', fontWeight: 600, color: '#94a3b8', backgroundColor: '#2a2a4e', padding: '0 4px', borderRadius: '3px' }}>{v.value}</span>
            </div>
          ) : null
        ))}
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

  // --- Desktop Right Panel ---
  const renderDesktopPanel = () => {
    if (!(!isMobile && pipelineState)) return null;

    const cs = pipelineState.character_states || {};
    const scene = pipelineState.scene_state || {};
    const combat = pipelineState.combat;
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
        {Object.keys(cs).length + Object.keys(pipelineState.npc_memories || {}).length > 0 && (
          <div style={{ padding: '8px 12px', borderTop: '1px solid #333' }}>
            <button
              onClick={() => setShowAllCharactersModal(true)}
              style={{ width: '100%', padding: '6px', fontSize: '0.75rem', color: '#888', backgroundColor: '#1e1e3a', border: '1px solid #2a2a4e', borderRadius: '4px', cursor: 'pointer' }}
              onMouseEnter={e => { e.currentTarget.style.backgroundColor = '#2a2a4e'; e.currentTarget.style.color = '#ccc'; }}
              onMouseLeave={e => { e.currentTarget.style.backgroundColor = '#1e1e3a'; e.currentTarget.style.color = '#888'; }}
            >
              View All ({new Set([...Object.keys(cs), ...Object.keys(pipelineState.npc_memories || {})]).size})
            </button>
          </div>
        )}
      </div>
    );
  };

  // --- Mobile Bottom Sheet ---
  const renderMobileBottomSheet = () => {
    if (!(isMobile && pipelineState)) return null;

    const cs = pipelineState.character_states || {};
    const scene = pipelineState.scene_state || {};
    const pcsPresent = scene.pcs_present || [];
    const npcsPresent = scene.npcs_present || [];
    const allPresent = pcsPresent.concat(npcsPresent).filter((v: string, i: number, a: string[]) => a.indexOf(v) === i);
    if (allPresent.length === 0) return null;

    const mobileVitalColor = (cur: number, max: number) => {
      const pct = max > 0 ? cur / max : 1;
      return pct > 0.6 ? '#4ade80' : pct > 0.3 ? '#fbbf24' : '#ef4444';
    };

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
            {allPresent.map((name: string) => {
              const data = getCharData(name);
              const type = data.type || 'npc';
              const vitals = data.vitals || [];
              return (
                <div
                  key={name}
                  onClick={(e) => { e.stopPropagation(); setMobileBottomSheetOpen(false); setSelectedCharacter(name); setShowCharacterSheet(true); }}
                  style={{ padding: '8px 10px', marginBottom: '4px', borderRadius: '6px', backgroundColor: '#1e1e3a', border: '1px solid #2a2a4e' }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: vitals.length ? '4px' : 0 }}>
                    <span style={{ fontWeight: 600, fontSize: '0.82rem', color: '#e0e0e0' }}>{name}</span>
                    <span style={{ fontSize: '0.6rem', padding: '1px 5px', borderRadius: '3px', fontWeight: 600, backgroundColor: typeBadgeColor(type) + '22', color: typeBadgeColor(type) }}>{type.toUpperCase()}</span>
                  </div>
                  {vitals.filter((v: any) => 'current' in v && 'max' in v).slice(0, 2).map((v: any, i: number) => (
                    <div key={i} style={{ marginBottom: '2px' }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.68rem', color: '#999' }}><span>{v.label}</span><span>{v.current}/{v.max}</span></div>
                      <div style={{ height: '4px', backgroundColor: '#2a2a4e', borderRadius: '2px', overflow: 'hidden' }}>
                        <div style={{ height: '100%', width: `${v.max > 0 ? Math.max(0, Math.min(100, (v.current / v.max) * 100)) : 0}%`, backgroundColor: mobileVitalColor(v.current, v.max), borderRadius: '2px' }} />
                      </div>
                    </div>
                  ))}
                </div>
              );
            })}
          </div>
        )}
      </div>
    );
  };

  // --- Character Sheet Modal ---
  const renderCharacterSheetModal = () => {
    if (!(showCharacterSheet && selectedCharacter && pipelineState)) return null;

    const cs = pipelineState.character_states || {};
    const entry = cs[selectedCharacter];
    const data = entry?.data || entry || {};
    const vitals = data.vitals || [];
    const resources = data.resources || [];
    const conditions = data.conditions || [];
    const type = data.type || 'npc';
    const summary = data.summary || '';
    const gameState = pipelineState.game_state || {};
    const hudFunds = pipelineState.hud_state?.funds?.[selectedCharacter];
    const memories = (pipelineState.npc_memories || {})[selectedCharacter];

    // Find character section in .md sheet
    const sheetSection = (() => {
      if (!characterSheetMd) return '';
      const sections = characterSheetMd.split(/(?=^## )/m);
      const match = sections.find(s => {
        const heading = s.split('\n')[0].replace(/^## /, '').trim();
        return heading.toLowerCase() === selectedCharacter.toLowerCase() || heading.toLowerCase().includes(selectedCharacter.toLowerCase());
      });
      return match || '';
    })();

    // Game-specific state sections
    const renderGameState = () => {
      const gs = chatGameSystem || 'dnd5e';
      const parts: React.ReactNode[] = [];

      // D&D 5E / D&D 5E Cyber: Relationships
      if ((gs === 'dnd5e' || gs === 'dnd5e_cyber') && gameState.relationships) {
        const rels = Object.entries(gameState.relationships || {}).filter(([_, r]: [string, any]) => r && typeof r === 'object');
        if (rels.length > 0) {
          parts.push(
            <div key="rels" style={{ marginTop: '12px' }}>
              <div style={{ fontSize: '0.75rem', fontWeight: 600, color: '#888', marginBottom: '6px', textTransform: 'uppercase' as const, letterSpacing: '0.05em' }}>Relationships</div>
              {rels.map(([npc, r]: [string, any]) => {
                const rs = r.rs || 0;
                const tierColor = rs <= -3 ? '#ef4444' : rs <= 0 ? '#94a3b8' : rs <= 3 ? '#4ade80' : '#a78bfa';
                return (
                  <div key={npc} style={{ display: 'flex', justifyContent: 'space-between', padding: '3px 0', fontSize: '0.78rem', borderBottom: '1px solid #2a2a4e' }}>
                    <span style={{ color: '#ccc' }}>{npc}</span>
                    <span style={{ color: tierColor, fontWeight: 500 }}>RS {rs} — {r.tier || '?'}{r.roms > 0 ? ` \u2665${r.roms}` : ''}</span>
                  </div>
                );
              })}
            </div>
          );
        }
        // Factions
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

      // D&D 5E Cyber: Ship
      if (gs === 'dnd5e_cyber' && gameState.ship) {
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
              <div style={{ fontSize: '0.72rem', color: '#fbbf24', marginTop: '4px' }}>Credits: {ship.credits}</div>
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
      <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: 'rgba(0,0,0,0.7)', zIndex: 1900, display: 'flex', justifyContent: 'center', alignItems: 'center' }} onClick={() => { setShowCharacterSheet(false); setSelectedCharacter(null); }}>
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

          {/* Funds */}
          {hudFunds && (
            <div style={{ marginTop: '12px' }}>
              <div style={{ fontSize: '0.75rem', fontWeight: 600, color: '#888', marginBottom: '4px', textTransform: 'uppercase' as const, letterSpacing: '0.05em', borderBottom: '1px solid #2a2a4e', paddingBottom: '4px' }}>Funds</div>
              <div style={{ fontSize: '0.85rem', color: '#fbbf24' }}>{typeof hudFunds === 'string' ? hudFunds : JSON.stringify(hudFunds)}</div>
            </div>
          )}

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

          {/* Character sheet .md (collapsed) */}
          {sheetSection && (
            <details style={{ marginTop: '16px', borderTop: '1px solid #2a2a4e', paddingTop: '8px' }}>
              <summary style={{ fontSize: '0.78rem', color: '#888', cursor: 'pointer', padding: '4px 0', userSelect: 'none' }}>Full Character Sheet</summary>
              <div style={{ fontSize: '0.78rem', color: '#ccc', lineHeight: 1.5, marginTop: '8px', whiteSpace: 'pre-wrap' }}>
                {sheetSection.split('\n').slice(1).join('\n').trim()}
              </div>
            </details>
          )}
        </div>
      </div>
    );
  };

  // --- All Characters Modal ---
  const renderAllCharactersModal = () => {
    if (!(showAllCharactersModal && pipelineState)) return null;

    const cs = pipelineState.character_states || {};
    const npcMem = pipelineState.npc_memories || {};
    const scene = pipelineState.scene_state || {};
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
    if (!(showNpcMemories && pipelineState)) return null;

    const memories = (pipelineState.npc_memories || {})[showNpcMemories] || [];
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
