import React, { useState, useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import { Artifact } from '../types';

export interface ArtifactPanelProps {
  isMobile: boolean;
  artifacts: Record<string, Artifact>;
  selectedArtifactId: string | null;
  setSelectedArtifactId: (v: string | null) => void;
  rightPanelOpen: boolean;
  setRightPanelOpen: (v: boolean) => void;
  mobileBottomSheetOpen: boolean;
  setMobileBottomSheetOpen: (v: boolean) => void;
}

// ── VS Code Dark+ Colors ───────────────────────────────────────────

const vs = {
  keyword: '#569CD6',
  string: '#CE9178',
  comment: '#6A9955',
  number: '#B5CEA8',
  function: '#DCDCAA',
  variable: '#9CDCFE',
  punct: '#D4D4D4',
  text: '#D4D4D4',
  bool: '#569CD6',
  bg: '#1e1e2e',
  bgLight: '#252535',
  lineNum: '#858585',
};

const typeColors: Record<string, string> = {
  prose: '#a78bfa',
  outline: '#38bdf8',
  notes: '#fbbf24',
  character: '#34d399',
  json: '#9CDCFE',
  pdf: '#ef4444',
};

// ── Helpers ─────────────────────────────────────────────────────────

function getFormat(doc: Artifact): string {
  if (doc.format) return doc.format;
  if (doc.type === 'json') return 'json';
  if (doc.type === 'pdf') return 'pdf';
  return 'md';
}

function getExtension(doc: Artifact): string {
  const fmt = getFormat(doc);
  return fmt === 'md' ? '.md' : fmt === 'json' ? '.json' : fmt === 'pdf' ? '.pdf' : '.txt';
}

function downloadArtifact(doc: Artifact) {
  const fmt = getFormat(doc);
  const ext = getExtension(doc);
  const mimeType = fmt === 'json' ? 'application/json'
    : fmt === 'pdf' ? 'application/pdf'
    : 'text/plain';
  const content = fmt === 'pdf' ? atob(doc.content) : doc.content;
  const blob = fmt === 'pdf'
    ? new Blob([Uint8Array.from(content, c => c.charCodeAt(0))], { type: mimeType })
    : new Blob([content], { type: mimeType + ';charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = doc.doc_id + ext;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// ── JSON Syntax Highlighting ────────────────────────────────────────

function highlightJson(json: string): React.ReactNode[] {
  // Tokenize JSON with a regex that captures keys, strings, numbers, booleans, null, punctuation
  const tokenRegex = /("(?:\\.|[^"\\])*")\s*:|("(?:\\.|[^"\\])*")|((?:-?\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?)|(\btrue\b|\bfalse\b|\bnull\b)|([{}[\]:,])|(\s+)/g;
  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  let match;

  while ((match = tokenRegex.exec(json)) !== null) {
    // Any text between matches
    if (match.index > lastIndex) {
      parts.push(json.slice(lastIndex, match.index));
    }
    const [full, key, str, num, bool, punct, ws] = match;
    if (key) {
      parts.push(<span key={parts.length} style={{ color: vs.variable }}>{key}</span>);
      parts.push(<span key={parts.length + 0.5} style={{ color: vs.punct }}>:</span>);
    } else if (str) {
      parts.push(<span key={parts.length} style={{ color: vs.string }}>{str}</span>);
    } else if (num) {
      parts.push(<span key={parts.length} style={{ color: vs.number }}>{num}</span>);
    } else if (bool) {
      parts.push(<span key={parts.length} style={{ color: vs.bool }}>{bool}</span>);
    } else if (punct) {
      parts.push(<span key={parts.length} style={{ color: vs.punct }}>{punct}</span>);
    } else if (ws) {
      parts.push(ws);
    }
    lastIndex = match.index + full.length;
  }
  if (lastIndex < json.length) {
    parts.push(json.slice(lastIndex));
  }
  return parts;
}

// ── Markdown Code Block Component ───────────────────────────────────

function CodeBlock({ children, className }: { children?: React.ReactNode; className?: string }) {
  const isInline = !className;
  const text = String(children).replace(/\n$/, '');

  if (isInline) {
    return (
      <code style={{
        backgroundColor: vs.bgLight, color: vs.string,
        padding: '1px 4px', borderRadius: '3px', fontSize: '0.85em',
      }}>
        {text}
      </code>
    );
  }

  return (
    <pre style={{
      backgroundColor: vs.bg, padding: '12px 14px', borderRadius: '6px',
      overflow: 'auto', fontSize: '0.82rem', lineHeight: 1.5,
      border: '1px solid #333', margin: '8px 0',
      scrollbarWidth: 'thin' as any,
    }}>
      <code style={{ color: vs.text, fontFamily: "'Consolas', 'Monaco', 'Courier New', monospace" }}>
        {text}
      </code>
    </pre>
  );
}

// ── Component ───────────────────────────────────────────────────────

export default function ArtifactPanel({
  isMobile, artifacts, selectedArtifactId, setSelectedArtifactId,
  rightPanelOpen, setRightPanelOpen, mobileBottomSheetOpen, setMobileBottomSheetOpen,
}: ArtifactPanelProps) {
  const [copied, setCopied] = useState(false);
  const docList = useMemo(() => Object.values(artifacts), [artifacts]);
  const selected = selectedArtifactId ? artifacts[selectedArtifactId] : null;

  // Auto-select most recently updated doc if none selected
  const effectiveDoc = selected || (docList.length > 0
    ? docList.reduce((a, b) => (a.updated_at || '') > (b.updated_at || '') ? a : b)
    : null);

  const handleCopy = () => {
    if (effectiveDoc) {
      navigator.clipboard.writeText(effectiveDoc.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  const handleDownload = () => {
    if (effectiveDoc) downloadArtifact(effectiveDoc);
  };

  const handleClose = () => {
    setRightPanelOpen(false);
  };

  // ── Content Renderers ─────────────────────────────────────────────

  const renderContent = () => {
    if (!effectiveDoc) {
      return (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#555', fontSize: '0.85rem' }}>
          No documents yet
        </div>
      );
    }

    const fmt = getFormat(effectiveDoc);

    if (fmt === 'json') {
      let formatted: string;
      try {
        formatted = JSON.stringify(JSON.parse(effectiveDoc.content), null, 2);
      } catch {
        formatted = effectiveDoc.content;
      }
      return (
        <div style={{ flex: 1, overflow: 'auto', padding: '16px 20px', scrollbarWidth: 'thin' as any }}>
          <pre style={{
            backgroundColor: vs.bg, padding: '14px 16px', borderRadius: '6px',
            fontSize: '0.82rem', lineHeight: 1.5, border: '1px solid #333',
            fontFamily: "'Consolas', 'Monaco', 'Courier New', monospace",
            margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word' as const,
          }}>
            {highlightJson(formatted)}
          </pre>
        </div>
      );
    }

    if (fmt === 'pdf') {
      return (
        <div style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' as const }}>
          <iframe
            src={`data:application/pdf;base64,${effectiveDoc.content}`}
            style={{ flex: 1, border: 'none', width: '100%', backgroundColor: '#fff' }}
            title={effectiveDoc.title}
          />
        </div>
      );
    }

    if (fmt === 'txt') {
      return (
        <div style={{ flex: 1, overflow: 'auto', padding: '16px 20px', scrollbarWidth: 'thin' as any }}>
          <pre style={{
            color: vs.text, fontSize: '0.85rem', lineHeight: 1.6,
            fontFamily: "'Consolas', 'Monaco', 'Courier New', monospace",
            margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word' as const,
          }}>
            {effectiveDoc.content}
          </pre>
        </div>
      );
    }

    // Default: markdown
    return (
      <div style={{ flex: 1, overflow: 'auto', padding: '16px 20px', scrollbarWidth: 'thin' as any }}>
        <div style={{ fontSize: '0.88rem', color: '#ccc', lineHeight: 1.7 }} className="artifactMarkdown">
          <ReactMarkdown components={{
            code: ({ children, className }) => <CodeBlock className={className}>{children}</CodeBlock>,
          }}>
            {effectiveDoc.content}
          </ReactMarkdown>
        </div>
      </div>
    );
  };

  // ── Doc Tabs ──────────────────────────────────────────────────────

  const renderDocTabs = () => {
    if (docList.length <= 1) return null;
    return (
      <div style={{
        display: 'flex', gap: '2px', padding: '0 16px 8px',
        overflowX: 'auto', scrollbarWidth: 'thin' as any,
        borderBottom: '1px solid #2a2a4e',
      }}>
        {docList.map(doc => {
          const isActive = effectiveDoc?.doc_id === doc.doc_id;
          const color = typeColors[doc.type] || '#888';
          return (
            <button
              key={doc.doc_id}
              onClick={() => setSelectedArtifactId(doc.doc_id)}
              style={{
                padding: '4px 10px', fontSize: '0.72rem', borderRadius: '4px 4px 0 0',
                border: 'none', cursor: 'pointer', whiteSpace: 'nowrap' as const,
                backgroundColor: isActive ? '#1e1e3a' : 'transparent',
                color: isActive ? '#e0e0e0' : '#888',
                borderBottom: isActive ? `2px solid ${color}` : '2px solid transparent',
                fontWeight: isActive ? 600 : 400,
              }}
              onMouseEnter={e => { if (!isActive) e.currentTarget.style.color = '#ccc'; }}
              onMouseLeave={e => { if (!isActive) e.currentTarget.style.color = '#888'; }}
            >
              {doc.title.length > 24 ? doc.title.slice(0, 22) + '\u2026' : doc.title}
            </button>
          );
        })}
      </div>
    );
  };

  // ── Header ────────────────────────────────────────────────────────

  const renderHeader = () => {
    const fmt = effectiveDoc ? getFormat(effectiveDoc) : 'md';
    const fmtColor = effectiveDoc ? (typeColors[effectiveDoc.type] || '#888') : '#888';
    return (
      <div style={{
        padding: '10px 16px', borderBottom: '1px solid #2a2a4e',
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        flexShrink: 0,
      }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          {effectiveDoc ? (
            <>
              <div style={{ fontSize: '0.92rem', fontWeight: 600, color: '#e0e0e0', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' as const }}>
                {effectiveDoc.title}
              </div>
              <div style={{ fontSize: '0.62rem', color: '#666', marginTop: '2px', display: 'flex', gap: '8px', alignItems: 'center' }}>
                <span style={{ color: fmtColor, fontWeight: 600 }}>{fmt.toUpperCase()}</span>
                <span>v{effectiveDoc.version}</span>
                {fmt !== 'pdf' && <span>~{effectiveDoc.content.split(/\s+/).length} words</span>}
              </div>
            </>
          ) : (
            <span style={{ fontSize: '0.85rem', fontWeight: 600, color: '#e0e0e0' }}>Documents</span>
          )}
        </div>
        <div style={{ display: 'flex', gap: '4px', alignItems: 'center', flexShrink: 0 }}>
          {effectiveDoc && (
            <>
              <button
                onClick={handleCopy}
                title="Copy to clipboard"
                style={{
                  padding: '4px 8px', fontSize: '0.7rem', color: copied ? '#34d399' : '#888',
                  backgroundColor: '#1e1e3a', border: '1px solid #2a2a4e', borderRadius: '4px', cursor: 'pointer',
                }}
                onMouseEnter={e => { e.currentTarget.style.backgroundColor = '#2a2a4e'; }}
                onMouseLeave={e => { e.currentTarget.style.backgroundColor = '#1e1e3a'; }}
              >
                {copied ? '\u2713' : 'Copy'}
              </button>
              <button
                onClick={handleDownload}
                title="Download file"
                style={{
                  padding: '4px 8px', fontSize: '0.7rem', color: '#888',
                  backgroundColor: '#1e1e3a', border: '1px solid #2a2a4e', borderRadius: '4px', cursor: 'pointer',
                }}
                onMouseEnter={e => { e.currentTarget.style.backgroundColor = '#2a2a4e'; e.currentTarget.style.color = '#ccc'; }}
                onMouseLeave={e => { e.currentTarget.style.backgroundColor = '#1e1e3a'; e.currentTarget.style.color = '#888'; }}
              >
                {'\u2B73'}
              </button>
            </>
          )}
          <button
            onClick={handleClose}
            title="Close viewer"
            style={{ background: 'none', border: 'none', fontSize: '1.1rem', color: '#888', cursor: 'pointer', padding: '0 4px', marginLeft: '4px' }}
            onMouseEnter={e => { e.currentTarget.style.color = '#ccc'; }}
            onMouseLeave={e => { e.currentTarget.style.color = '#888'; }}
          >
            {'\u2715'}
          </button>
        </div>
      </div>
    );
  };

  // ── Desktop: Collapsed Tab ────────────────────────────────────────

  if (!isMobile && !rightPanelOpen) {
    return (
      <div
        onClick={() => setRightPanelOpen(true)}
        style={{
          width: '28px', minWidth: '28px', backgroundColor: '#16162a',
          borderLeft: '1px solid #333', display: 'flex', flexDirection: 'column' as const,
          alignItems: 'center', justifyContent: 'center', cursor: 'pointer',
          transition: 'background-color 0.2s',
        }}
        onMouseEnter={e => (e.currentTarget.style.backgroundColor = '#1e1e3a')}
        onMouseLeave={e => (e.currentTarget.style.backgroundColor = '#16162a')}
      >
        <span style={{
          writingMode: 'vertical-rl' as const, transform: 'rotate(180deg)',
          fontSize: '0.72rem', color: '#888', letterSpacing: '0.05em',
        }}>
          DOCS
        </span>
        {docList.length > 0 && (
          <span style={{ fontSize: '0.65rem', color: '#888', marginTop: '8px', backgroundColor: '#2a2a4e', borderRadius: '8px', padding: '1px 5px' }}>
            {docList.length}
          </span>
        )}
      </div>
    );
  }

  // ── Desktop: Full Viewer (flex: 1 = 50% of remaining space) ──────

  if (!isMobile) {
    return (
      <div style={{
        flex: 1, backgroundColor: '#16162a',
        borderLeft: '1px solid #333', display: 'flex', flexDirection: 'column' as const,
        overflow: 'hidden', minWidth: 0,
      }}>
        {renderHeader()}
        {renderDocTabs()}
        {renderContent()}
      </div>
    );
  }

  // ── Mobile: Bottom Sheet ──────────────────────────────────────────

  if (!mobileBottomSheetOpen || docList.length === 0) return null;

  return (
    <div style={{
      position: 'fixed', bottom: 0, left: 0, right: 0, zIndex: 1500,
      backgroundColor: '#16162a', borderTop: '1px solid #333',
      maxHeight: '70vh', display: 'flex', flexDirection: 'column' as const,
      borderRadius: '12px 12px 0 0', overflow: 'hidden',
    }}>
      {renderHeader()}
      {renderDocTabs()}
      {renderContent()}
    </div>
  );
}

// Export download helper for use by ChatView inline cards
export { downloadArtifact };
