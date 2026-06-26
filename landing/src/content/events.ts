// ═══════════════════════════════════════════════════════════════════════════════
// OpenZep Landing — Community Events Data
// ═══════════════════════════════════════════════════════════════════════════════

export interface EventItem {
  title: string;
  date: string;
  description: string;
  href: string;
  image?: string;
}

export const events: EventItem[] = [
  {
    title: "OpenZep Sprint February 2026",
    date: "February 10–14, 2026",
    description:
      "A week-long community sprint focused on plugin development, documentation improvements, and performance benchmarking across all supported graph backends.",
    href: "https://github.com/openzep/openzep/discussions",
  },
  {
    title: "OpenZep Conference 2026",
    date: "September 21–27, 2026",
    description:
      "The inaugural OpenZep Conference in Maastricht, Netherlands. Three days of talks, workshops, and sprints focused on agent memory infrastructure.",
    href: "https://conf.openzep.com",
  },
  {
    title: "World Agent Memory Day 2026",
    date: "April 16, 2026",
    description:
      "A global online event featuring talks from the agent memory community. Over 40 sessions covering graph backends, LLM integration, and production patterns.",
    href: "https://openzep.com/events/wamd-2026",
  },
];

export const allEventsHref = "/events";
