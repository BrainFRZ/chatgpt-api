import React, { useMemo } from 'react';
import type { SystemMap } from '../types';

interface NetMapPopupProps {
  systemMap: SystemMap;
  revealedNodes: string[];
  currentNode: string;
  nodesVisited: string[];
  iceStatus: Record<string, any>;
  /** SR override — backend stores SR at hack_state.sr, NOT inside the
   * system_map dict. Without this prop the header badge renders as "SR ".
   */
  sr?: number | string;
  onClose: () => void;
}

// --- Layout ---

interface NodePosition {
  x: number;
  y: number;
  name: string;
}

/** BFS-layered layout: Gateway at top, layers by distance. */
function computeLayout(systemMap: SystemMap): Record<string, NodePosition> {
  const nodes = systemMap.nodes || {};
  const names = Object.keys(nodes);
  if (names.length === 0) return {};

  // Find gateway or first node — type matching is case-insensitive because
  // the model emits TitleCase ("Gateway") while the renderer enum is lower.
  const gateway =
    names.find(n => normalizeNodeType(nodes[n]?.type) === 'gateway')
    || names.find(n => n === 'Gateway' || n.toLowerCase() === 'gateway')
    || names[0];

  // BFS to assign layers
  const layers: Record<string, number> = {};
  const queue: string[] = [gateway];
  layers[gateway] = 0;
  while (queue.length > 0) {
    const current = queue.shift()!;
    const conns = nodes[current]?.connections || [];
    for (const neighbor of conns) {
      if (!(neighbor in layers) && neighbor in nodes) {
        layers[neighbor] = layers[current] + 1;
        queue.push(neighbor);
      }
    }
  }

  // Catch disconnected nodes
  for (const n of names) {
    if (!(n in layers)) {
      layers[n] = Math.max(0, ...Object.values(layers)) + 1;
    }
  }

  // Group by layer
  const layerGroups: Record<number, string[]> = {};
  for (const [name, layer] of Object.entries(layers)) {
    if (!layerGroups[layer]) layerGroups[layer] = [];
    layerGroups[layer].push(name);
  }

  const maxLayer = Math.max(...Object.keys(layerGroups).map(Number));
  const canvasW = 600;
  const canvasH = 400;
  const padX = 80;
  const padY = 60;
  const usableW = canvasW - padX * 2;
  const usableH = canvasH - padY * 2;

  const positions: Record<string, NodePosition> = {};
  for (let layer = 0; layer <= maxLayer; layer++) {
    const group = layerGroups[layer] || [];
    const y = maxLayer === 0 ? canvasH / 2 : padY + (layer / maxLayer) * usableH;
    const spacing = usableW / (group.length + 1);
    group.forEach((name, i) => {
      positions[name] = {
        x: padX + spacing * (i + 1),
        y,
        name,
      };
    });
  }

  return positions;
}

// --- SVG shapes for node types ---

const NODE_RADIUS = 24;

function hexagonPoints(cx: number, cy: number, r: number): string {
  const pts: string[] = [];
  for (let i = 0; i < 6; i++) {
    const angle = (Math.PI / 3) * i - Math.PI / 2;
    pts.push(`${cx + r * Math.cos(angle)},${cy + r * Math.sin(angle)}`);
  }
  return pts.join(' ');
}

function diamondPoints(cx: number, cy: number, r: number): string {
  return `${cx},${cy - r} ${cx + r},${cy} ${cx},${cy + r} ${cx - r},${cy}`;
}

function starPoints(cx: number, cy: number, r: number): string {
  const pts: string[] = [];
  for (let i = 0; i < 8; i++) {
    const angle = (Math.PI / 4) * i - Math.PI / 2;
    const rad = i % 2 === 0 ? r : r * 0.55;
    pts.push(`${cx + rad * Math.cos(angle)},${cy + rad * Math.sin(angle)}`);
  }
  return pts.join(' ');
}

/** Normalize a node `type` string (model uses TitleCase + spaces; backend
 * sometimes uses snake_case; CRB has multiple aliases) to one of the
 * canonical shape buckets the renderer understands.  Without this every
 * non-matching type falls through to the default tiny rectangle, which
 * is what makes Service Port / Password gates render as half-ghosts.
 */
function normalizeNodeType(type: unknown): string {
  if (!type) return 'data_node';
  const t = String(type).trim().toLowerCase().replace(/[ -]+/g, '_');
  if (t === 'gateway') return 'gateway';
  if (t === 'target' || t === 'objective' || t === 'goal') return 'target';
  if (t === 'password' || t === 'password_gate' || t === 'password_node') return 'password_gate';
  if (t === 'control' || t === 'control_node' || t === 'control_room') return 'control_node';
  if (t === 'file' || t === 'file_node' || t === 'data' || t === 'data_node') return 'data_node';
  return 'data_node';
}

function NodeShape({ type, cx, cy, stroke, fill, opacity, glowing }: {
  type: string; cx: number; cy: number; stroke: string; fill: string; opacity: number; glowing?: boolean;
}) {
  const filter = glowing ? 'url(#glow)' : undefined;
  const r = NODE_RADIUS;
  const common = { stroke, strokeWidth: glowing ? 2 : 1.5, fill, opacity, filter };
  const canonical = normalizeNodeType(type);

  switch (canonical) {
    case 'gateway':
      return <polygon points={hexagonPoints(cx, cy, r)} {...common} />;
    case 'password_gate':
      return <polygon points={diamondPoints(cx, cy, r)} {...common} />;
    case 'target':
      return <polygon points={starPoints(cx, cy, r + 2)} {...common} />;
    case 'control_node':
      return <circle cx={cx} cy={cy} r={r - 2} {...common} />;
    default: // data_node
      return <rect x={cx - r + 4} y={cy - r + 6} width={(r - 4) * 2} height={(r - 6) * 2} rx={3} {...common} />;
  }
}

// --- ICE color ---

function iceColor(ice: string | null): string | null {
  switch (ice) {
    case 'patrol': return '#fbbf24';
    case 'tar': return '#fb923c';
    case 'black': return '#ef4444';
    case 'trace': return '#a855f7';
    default: return null;
  }
}

/** Find every ICE entry placed at the given node.  Backend keys ice_status
 * as `<node>_<species>` and also stores the node in each entry's `node`
 * field — the lookup checks both so we don't miss ICE just because the
 * key separator differs from what the renderer assumed.  Returns the
 * highest-severity color, with Black > Trace > Tar > Patrol precedence
 * so the dot accurately signals threat at a glance.
 */
function iceStatusColor(iceStatus: Record<string, any>, nodeName: string): string | null {
  if (!iceStatus || typeof iceStatus !== 'object') return null;
  const matches: any[] = [];
  for (const [key, status] of Object.entries(iceStatus)) {
    if (!status || typeof status !== 'object') continue;
    const matchesNode =
      key === nodeName
      || key.startsWith(`${nodeName}_`)
      || key.startsWith(`${nodeName}::`)
      || (status as any).node === nodeName;
    if (matchesNode) matches.push(status);
  }
  if (matches.length === 0) return null;
  // If ANY active ICE present, use the highest-severity active one.
  const severity: Record<string, number> = { black: 4, trace: 3, tar: 2, patrol: 1 };
  let best: any = null;
  for (const m of matches) {
    if (m.status === 'derezzed' || m.status === 'disabled' || m.status === 'destroyed') continue;
    if (m.status === 'bypassed') continue;
    const beh = String(m.behavior || '').toLowerCase();
    if (!best || (severity[beh] || 0) > (severity[String(best.behavior || '').toLowerCase()] || 0)) {
      best = m;
    }
  }
  if (best) return iceColor(String(best.behavior || '').toLowerCase());
  // No active ICE — show a dim dot to signal "ICE was here, now gone".
  const anyDerezzed = matches.some(m =>
    m.status === 'derezzed' || m.status === 'disabled' || m.status === 'destroyed'
  );
  const anyBypassed = matches.some(m => m.status === 'bypassed');
  if (anyDerezzed) return '#333';
  if (anyBypassed) return '#555';
  return null;
}

// --- Edge dissolve particles ---

function DissolveParticles({ x1, y1, x2, y2 }: { x1: number; y1: number; x2: number; y2: number }) {
  // Draw 5-8 small circles along the last 40% of the stub, fading out
  const particles: React.ReactElement[] = [];
  const count = 6;
  for (let i = 0; i < count; i++) {
    const t = 0.6 + (i / count) * 0.4;
    const px = x1 + (x2 - x1) * t;
    const py = y1 + (y2 - y1) * t;
    // Add slight random-looking scatter using deterministic offset
    const scatter = ((i * 7 + 3) % 5) - 2;
    const opacity = 1 - (i / count);
    particles.push(
      <circle
        key={i}
        cx={px + scatter}
        cy={py + scatter * 0.7}
        r={1.5 - i * 0.15}
        fill="#00ff41"
        opacity={opacity * 0.6}
      />
    );
  }
  return <>{particles}</>;
}

// --- Main component ---

const NetMapPopup: React.FC<NetMapPopupProps> = ({
  systemMap, revealedNodes, currentNode, nodesVisited, iceStatus, sr, onClose,
}) => {
  const displaySr = sr ?? (systemMap as any)?.sr ?? '?';
  const positions = useMemo(() => computeLayout(systemMap), [systemMap]);
  const revealedSet = useMemo(() => new Set(revealedNodes), [revealedNodes]);
  const visitedSet = useMemo(() => new Set(nodesVisited), [nodesVisited]);

  const nodes = systemMap.nodes || {};

  // Build edge list (deduplicated)
  const edges = useMemo(() => {
    const mapNodes = systemMap.nodes || {};
    const seen = new Set<string>();
    const result: { from: string; to: string; bothRevealed: boolean }[] = [];
    for (const [name, node] of Object.entries(mapNodes)) {
      for (const conn of (node.connections || [])) {
        const key = name < conn ? `${name}\0${conn}` : `${conn}\0${name}`;
        if (!seen.has(key) && positions[name] && positions[conn]) {
          seen.add(key);
          result.push({
            from: name,
            to: conn,
            bothRevealed: revealedSet.has(name) && revealedSet.has(conn),
          });
        }
      }
    }
    return result;
  }, [systemMap, positions, revealedSet]);

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
        backgroundColor: 'rgba(0, 5, 0, 0.85)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        zIndex: 2000,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          backgroundColor: '#050a05',
          border: '1px solid #00ff4140',
          borderRadius: '8px',
          width: '90%',
          maxWidth: '700px',
          maxHeight: '90vh',
          display: 'flex',
          flexDirection: 'column',
          boxShadow: '0 0 30px rgba(0, 255, 65, 0.15)',
          overflow: 'hidden',
        }}
      >
        {/* Header */}
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          padding: '12px 16px',
          borderBottom: '1px solid #00ff4120',
          background: 'linear-gradient(180deg, #0a1a0a 0%, #050a05 100%)',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <span style={{
              fontFamily: '"Courier New", monospace',
              fontSize: '0.8rem',
              fontWeight: 700,
              color: '#00ff41',
              letterSpacing: '3px',
              textShadow: '0 0 8px rgba(0, 255, 65, 0.5)',
            }}>
              SYSTEM ARCHITECTURE
            </span>
            <span style={{
              fontSize: '0.6rem',
              color: '#00ff41',
              backgroundColor: '#00ff4118',
              padding: '2px 6px',
              borderRadius: '3px',
              fontFamily: 'monospace',
            }}>
              SR {displaySr}
            </span>
          </div>
          <button
            onClick={onClose}
            style={{
              background: 'none', border: 'none', color: '#00ff4180',
              fontSize: '1.2rem', cursor: 'pointer', padding: '2px 6px',
              fontFamily: 'monospace',
            }}
          >
            ×
          </button>
        </div>

        {/* SVG Map */}
        <div style={{
          flex: 1,
          padding: '8px',
          position: 'relative',
          overflow: 'auto',
          backgroundImage: 'repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0, 255, 65, 0.015) 2px, rgba(0, 255, 65, 0.015) 4px)',
        }}>
          <svg
            viewBox="0 0 600 400"
            style={{ width: '100%', height: 'auto', display: 'block' }}
          >
            <defs>
              {/* Glow filter for current node */}
              <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
                <feGaussianBlur stdDeviation="4" result="blur" />
                <feMerge>
                  <feMergeNode in="blur" />
                  <feMergeNode in="SourceGraphic" />
                </feMerge>
              </filter>
              {/* Subtle glow for edges */}
              <filter id="edgeGlow" x="-20%" y="-20%" width="140%" height="140%">
                <feGaussianBlur stdDeviation="1.5" result="blur" />
                <feMerge>
                  <feMergeNode in="blur" />
                  <feMergeNode in="SourceGraphic" />
                </feMerge>
              </filter>
            </defs>

            {/* Edges */}
            {edges.map(({ from, to, bothRevealed }) => {
              const p1 = positions[from];
              const p2 = positions[to];
              if (!p1 || !p2) return null;

              const fromRevealed = revealedSet.has(from);
              const toRevealed = revealedSet.has(to);

              if (bothRevealed) {
                // Both revealed — full solid edge.  No filter: feGaussianBlur
                // on a vertical line has a degenerate (zero-width) bounding
                // box that some renderers clip, hiding the line entirely.
                // Bumping strokeWidth + opacity gives the same readability.
                return (
                  <line
                    key={`${from}-${to}`}
                    x1={p1.x} y1={p1.y} x2={p2.x} y2={p2.y}
                    stroke="#00ff41"
                    strokeWidth={2}
                    opacity={0.85}
                    strokeLinecap="round"
                  />
                );
              } else if (fromRevealed || toRevealed) {
                // One end revealed — draw stub from the revealed node's
                // EDGE (not center) so the stub doesn't run through the
                // node's label below it.  The label sits at
                // y = center + NODE_RADIUS + 14, so starting the stub at
                // y = center + NODE_RADIUS + 18 keeps it below the label.
                const revealed = fromRevealed ? p1 : p2;
                const hidden = fromRevealed ? p2 : p1;
                const dx = hidden.x - revealed.x;
                const dy = hidden.y - revealed.y;
                const dist = Math.hypot(dx, dy) || 1;
                // Push start 38px from center (past NODE_RADIUS + label band)
                const startOffset = 38;
                const startX = revealed.x + (dx / dist) * startOffset;
                const startY = revealed.y + (dy / dist) * startOffset;
                // Stub extends another ~28% of remaining distance.
                const stubX = startX + dx * 0.28;
                const stubY = startY + dy * 0.28;
                return (
                  <g key={`${from}-${to}`}>
                    <line
                      x1={startX} y1={startY} x2={stubX} y2={stubY}
                      stroke="#00ff41"
                      strokeWidth={1}
                      opacity={0.35}
                      strokeDasharray="4 3"
                    />
                    <DissolveParticles x1={startX} y1={startY} x2={stubX} y2={stubY} />
                  </g>
                );
              }
              // Neither revealed — don't render
              return null;
            })}

            {/* Nodes */}
            {Object.entries(positions).map(([name, pos]) => {
              if (!revealedSet.has(name)) return null;

              const node = nodes[name];
              if (!node) return null;

              const isCurrent = name === currentNode;
              const isVisited = visitedSet.has(name);
              const opacity = isCurrent ? 1 : isVisited ? 0.85 : 0.5;
              const strokeColor = isCurrent ? '#00ff41' : isVisited ? '#00cc33' : '#00802080';
              const fillColor = isCurrent ? '#00ff4115' : '#050a0580';

              // ICE dot color — check iceStatus first, then system_map
              const iceStatusCol = iceStatusColor(iceStatus, name);
              const iceMapCol = iceColor(node.ice);
              const iceDotColor = iceStatusCol || iceMapCol;

              return (
                <g key={name}>
                  {/* Current node pulse animation */}
                  {isCurrent && (
                    <circle cx={pos.x} cy={pos.y} r={NODE_RADIUS + 6} fill="none" stroke="#00ff41" strokeWidth={1} opacity={0.3}>
                      <animate attributeName="r" values={`${NODE_RADIUS + 4};${NODE_RADIUS + 10};${NODE_RADIUS + 4}`} dur="2s" repeatCount="indefinite" />
                      <animate attributeName="opacity" values="0.3;0.1;0.3" dur="2s" repeatCount="indefinite" />
                    </circle>
                  )}

                  {/* Node shape */}
                  <NodeShape
                    type={node.type}
                    cx={pos.x}
                    cy={pos.y}
                    stroke={strokeColor}
                    fill={fillColor}
                    opacity={opacity}
                    glowing={isCurrent}
                  />

                  {/* Node name */}
                  <text
                    x={pos.x}
                    y={pos.y + NODE_RADIUS + 14}
                    textAnchor="middle"
                    fill={isCurrent ? '#00ff41' : isVisited ? '#00cc33' : '#00802080'}
                    fontSize="9"
                    fontFamily='"Courier New", monospace'
                    style={{ textShadow: isCurrent ? '0 0 6px rgba(0, 255, 65, 0.5)' : 'none' }}
                  >
                    {name}
                  </text>

                  {/* DV badge — only for VISITED nodes per RAW.  Pathfinder
                       reveals layout (existence, type, connections, ICE)
                       but NOT the DV; the runner has to actually enter
                       the node to learn how hard the gate/check is. */}
                  {isVisited && node.dv > 0 && (
                    <text
                      x={pos.x}
                      y={pos.y + 4}
                      textAnchor="middle"
                      fill={isCurrent ? '#00ff41' : '#00aa30'}
                      fontSize="8"
                      fontFamily='monospace'
                      fontWeight={600}
                      opacity={opacity}
                    >
                      DV{node.dv}
                    </text>
                  )}

                  {/* ICE indicator dot */}
                  {iceDotColor && (
                    <circle
                      cx={pos.x + NODE_RADIUS - 6}
                      cy={pos.y - NODE_RADIUS + 6}
                      r={4}
                      fill={iceDotColor}
                      stroke="#050a05"
                      strokeWidth={1}
                      opacity={iceStatusCol === '#333' || iceStatusCol === '#555' ? 0.4 : 0.9}
                    />
                  )}

                  {/* Type label — visible for every revealed non-Gateway
                       node (was: only visited).  Pathfinder reveals layout
                       including node types.  Bypassed gates carry a
                       "(BYPASSED)" suffix so the runner sees at a glance
                       which obstacles are no longer barriers. */}
                  {normalizeNodeType(node.type) !== 'gateway' && (
                    <text
                      x={pos.x}
                      y={pos.y - NODE_RADIUS - 4}
                      textAnchor="middle"
                      fill={(node as any).bypassed
                        ? '#00aa3066'
                        : isVisited ? '#00802099' : '#00802070'}
                      fontSize="6.5"
                      fontFamily='monospace'
                      style={{
                        textTransform: 'uppercase',
                        textDecoration: (node as any).bypassed ? 'line-through' : 'none',
                      }}
                    >
                      {String(node.type || '').replace(/_/g, ' ')}
                      {(node as any).bypassed ? ' (BYPASSED)' : ''}
                    </text>
                  )}
                </g>
              );
            })}
          </svg>
        </div>

        {/* Legend */}
        <div style={{
          padding: '8px 16px',
          borderTop: '1px solid #00ff4120',
          display: 'flex',
          gap: '12px',
          flexWrap: 'wrap',
          fontSize: '0.55rem',
          fontFamily: 'monospace',
          color: '#00802080',
        }}>
          {[
            ['#fbbf24', 'Patrol'],
            ['#fb923c', 'Tar'],
            ['#ef4444', 'Black'],
            ['#a855f7', 'Trace'],
          ].map(([color, label]) => (
            <span key={label} style={{ display: 'flex', alignItems: 'center', gap: '3px' }}>
              <span style={{ width: '6px', height: '6px', borderRadius: '50%', backgroundColor: color, display: 'inline-block' }} />
              {label}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
};

export default NetMapPopup;
