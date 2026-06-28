import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { Button } from "@openzep/design-system";
import { siteConfig } from "@/content/site-config";

/**
 * Call-to-action section used at the bottom of pages.
 */
export function CtaSection() {
  return (
    <section className="relative overflow-hidden py-20 md:py-28">
      {/* Background */}
      <div className="absolute inset-0 bg-surface-900/50 border-t border-surface-800" />

      <div className="relative z-10 mx-auto max-w-3xl px-6 text-center">
        <h2 className="text-3xl md:text-4xl font-bold tracking-tight mb-4">
          Ready to give your agents
          <br />
          <span className="text-brand-300">
            persistent memory
          </span>
          ?
        </h2>
        <p className="text-surface-400 text-lg mb-8 max-w-xl mx-auto">
          Start building in minutes. No credit card required.
        </p>
        <Link href={`${siteConfig.appUrl}/signup`}>
          <Button variant="primary" size="lg" icon={<ArrowRight size={18} />}>
            Get Started Free
          </Button>
        </Link>
      </div>
    </section>
  );
}
