import type { Metadata } from "next";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";

export const metadata: Metadata = {
  title: "Page Not Found",
  description: "The page you're looking for doesn't exist.",
};

/**
 * Custom 404 page — Plone-inspired layout with helpful links.
 */
export default function NotFound() {
  return (
    <div className="flex min-h-[70vh] flex-col items-center justify-center px-6">
      <div className="text-center max-w-lg">
        {/* Large 404 indicator */}
        <div className="text-8xl md:text-9xl font-extrabold tracking-tighter mb-4">
          <span className="bg-gradient-to-r from-brand-300 to-accent-300 bg-clip-text text-transparent">
            404
          </span>
        </div>

        <h1 className="text-2xl md:text-3xl font-bold tracking-tight mb-3">
          Page Not Found
        </h1>

        <p className="text-surface-400 mb-8 leading-relaxed">
          The page you&rsquo;re looking for doesn&rsquo;t exist or has been moved.
          Let&rsquo;s get you back on track.
        </p>

        <div className="flex flex-col sm:flex-row items-center justify-center gap-4">
          <Link
            href="/"
            className="inline-flex items-center gap-2 rounded-lg bg-brand-500 px-6 py-3 text-sm font-semibold text-white hover:bg-brand-400 transition-colors"
          >
            <ArrowLeft size={16} />
            Back to Home
          </Link>
          <Link
            href="/features"
            className="inline-flex items-center gap-2 rounded-lg border border-surface-700 px-6 py-3 text-sm font-medium text-surface-300 hover:bg-surface-800 transition-colors"
          >
            Explore Features
          </Link>
        </div>

        <div className="mt-10 flex flex-wrap justify-center gap-x-6 gap-y-2 text-sm">
          <Link href="/blog" className="text-surface-500 hover:text-surface-300 transition-colors">
            Blog
          </Link>
          <Link href="/changelog" className="text-surface-500 hover:text-surface-300 transition-colors">
            Changelog
          </Link>
          <Link href="/faq" className="text-surface-500 hover:text-surface-300 transition-colors">
            FAQ
          </Link>
          <Link href="/use-cases" className="text-surface-500 hover:text-surface-300 transition-colors">
            Use Cases
          </Link>
        </div>
      </div>
    </div>
  );
}
