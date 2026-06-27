import type { Metadata } from "next";
import Link from "next/link";
import { Calendar, ArrowRight, ExternalLink } from "lucide-react";
import { Breadcrumbs, buildBreadcrumbSegments } from "@/components/landing/breadcrumbs";
import { events, type EventItem } from "@/content/events";
import { CtaSection } from "@/components/landing/cta-section";

export const metadata: Metadata = {
  title: "Events",
  description:
    "Join the OpenZep community at events around the world — conferences, sprints, online meetups, and more.",
};

/** Parse event start date, handling range format "Month Day–Day, Year" and single date. */
function parseStartDate(dateStr: string): Date {
  const start = dateStr.split("–")[0]?.trim() ?? dateStr;
  if (!/\d{4}/.test(start)) {
    const yearMatch = dateStr.match(/(\d{4})/);
    return new Date(`${start}, ${yearMatch?.[1] ?? new Date().getFullYear()}`);
  }
  return new Date(start);
}

/** Compute month/day from an event for the date badge. */
function eventBadgeParts(event: EventItem): { month: string; day: string } {
  const d = parseStartDate(event.date);
  if (isNaN(d.getTime())) return { month: "", day: "" };
  return {
    month: d.toLocaleDateString("en-US", { month: "short" }),
    day: String(d.getDate()),
  };
}

/** Render a single event card. */
function EventCard({
  event,
  variant,
}: {
  event: EventItem;
  variant: "upcoming" | "past";
}) {
  const { month, day } = eventBadgeParts(event);
  const isUpcoming = variant === "upcoming";
  const badgeStyle = isUpcoming
    ? "bg-brand-500/10 border-brand-500/20 text-brand-300 [&_.day]:text-text-primary"
    : "bg-surface-800 border-surface-700 text-surface-500 [&_.day]:text-surface-400";
  const linkStyle = isUpcoming ? "card-interactive group" : "card-base opacity-60 hover:opacity-100 transition-opacity";

  return (
    <a
      href={event.href}
      target="_blank"
      rel="noopener noreferrer"
      className={linkStyle}
    >
      <div className="p-6 flex items-start gap-4">
        {/* Date badge */}
        <div
          className={`flex shrink-0 flex-col items-center justify-center w-16 h-16 rounded-lg border ${badgeStyle}`}
        >
          <span className="text-xs font-bold uppercase tracking-wider">{month}</span>
          <span className="day text-lg font-extrabold">{day}</span>
        </div>
        <div className="flex-1 min-w-0">
          <h3
            className={`text-lg font-semibold text-text-primary mb-1 ${
              isUpcoming ? "group-hover:text-brand-300 transition-colors" : ""
            }`}
          >
            {event.title}
          </h3>
          <p className="text-sm text-surface-500 mb-2">{event.date}</p>
          <p className="text-sm text-surface-400 leading-relaxed">{event.description}</p>
          {isUpcoming && (
            <span className="inline-flex items-center gap-1 text-sm text-brand-300 mt-3 group-hover:gap-2 transition-all">
              Learn more <ExternalLink size={14} />
            </span>
          )}
        </div>
      </div>
    </a>
  );
}

/**
 * Events page — Plone-style community events listing.
 */
export default function EventsPage() {
  const segments = buildBreadcrumbSegments("/events");

  const now = new Date();
  const upcoming = events.filter((e) => {
    const d = parseStartDate(e.date);
    return d >= now || isNaN(d.getTime());
  });
  const past = events.filter((e) => {
    const d = parseStartDate(e.date);
    return d < now && !isNaN(d.getTime());
  });

  return (
    <>
      {/* Header */}
      <section className="pt-36 pb-16">
        <div className="mx-auto max-w-5xl px-6">
          <Breadcrumbs segments={segments} />
          <h1 className="text-4xl md:text-5xl font-extrabold tracking-tight mb-4">
            Community Events
          </h1>
          <p className="text-surface-400 text-lg max-w-2xl">
            Meet the OpenZep community at events around the world. From conferences
            to online sprints — there&rsquo;s a place for everyone.
          </p>
        </div>
      </section>

      {/* Upcoming events */}
      <section className="pb-16">
        <div className="mx-auto max-w-5xl px-6">
          <h2 className="text-2xl font-bold tracking-tight mb-8 flex items-center gap-3">
            <span className="flex h-8 w-8 items-center justify-center rounded-full bg-brand-500/20">
              <Calendar size={16} className="text-brand-300" />
            </span>
            Upcoming Events
          </h2>

          {upcoming.length === 0 ? (
            <div className="card-base p-10 text-center">
              <p className="text-surface-400 text-lg mb-2">No upcoming events right now.</p>
              <p className="text-surface-500 text-sm">
                Check back soon or{" "}
                <a href="mailto:hello@openzep.com" className="text-brand-300 hover:text-brand-200 transition-colors">
                  get in touch
                </a>{" "}
                to host your own.
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {upcoming.map((event) => (
                <EventCard key={event.title} event={event} variant="upcoming" />
              ))}
            </div>
          )}
        </div>
      </section>

      {/* Past events */}
      {past.length > 0 && (
        <section className="pb-20 border-t border-surface-800 pt-16">
          <div className="mx-auto max-w-5xl px-6">
            <h2 className="text-2xl font-bold tracking-tight mb-8 flex items-center gap-3">
              <span className="flex h-8 w-8 items-center justify-center rounded-full bg-surface-800">
                <Calendar size={16} className="text-surface-500" />
              </span>
              Past Events
            </h2>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {past.map((event) => (
                <EventCard key={event.title} event={event} variant="past" />
              ))}
            </div>
          </div>
        </section>
      )}

      {/* Host your own CTA */}
      <section className="pb-20 border-t border-surface-800 pt-16">
        <div className="mx-auto max-w-3xl px-6 text-center">
          <h2 className="text-2xl md:text-3xl font-bold tracking-tight mb-4">
            Host Your Own Event
          </h2>
          <p className="text-surface-400 mb-6">
            Whether it&rsquo;s a meetup, workshop, or conference — we&rsquo;d love
            to support your community event. Reach out and we&rsquo;ll help promote it.
          </p>
          <a
            href="mailto:hello@openzep.com"
            className="inline-flex items-center gap-2 rounded-lg bg-brand-500 px-6 py-3 text-sm font-semibold text-white hover:bg-brand-400 transition-colors"
          >
            Get in Touch <ArrowRight size={16} />
          </a>
          <div className="mt-4">
            <Link
              href="/blog"
              className="text-sm text-surface-500 hover:text-surface-300 transition-colors"
            >
              Read about past events on our blog
            </Link>
          </div>
        </div>
      </section>

      <CtaSection />
    </>
  );
}
