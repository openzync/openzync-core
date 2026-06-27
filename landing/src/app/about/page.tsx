import type { Metadata } from "next";
import Link from "next/link";
import { Breadcrumbs, buildBreadcrumbSegments } from "@/components/landing/breadcrumbs";
import { CtaSection } from "@/components/landing/cta-section";
import { Code, Shield, Zap, Heart, Globe, Users } from "lucide-react";
import { ArrowRight } from "lucide-react";

export const metadata: Metadata = {
  title: "About",
  description: "Learn about OpenZep, the team behind persistent agent memory infrastructure.",
};

const values = [
  {
    icon: Zap,
    title: "Performance First",
    description:
      "Agent memory should never be the bottleneck. Every layer — from storage to retrieval — is optimized for low-latency production workloads.",
  },
  {
    icon: Shield,
    title: "Production Safety",
    description:
      "Idempotency, audit trails, circuit breakers, and human-in-the-loop gates. We design for the worst case, not the happy path.",
  },
  {
    icon: Code,
    title: "Developer Experience",
    description:
      "Clean APIs, typed interfaces, comprehensive documentation, and predictable semantics. Your infrastructure should get out of your way.",
  },
];

const coreValues = [
  {
    icon: Heart,
    title: "OpenZep is human",
    description:
      "OpenZep is the core of a bright and passionate open source community that loves agent infrastructure and works to make sure others love it too. Built by humans, for humans.",
  },
  {
    icon: Globe,
    title: "OpenZep is power",
    description:
      "Powerful organizations need powerful agent infrastructure. Whether a small team or a global enterprise, the full power of OpenZep is available to everyone.",
  },
  {
    icon: Users,
    title: "OpenZep is community",
    description:
      "Backed by the OpenZep Foundation and a global community of contributors, OpenZep is free, open, and sovereign — forever.",
  },
];

/**
 * About page — Plone's "What is Plone?" pattern.
 * Mission + values + related links.
 */
export default function AboutPage() {
  const segments = buildBreadcrumbSegments("/about");

  return (
    <>
      {/* Header */}
      <section className="pt-36 pb-16 md:pb-20">
        <div className="mx-auto max-w-3xl px-6">
          <Breadcrumbs segments={segments} />
          <h1 className="text-4xl md:text-5xl font-extrabold tracking-tight mb-6">
            What is{" "}
            <span className="bg-gradient-to-r from-brand-300 to-accent-300 bg-clip-text text-transparent">
              OpenZep
            </span>
            ?
          </h1>

          <div className="flex flex-col md:flex-row gap-8 items-start mb-10">
            <div className="flex-1">
              <p className="text-surface-400 text-lg leading-relaxed mb-4">
                OpenZep is an open source persistent memory infrastructure for AI agents.
                It is used to create, store, and manage agent memory — like knowledge
                graphs, session context, and entity relationships — that agents can read
                from, write to, and reason over across conversations.
              </p>
              <p className="text-surface-400 leading-relaxed">
                It comes with over 2 years of focused development, support for 10+ graph
                backends, and integrations with 5+ LLM providers. The result is a system
                trusted by teams building production-grade agent systems.
              </p>
            </div>
          </div>

          {/* Core values (Plone pattern) */}
          <div className="space-y-8">
            {coreValues.map((v) => {
              const Icon = v.icon;
              return (
                <div key={v.title} className="flex gap-4 items-start">
                  <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-brand-500/10 mt-0.5">
                    <Icon size={22} className="text-brand-300" />
                  </div>
                  <div>
                    <h2 className="text-lg font-semibold text-text-primary mb-1">{v.title}</h2>
                    <p className="text-sm text-surface-400 leading-relaxed">{v.description}</p>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </section>

      {/* Mission */}
      <section className="py-16 border-t border-surface-800">
        <div className="mx-auto max-w-3xl px-6">
          <h2 className="text-2xl font-bold tracking-tight mb-4">Our Mission</h2>
          <p className="text-surface-400 leading-relaxed mb-4">
            Agents are transforming how we interact with software, but they are
            fundamentally limited without durable memory. Every reset loses
            context, every cold start wastes tokens re-building state, and every
            failure requires replaying from scratch.
          </p>
          <p className="text-surface-400 leading-relaxed">
            OpenZep solves this by providing a persistent, graph-based memory
            layer that agents can read from, write to, and reason over. We
            support 10+ graph backends, 5+ LLM providers, and are designed for
            production from day one — with observability, billing, and
            human-in-the-loop built into the core.
          </p>
        </div>
      </section>

      {/* Values (existing component) */}
      <section className="py-16 border-t border-surface-800">
        <div className="mx-auto max-w-7xl px-6">
          <h2 className="text-2xl font-bold tracking-tight text-center mb-12">
            What We Believe
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-8 max-w-4xl mx-auto">
            {values.map((value) => {
              const Icon = value.icon;
              return (
                <div key={value.title} className="card-base p-6">
                  <div className="flex h-11 w-11 items-center justify-center rounded-lg bg-brand-500/10 mb-4">
                    <Icon size={22} className="text-brand-300" />
                  </div>
                  <h3 className="text-lg font-semibold mb-2">{value.title}</h3>
                  <p className="text-sm text-surface-400 leading-relaxed">
                    {value.description}
                  </p>
                </div>
              );
            })}
          </div>
        </div>
      </section>

      {/* Related links (Plone pattern) */}
      <section className="py-16 border-t border-surface-800">
        <div className="mx-auto max-w-4xl px-6">
          <h2 className="text-2xl font-bold tracking-tight mb-8 text-center">
            Learn More
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <Link href="/features" className="card-interactive p-6 flex items-start gap-4">
              <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-brand-500/10">
                <Code size={22} className="text-brand-300" />
              </div>
              <div>
                <h3 className="text-base font-semibold text-text-primary mb-1">Features</h3>
                <p className="text-sm text-surface-400">Everything OpenZep offers</p>
                <span className="inline-flex items-center gap-1 text-sm text-brand-300 mt-2">Read more <ArrowRight size={14} /></span>
              </div>
            </Link>
            <Link href="/faq" className="card-interactive p-6 flex items-start gap-4">
              <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-brand-500/10">
                <Shield size={22} className="text-brand-300" />
              </div>
              <div>
                <h3 className="text-base font-semibold text-text-primary mb-1">FAQ</h3>
                <p className="text-sm text-surface-400">Frequently asked questions</p>
                <span className="inline-flex items-center gap-1 text-sm text-brand-300 mt-2">Read more <ArrowRight size={14} /></span>
              </div>
            </Link>
          </div>
        </div>
      </section>

      <CtaSection />
    </>
  );
}
