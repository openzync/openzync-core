import type { Metadata } from "next";
import Link from "next/link";
import { Badge } from "@openzync/design-system";
import { getAllChangelogEntries } from "@/lib/changelog";
import { Calendar } from "lucide-react";

export const metadata: Metadata = {
  title: "Changelog",
  description: "Latest updates, features, and improvements to OpenZync.",
};

export default function ChangelogPage() {
  const entries = getAllChangelogEntries();

  return (
    <div className="pt-36 pb-20">
      <div className="mx-auto max-w-3xl px-6">
        {/* Header */}
        <div className="mb-12">
          <h1 className="text-4xl md:text-5xl font-extrabold tracking-tight mb-4">
            Changelog
          </h1>
          <p className="text-surface-400 text-lg">
            Latest updates, features, and improvements to OpenZync.
          </p>
        </div>

        {/* Entries */}
        {entries.length === 0 ? (
          <div className="text-center py-20">
            <p className="text-surface-500">No changelog entries yet.</p>
          </div>
        ) : (
          <div className="space-y-8">
            {entries.map((entry) => (
              <Link
                key={entry.slug}
                href={`/changelog/${entry.slug}`}
                className="card-interactive block p-6"
              >
                <div className="flex items-center gap-3 mb-3">
                  {entry.version && (
                    <Badge variant="brand" size="sm">
                      v{entry.version}
                    </Badge>
                  )}
                  {entry.date && (
                    <span className="flex items-center gap-1 text-xs text-surface-500">
                      <Calendar size={12} />
                      {new Date(entry.date).toLocaleDateString("en-US", {
                        year: "numeric",
                        month: "long",
                        day: "numeric",
                      })}
                    </span>
                  )}
                </div>
                <h2 className="text-xl font-semibold text-text-primary mb-2">
                  {entry.title}
                </h2>
                <p className="text-sm text-surface-400 leading-relaxed">
                  {entry.excerpt}
                </p>
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
