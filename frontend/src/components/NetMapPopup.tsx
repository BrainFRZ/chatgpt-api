import React, { useMemo } from 'react';
import type { SystemMap } from '../types';

interface NetMapPopupProps {
  systemMap: SystemMap;
  revealedNodes: string[];
  currentNode: string;
  nodesVisited: string[];
  iceStatus: Record<string, any>;
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

  // Find gateway or first node
  const gateway = names.find(n => nodes[n]?.type === 'gateway') || names[0];

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

function NodeShape({ type, cx, cy, stroke, fill, opacity, glowing }: {
  type: string; cx: number; cy: number; stroke: string; fill: string; opacity: number; glowing?: boolean;
}) {
  const filter = glowing ? 'url(#glow)' : undefined;
  const r = NODE_RADIUS;
  const common = { stroke, strokeWidth: glowing ? 2 : 1.5, fill, opacity, filter };

  switch (type) {
    case 'gateway':
      return <polygon points={hexagonPoints(cx, cy, r)} {...common} />;
    case 'password_gate':
      return <polygon points={diamondPoints(cx, cy, r)} {...common} />;
    case 'target':
      return <polygon points={starPoints(cx, cy, r + 2)} {...common} />;
    case 'control_node':
      return <circle cx={cx} cy={cy} r={r - 2} {...common} />;
    default: // data_node or unknown
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

function iceStatusColor(iceStatus: Record<string, any>, nodeName: string): string | null {
  // Check if there's ICE at this node in iceStatus
  const status = iceStatus[nodeName];
  if (!status || typeof status !== 'object') return null;
  if (status.status === 'derezzed' || status.status === 'disabled') return '#333';
  if (status.status === 'bypassed') return '#555';
  // Active — use behavior color
  return iceColor(status.behavior) || iceColor(null);
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
  systemMap, revealedNodes, currentNode, nodesVisited, iceStatus, onClose,
}) => {
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
              SR {systemMap.sr}
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
                // Both revealed — full solid edge
                return (
                  <line
                    key={`${from}-${to}`}
                    x1={p1.x} y1={p1.y} x2={p2.x} y2={p2.y}
                    stroke="#00ff41"
                    strokeWidth={1}
                    opacity={0.4}
                    filter="url(#edgeGlow)"
                  />
                );
              } else if (fromRevealed || toRevealed) {
                // One end revealed — draw stub from revealed end, dissolving
                const revealed = fromRevealed ? p1 : p2;
                const hidden = fromRevealed ? p2 : p1;
                // Stub goes ~35% of the way
                const stubX = revealed.x + (hidden.x - revealed.x) * 0.35;
                const stubY = revealed.y + (hidden.y - revealed.y) * 0.35;
                return (
                  <g key={`${from}-${to}`}>
                    <line
                      x1={revealed.x} y1={revealed.y} x2={stubX} y2={stubY}
                      stroke="#00ff41"
                      strokeWidth={1}
                      opacity={0.25}
                      strokeDasharray="4 3"
                    />
                    <DissolveParticles x1={revealed.x} y1={revealed.y} x2={stubX} y2={stubY} />
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

                  {/* DV badge */}
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

                  {/* Type label for visited nodes */}
                  {isVisited && node.type !== 'gateway' && (
                    <text
                      x={pos.x}
                      y={pos.y - NODE_RADIUS - 4}
                      textAnchor="middle"
                      fill="#00802060"
                      fontSize="6.5"
                      fontFamily='monospace'
                      style={{ textTransform: 'uppercase' }}
                    >
                      {node.type.replace('_', ' ')}
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
