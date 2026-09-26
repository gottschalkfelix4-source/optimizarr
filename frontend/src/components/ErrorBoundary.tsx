/** Catches a page that fails to render, so the shell and navigation survive. */
import { Component, type ErrorInfo, type ReactNode } from "react";
import { ErrorState, Panel } from "./ui";

export class ErrorBoundary extends Component<
  { children: ReactNode; resetKey?: string },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidUpdate(prev: { resetKey?: string }) {
    // Navigating elsewhere gives the next page a fresh start.
    if (this.state.error && prev.resetKey !== this.props.resetKey) this.setState({ error: null });
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Seite konnte nicht angezeigt werden", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <Panel>
        <ErrorState
          title="Diese Seite konnte nicht angezeigt werden"
          error={this.state.error}
          onRetry={() => this.setState({ error: null })}
        />
      </Panel>
    );
  }
}
