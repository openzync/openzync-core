import type { Metadata } from "next";
import { Breadcrumbs, buildBreadcrumbSegments } from "@/components/landing/breadcrumbs";
import { Accordion } from "@/components/landing/accordion";
import { faqItems } from "@/content/faq";
import { CtaSection } from "@/components/landing/cta-section";

export const metadata: Metadata = {
  title: "FAQ",
  description: "Frequently asked questions about OpenZync's persistent agent memory infrastructure.",
};

export default function FaqPage() {
  const segments = buildBreadcrumbSegments("/faq");

  return (
    <>
      <section className="pt-36 pb-16">
        <div className="mx-auto max-w-3xl px-6">
          <Breadcrumbs segments={segments} />
          <h1 className="text-4xl md:text-5xl font-extrabold tracking-tight mb-4">
            Frequently Asked Questions
          </h1>
          <p className="text-surface-400 text-lg">
            Everything you need to know about OpenZync.
          </p>
        </div>
      </section>

      <section className="pb-20">
        <div className="mx-auto max-w-3xl px-6">
          <Accordion items={faqItems} />
        </div>
      </section>

      <CtaSection />
    </>
  );
}
