import Link from "next/link";
import { Calendar, ArrowRight } from "lucide-react";
import { events, allEventsHref } from "@/content/events";

/**
 * Community events section — Plone's pattern: 2 event cards + "All events" CTA.
 */
export function CommunityEvents() {
  if (events.length === 0) return null;

  return (
    <section className="py-20 md:py-28 border-t border-surface-800">
      <div className="mx-auto max-w-7xl px-6">
        <h2 className="text-3xl md:text-4xl font-bold tracking-tight text-center mb-12">
          Community Events
        </h2>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 max-w-4xl mx-auto">
          {events.slice(0, 2).map((event) => (
            <a
              key={event.title}
              href={event.href}
              target="_blank"
              rel="noopener noreferrer"
              className="card-interactive p-6 flex flex-col"
            >
              <div className="flex items-center gap-2 text-xs text-surface-500 mb-3">
                <Calendar size={14} />
                {event.date}
              </div>
              <h3 className="text-lg font-semibold text-text-primary mb-2">
                {event.title}
              </h3>
              <p className="text-sm text-surface-400 leading-relaxed flex-1">
                {event.description}
              </p>
              <span className="inline-flex items-center gap-1 text-sm font-medium text-brand-300 hover:text-brand-200 transition-colors mt-4">
                Read more
                <ArrowRight size={14} />
              </span>
            </a>
          ))}
        </div>

        {allEventsHref && (
          <div className="text-center mt-8">
            <Link
              href={allEventsHref}
              className="inline-flex items-center gap-1.5 text-sm font-medium text-brand-300 hover:text-brand-200 transition-colors"
            >
              All events
              <ArrowRight size={14} />
            </Link>
          </div>
        )}
      </div>
    </section>
  );
}
