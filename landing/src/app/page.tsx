import { Hero } from "@/components/landing/hero";
import { CommunityEvents } from "@/components/landing/community-events";
import { ReleaseSpotlight } from "@/components/landing/release-spotlight";
import { AudienceTabs } from "@/components/landing/audience-tabs";
import { StatsSection } from "@/components/landing/stats-section";
import { CaseStudies } from "@/components/landing/case-studies";
import { NewsSection } from "@/components/landing/news-section";
import { SponsorsSection } from "@/components/landing/sponsors-section";
import { CtaSection } from "@/components/landing/cta-section";

export default function HomePage() {
  return (
    <>
      <Hero />
      <CommunityEvents />
      <ReleaseSpotlight />
      <AudienceTabs />
      <StatsSection />
      <CaseStudies />
      <NewsSection />
      <SponsorsSection />
      <CtaSection />
    </>
  );
}
