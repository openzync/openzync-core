import type { Metadata } from "next";
import { Breadcrumbs, buildBreadcrumbSegments } from "@/components/landing/breadcrumbs";
import { CtaSection } from "@/components/landing/cta-section";

export const metadata: Metadata = {
  title: "Terms of Service",
  description: "OpenZep terms of service — the terms governing use of our platform and services.",
};

/**
 * Terms of Service page — clean legal typography with sections.
 */
export default function TermsPage() {
  const segments = buildBreadcrumbSegments("/terms");

  return (
    <>
      <section className="pt-28 pb-16">
        <div className="mx-auto max-w-3xl px-6">
          <Breadcrumbs segments={segments} />
          <h1 className="text-4xl md:text-5xl font-extrabold tracking-tight mb-4">
            Terms of Service
          </h1>
          <p className="text-surface-500 text-sm mb-8">Last updated: March 15, 2026</p>
        </div>
      </section>

      <section className="pb-20">
        <div className="mx-auto max-w-3xl px-6">
          <Section title="Acceptance of Terms">
            <p>
              By accessing or using OpenZep (&ldquo;the Service&rdquo;), you agree to be bound by
              these Terms of Service. If you do not agree to all the terms, you may not access
              or use the Service.
            </p>
          </Section>

          <Section title="Description of Service">
            <p>
              OpenZep provides persistent, graph-based memory infrastructure for AI agents.
              The Service includes a cloud-hosted platform, open-source software, SDKs, APIs,
              and related documentation (&ldquo;the Software&rdquo;).
            </p>
          </Section>

          <Section title="Open Source License">
            <p>
              The OpenZep open-source software is licensed under the Apache License, Version 2.0.
              You may use, modify, and distribute the Software in accordance with that license.
              The Apache 2.0 license does not cover the cloud-hosted Service, which is governed
              by these Terms.
            </p>
          </Section>

          <Section title="Account Registration">
            <p>
              You are responsible for maintaining the confidentiality of your account credentials
              and for all activities that occur under your account. You must notify us immediately
              of any unauthorized use of your account.
            </p>
          </Section>

          <Section title="Acceptable Use">
            <p>You agree not to:</p>
            <ul>
              <li>Use the Service for any illegal or unauthorized purpose</li>
              <li>Attempt to bypass or circumvent any security measures</li>
              <li>Interfere with or disrupt the integrity or performance of the Service</li>
              <li>Use the Service to store or process sensitive personal data without appropriate safeguards</li>
              <li>Reverse engineer the cloud-hosted Service (the open-source Software is exempt)</li>
            </ul>
          </Section>

          <Section title="Data Ownership">
            <p>
              You retain all rights and ownership of the agent memory data you store using OpenZep.
              We do not claim any intellectual property rights over your data. We will not access,
              use, or share your data except as necessary to provide the Service or as required by law.
            </p>
          </Section>

          <Section title="Service Level">
            <p>
              We strive to provide a reliable and performant Service, but we do not guarantee
              that the Service will be uninterrupted or error-free. The open-source Software is
              provided &ldquo;as is&rdquo; without warranty of any kind.
            </p>
          </Section>

          <Section title="Limitation of Liability">
            <p>
              To the maximum extent permitted by law, OpenZep and its contributors shall not be
              liable for any indirect, incidental, special, consequential, or punitive damages
              arising out of or relating to your use of the Service or Software.
            </p>
          </Section>

          <Section title="Termination">
            <p>
              We reserve the right to suspend or terminate your access to the Service at any
              time for violations of these Terms or for any other reason. Upon termination,
              your right to use the Service will immediately cease.
            </p>
          </Section>

          <Section title="Changes to Terms">
            <p>
              We reserve the right to modify these Terms at any time. We will notify users of
              material changes via email or through the Service. Continued use of the Service
              after changes constitutes acceptance of the new Terms.
            </p>
          </Section>

          <Section title="Contact">
            <p>
              For questions about these Terms, please contact us at{" "}
              <a href="mailto:legal@openzep.com" className="text-brand-300 hover:text-brand-200 transition-colors">
                legal@openzep.com
              </a>
              .
            </p>
          </Section>
        </div>
      </section>

      <CtaSection />
    </>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-10">
      <h2 className="text-xl font-bold text-text-primary mb-4">{title}</h2>
      <div className="text-surface-400 leading-relaxed space-y-3 text-sm [&_ul]:space-y-2 [&_ul]:pl-5 [&_li]:list-disc [&_strong]:text-surface-300">
        {children}
      </div>
    </div>
  );
}
