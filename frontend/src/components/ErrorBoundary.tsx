import React from 'react';

interface ErrorBoundaryProps {
  children: React.ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
  errorInfo: React.ErrorInfo | null;
}

// Top-level error boundary. Without this, a render-time throw anywhere
// in the tree unmounts the whole app and shows a blank white screen with
// no console message visible to a non-devtools user. Catching here lets
// us surface the error, preserve sessionStorage drafts, and offer a
// reload path instead of stranding the user.
export default class ErrorBoundary extends React.Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { error: null, errorInfo: null };
  }

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo): void {
    console.error('[ErrorBoundary] caught render error:', error, errorInfo);
    this.setState({ errorInfo });
  }

  handleReload = (): void => {
    window.location.reload();
  };

  handleDismiss = (): void => {
    this.setState({ error: null, errorInfo: null });
  };

  render(): React.ReactNode {
    if (this.state.error) {
      return (
        <div
          style={{
            minHeight: '100dvh',
            backgroundColor: '#1a1a2e',
            color: '#eee',
            fontFamily: 'system-ui, sans-serif',
            padding: '24px',
            display: 'flex',
            flexDirection: 'column',
            gap: '16px',
            overflow: 'auto',
          }}
        >
          <h1 style={{ margin: 0, fontSize: '1.4rem', color: '#ff6b6b' }}>
            Something broke in the UI
          </h1>
          <p style={{ margin: 0, color: '#bbb' }}>
            A render error crashed the chat view. Your draft input is preserved in sessionStorage,
            and any message the backend already accepted will reappear after reload.
          </p>
          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              onClick={this.handleReload}
              style={{
                padding: '8px 16px',
                fontSize: '0.9rem',
                borderRadius: '6px',
                border: 'none',
                backgroundColor: '#4a4a8e',
                color: '#fff',
                cursor: 'pointer',
              }}
            >
              Reload app
            </button>
            <button
              onClick={this.handleDismiss}
              style={{
                padding: '8px 16px',
                fontSize: '0.9rem',
                borderRadius: '6px',
                border: '1px solid #555',
                backgroundColor: 'transparent',
                color: '#eee',
                cursor: 'pointer',
              }}
            >
              Try to keep going
            </button>
          </div>
          <details style={{ marginTop: '8px' }} open>
            <summary style={{ cursor: 'pointer', color: '#aaa' }}>Error details</summary>
            <pre
              style={{
                marginTop: '8px',
                padding: '12px',
                backgroundColor: '#0f0f1e',
                color: '#ff9b9b',
                borderRadius: '6px',
                fontSize: '0.8rem',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                overflow: 'auto',
                maxHeight: '40dvh',
              }}
            >
              {this.state.error.toString()}
              {this.state.errorInfo?.componentStack || ''}
            </pre>
          </details>
        </div>
      );
    }
    return this.props.children;
  }
}
