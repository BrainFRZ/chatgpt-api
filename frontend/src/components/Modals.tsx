import React from 'react';
import { styles } from '../styles';
import { ModelInfo } from '../types';

interface ModalsProps {
  showUpdatesModal: boolean;
  setShowUpdatesModal: (v: boolean) => void;
  draftUpdatesText: string;
  updateUpdatesText: (v: string) => void;
  updatesTokenCount: number;
  updatesLoading: boolean;
  saveUpdates: () => void;
  showApiKeyModal: boolean;
  pendingModelSwitch: string | null;
  modalApiKey: string;
  setModalApiKey: (v: string) => void;
  handleApiKeyModalSave: () => void;
  handleApiKeyModalCancel: () => void;
  savingApiKey: boolean;
  availableModels: ModelInfo[];
}

export default function Modals(props: ModalsProps) {
  const {
    showUpdatesModal,
    setShowUpdatesModal,
    draftUpdatesText,
    updateUpdatesText,
    updatesTokenCount,
    updatesLoading,
    saveUpdates,
    showApiKeyModal,
    pendingModelSwitch,
    modalApiKey,
    setModalApiKey,
    handleApiKeyModalSave,
    handleApiKeyModalCancel,
    savingApiKey,
    availableModels,
  } = props;

  return (
    <>
      {showUpdatesModal && (
        <div style={styles.modalOverlay} onClick={() => setShowUpdatesModal(false)}>
          <div style={styles.modal} onClick={(e) => e.stopPropagation()}>
            <h3 style={styles.modalTitle}>Notes</h3>
            <p style={styles.modalDescription}>
              A personal notepad for this chat. Not sent to the model.
            </p>
            <textarea
              value={draftUpdatesText}
              onChange={(e) => updateUpdatesText(e.target.value)}
              placeholder="e.g., Session notes, reminders, character details..."
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

    </>
  );
}


// ── FlakinessBandsModal ─────────────────────────────────────────────
// Standalone modal opened from ChatView when an interview-finalize message
// includes a flakiness_bands_proposal. Pre-fills sliders with the model's
// recommendation; user can adjust before saving. Bands are auto-committed
// by finalize_interview already, so dismissing leaves the auto-committed
// values in place — this is review-and-adjust, not a gate.

const FB_CATEGORIES = ['work', 'shae', 'family', 'social', 'self_care', 'admin'] as const;
const FB_BUCKETS = ['as_planned', 'modified', 'cancelled'] as const;

type FBCategory = typeof FB_CATEGORIES[number];
type FBBucket = typeof FB_BUCKETS[number];
type FBRow = Record<FBBucket, number>;
type FBBands = Record<FBCategory, FBRow>;

const FB_CATEGORY_LABELS: Record<FBCategory, string> = {
  work: 'Work / on-the-clock',
  shae: 'Plans with you',
  family: 'Family obligations',
  social: 'Other social plans',
  self_care: 'Self-care (gym, yoga, runs)',
  admin: 'Admin / errands',
};

const FB_BUCKET_LABELS: Record<FBBucket, string> = {
  as_planned: 'as planned',
  modified: 'modified',
  cancelled: 'cancelled',
};

function rebalanceFB(row: FBRow, changedKey: FBBucket, newValue: number): FBRow {
  newValue = Math.max(0, Math.min(1, newValue));
  const others = FB_BUCKETS.filter((k) => k !== changedKey) as FBBucket[];
  const remaining = 1 - newValue;
  const otherSum = others.reduce((s, k) => s + (row[k] || 0), 0);
  const next = { ...row, [changedKey]: newValue } as FBRow;
  if (otherSum < 0.001) {
    const each = remaining / others.length;
    others.forEach((k) => { next[k] = each; });
  } else {
    others.forEach((k) => { next[k] = (row[k] || 0) * (remaining / otherSum); });
  }
  return next;
}

interface FlakinessBandsModalProps {
  isOpen: boolean;
  username: string;
  project: string;
  proposal: FBBands;
  onClose: () => void;
  onSaved?: (bands: FBBands) => void;
}

export function FlakinessBandsModal({
  isOpen, username, project, proposal, onClose, onSaved,
}: FlakinessBandsModalProps) {
  const [bands, setBands] = React.useState<FBBands>(proposal);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (isOpen) {
      setBands(proposal);
      setError(null);
    }
  }, [isOpen, proposal]);

  if (!isOpen) return null;

  const handleSlider = (cat: FBCategory, bucket: FBBucket, val: number) => {
    setBands((b) => ({ ...b, [cat]: rebalanceFB(b[cat], bucket, val) }));
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      const res = await fetch('/api/characters/commit-flakiness-bands', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, project, flakiness_bands: bands }),
      });
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        throw new Error((j as any).detail || `${res.status} ${res.statusText}`);
      }
      const data = await res.json();
      onSaved?.(data.flakiness_bands);
      onClose();
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={styles.modalOverlay} onClick={onClose}>
      <div
        style={{ ...styles.modal, maxWidth: 540, maxHeight: '85vh', overflowY: 'auto' }}
        onClick={(e) => e.stopPropagation()}
      >
        <h3 style={styles.modalTitle}>Review follow-through</h3>
        <p style={styles.modalDescription}>
          How reliable is this character across categories? Each row shows how 100 attempts split into
          "happened as planned", "happened differently", and "didn't happen". The model's recommendation
          is pre-filled — adjust any slider and the others rebalance to keep the row at 100%.
        </p>
        {FB_CATEGORIES.map((cat) => (
          <div key={cat} style={{ marginBottom: 14 }}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>{FB_CATEGORY_LABELS[cat]}</div>
            {FB_BUCKETS.map((bucket) => (
              <div key={bucket} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 2 }}>
                <span style={{ width: 100, fontSize: 13, color: '#888' }}>{FB_BUCKET_LABELS[bucket]}</span>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.01}
                  value={bands[cat]?.[bucket] ?? 0}
                  onChange={(e) => handleSlider(cat, bucket, parseFloat(e.target.value))}
                  style={{ flex: 1 }}
                />
                <span style={{ width: 42, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                  {Math.round((bands[cat]?.[bucket] ?? 0) * 100)}%
                </span>
              </div>
            ))}
          </div>
        ))}
        {error && (
          <div style={{ color: '#ff6b6b', marginBottom: 8, fontSize: 13 }}>{error}</div>
        )}
        <div style={styles.modalFooter}>
          <div />
          <div style={styles.modalActions}>
            <button onClick={onClose} style={styles.modalCancelButton}>Cancel</button>
            <button
              onClick={handleSave}
              disabled={saving}
              style={{ ...styles.modalSaveButton, opacity: saving ? 0.5 : 1 }}
            >
              {saving ? 'Saving...' : 'Save'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
