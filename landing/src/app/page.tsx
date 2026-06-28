import { Hero } from "@/components/landing/hero";
import { CommunityEvents } from "@/components/landing/community-events";
import { ReleaseSpotlight } from "@/components/landing/release-spotlight";
import { AudienceTabs } from "@/components/landing/audience-tabs";
import { StatsSection } from "@/components/landing/stats-section";
import { CaseStudies } from "@/components/landing/case-studies";
import { NewsSection } from "@/components/landing/news-section";
import { SponsorsSection } from "@/components/landing/sponsors-section";
import { CtaSection } from "@/components/landing/cta-section";
import { FadeIn } from "@/components/landing/fade-in";

export default function HomePage() {
  return (
    <>
      <Hero />
      <FadeIn><CommunityEvents /></FadeIn>
      <FadeIn delay={80}><ReleaseSpotlight /></FadeIn>
      <FadeIn delay={160}><AudienceTabs /></FadeIn>
      <FadeIn delay={240}><StatsSection /></FadeIn>
      <FadeIn delay={320}><CaseStudies /></FadeIn>
      <FadeIn delay={400}><NewsSection /></FadeIn>
      <FadeIn delay={480}><SponsorsSection /></FadeIn>
      <FadeIn delay={560}><CtaSection /></FadeIn>
    </>
  );
}
