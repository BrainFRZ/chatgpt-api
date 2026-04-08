import React from 'react';
import { styles } from '../styles';
import { ModelInfo } from '../types';

interface PendingTimeJump {
  seconds: number;
  duration: string;
  from_date: string;
  from_time: string;
  to_date: string;
  to_time: string;
}

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
  pendingTimeJump: PendingTimeJump | null;
  onConfirmTimeJump: (confirm: boolean) => void;
}

function formatHHMM(s: string): string {
  if (!s || s.length < 3) return s || '';
  const padded = s.padStart(4, '0');
  return `${padded.slice(0, 2)}:${padded.slice(2)}`;
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
    pendingTimeJump,
    onConfirmTimeJump,
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

      {/* Time-jump confirmation modal — fires when the model implies a forward time jump > 24h. */}
      {pendingTimeJump && (
        <div style={styles.modalOverlay}>
          <div style={{...styles.modal, maxWidth: '440px'}} onClick={(e) => e.stopPropagation()}>
            <h3 style={styles.modalTitle}>Advance the in-game clock?</h3>
            <p style={styles.modalDescription}>
              The story implies time has advanced by <strong>{pendingTimeJump.duration}</strong>.
              Apply this jump to the clock?
            </p>
            <div style={{
              padding: '12px 14px',
              background: '#1a1a1a',
              border: '1px solid #333',
              borderRadius: '4px',
              marginBottom: '16px',
              fontFamily: 'monospace',
              fontSize: '0.85rem',
              color: '#ddd',
              lineHeight: 1.6,
            }}>
              <div>From: {pendingTimeJump.from_date} {formatHHMM(pendingTimeJump.from_time)}</div>
              <div>To: &nbsp;&nbsp;{pendingTimeJump.to_date} {formatHHMM(pendingTimeJump.to_time)}</div>
            </div>
            <div style={styles.modalActions}>
              <button
                onClick={() => onConfirmTimeJump(false)}
                style={styles.modalCancelButton}
              >
                Cancel
              </button>
              <button
                onClick={() => onConfirmTimeJump(true)}
                style={styles.modalSaveButton}
              >
                Apply jump
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
