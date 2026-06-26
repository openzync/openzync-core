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
      <FadeIn delay={100}><ReleaseSpotlight /></FadeIn>
      <FadeIn delay={200}><AudienceTabs /></FadeIn>
      <FadeIn delay={100}><StatsSection /></FadeIn>
      <FadeIn delay={200}><CaseStudies /></FadeIn>
      <FadeIn delay={100}><NewsSection /></FadeIn>
      <FadeIn delay={200}><SponsorsSection /></FadeIn>
      <FadeIn delay={300}><CtaSection /></FadeIn>
    </>
  );
}
