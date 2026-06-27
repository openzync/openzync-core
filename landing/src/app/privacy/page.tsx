import type { Metadata } from "next";
import { Breadcrumbs, buildBreadcrumbSegments } from "@/components/landing/breadcrumbs";
import { CtaSection } from "@/components/landing/cta-section";

export const metadata: Metadata = {
  title: "Privacy Policy",
  description: "OpenZep privacy policy — how we collect, use, and protect your data.",
};

/**
 * Privacy Policy page — clean legal typography with sections.
 */
export default function PrivacyPage() {
  const segments = buildBreadcrumbSegments("/privacy");

  return (
    <>
      <section className="pt-36 pb-16">
        <div className="mx-auto max-w-3xl px-6">
          <Breadcrumbs segments={segments} />
          <h1 className="text-4xl md:text-5xl font-extrabold tracking-tight mb-4">
            Privacy Policy
          </h1>
          <p className="text-surface-500 text-sm mb-8">Last updated: March 15, 2026</p>
        </div>
      </section>

      <section className="pb-20">
        <div className="mx-auto max-w-3xl px-6 prose-custom">
          <Section title="Introduction">
            <p>
              OpenZep (&ldquo;we,&rdquo; &ldquo;our,&rdquo; or &ldquo;us&rdquo;) is committed to protecting
              your privacy. This Privacy Policy explains how we collect, use, disclose, and
              safeguard your information when you visit our website and use our services.
            </p>
          </Section>

          <Section title="Information We Collect">
            <p>We may collect the following types of information:</p>
            <ul>
              <li>
                <strong>Account Information:</strong> When you create an account, we collect your
                email address, name, and authentication credentials.
              </li>
              <li>
                <strong>Usage Data:</strong> We automatically collect information about how you
                interact with our services, including API calls, memory operations, and feature usage.
              </li>
              <li>
                <strong>Agent Data:</strong> The agent memory data you store through OpenZep is
                encrypted at rest and in transit. You retain full ownership of this data.
              </li>
              <li>
                <strong>Cookies:</strong> We use essential cookies for authentication and session
                management. Analytics cookies are used only with your consent.
              </li>
            </ul>
          </Section>

          <Section title="How We Use Your Information">
            <p>We use the collected information to:</p>
            <ul>
              <li>Provide, maintain, and improve our services</li>
              <li>Process transactions and manage your account</li>
              <li>Send technical notices, security alerts, and support messages</li>
              <li>Monitor usage patterns to improve performance and reliability</li>
              <li>Comply with legal obligations</li>
            </ul>
          </Section>

          <Section title="Data Security">
            <p>
              We implement industry-standard security measures to protect your data:
            </p>
            <ul>
              <li>Encryption at rest (AES-256) and in transit (TLS 1.3)</li>
              <li>Regular security audits and penetration testing</li>
              <li>Strict access controls and authentication requirements</li>
              <li>Automated threat detection and incident response</li>
            </ul>
          </Section>

          <Section title="Data Retention">
            <p>
              We retain your account information for as long as your account is active. Agent
              memory data is retained according to your configured retention policies. You can
              request deletion of your data at any time by contacting our support team.
            </p>
          </Section>

          <Section title="Third-Party Services">
            <p>
              OpenZep integrates with third-party services including graph databases and LLM
              providers. Data processed through these integrations is subject to their respective
              privacy policies. We recommend reviewing the privacy policies of any third-party
              services you connect to OpenZep.
            </p>
          </Section>

          <Section title="Your Rights">
            <p>Depending on your jurisdiction, you may have the right to:</p>
            <ul>
              <li>Access the personal data we hold about you</li>
              <li>Correct inaccurate or incomplete data</li>
              <li>Delete your personal data</li>
              <li>Object to or restrict processing of your data</li>
              <li>Data portability</li>
              <li>Withdraw consent at any time</li>
            </ul>
          </Section>

          <Section title="Contact Us">
            <p>
              If you have questions about this Privacy Policy or our data practices, please
              contact us at{" "}
              <a href="mailto:privacy@openzep.com" className="text-brand-300 hover:text-brand-200 transition-colors">
                privacy@openzep.com
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
