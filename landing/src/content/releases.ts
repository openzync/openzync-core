// ═══════════════════════════════════════════════════════════════════════════════
// OpenZep Landing — Release Spotlight Data
// ═══════════════════════════════════════════════════════════════════════════════

export interface Release {
  version: string;
  title: string;
  date: string;
  description: string;
  downloadUrl: string;
  image?: string;
}

export const latestRelease: Release = {
  version: "1.0.0",
  title: "OpenZep 1.0 Is Here!",
  date: "2026-03-15",
  description:
    "OpenZep 1.0 is the latest and greatest evolution of persistent agent memory infrastructure. With support for 10+ graph backends, 5+ LLM providers, human-in-the-loop workflows, and built-in observability — it's the memory layer your agents deserve.",
  downloadUrl: "https://github.com/openzep/openzep/releases/tag/v1.0.0",
};
