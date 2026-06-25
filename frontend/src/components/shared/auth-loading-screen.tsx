"use client";

/**
 * Full-page loading overlay displayed while authentication state is being checked.
 * Prevents any dashboard UI from flashing before auth is confirmed.
 *
 * Renders nothing after mount — the parent (RequireAuth) unmounts this
 * once the auth decision is made.
 */

export function AuthLoadingScreen() {
  return (
    <div className="fixed inset-0 z-[100] flex flex-col items-center justify-center bg-surface-950">
      {/* Animated background glow */}
      <div className="absolute inset-0 overflow-hidden">
        <div className="absolute left-1/2 top-1/3 h-96 w-96 -translate-x-1/2 -translate-y-1/2 rounded-full bg-brand-500/5 blur-3xl" />
        <div className="absolute left-1/3 top-1/2 h-64 w-64 -translate-y-1/2 rounded-full bg-accent-500/3 blur-3xl" />
      </div>

      {/* Logo */}
      <div className="relative mb-6">
        <div className="flex h-20 w-20 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500/20 to-brand-600/10 ring-1 ring-brand-500/20">
          <span className="text-4xl font-bold text-brand-500">O</span>
        </div>
      </div>

      {/* Brand name */}
      <h1 className="relative text-2xl font-bold text-text-primary mb-1">
        OpenZep
      </h1>
      <p className="relative text-sm text-surface-500 mb-10">
        Agent Memory Infrastructure
      </p>

      {/* Spinner */}
      <div className="relative flex items-center gap-3">
        <div className="h-5 w-5 animate-spin rounded-full border-2 border-surface-700 border-t-brand-500" />
        <span className="text-sm text-surface-500">Loading your workspace&hellip;</span>
      </div>
    </div>
  );
}
