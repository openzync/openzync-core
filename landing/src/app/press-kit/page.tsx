import type { Metadata } from "next";
import { Download, Palette, Type, Image } from "lucide-react";
import { Breadcrumbs, buildBreadcrumbSegments } from "@/components/landing/breadcrumbs";
import { CtaSection } from "@/components/landing/cta-section";

export const metadata: Metadata = {
  title: "Press Kit",
  description:
    "Download OpenZep brand assets, logos, color palette, and usage guidelines for media and press.",
};

const brandColors = [
  { name: "Brand Primary", hex: "#6366F1", class: "bg-[#6366F1]" },
  { name: "Brand Light", hex: "#A5B4FC", class: "bg-[#A5B4FC]" },
  { name: "Surface Dark", hex: "#0F0F12", class: "bg-[#0F0F12]" },
  { name: "Surface Card", hex: "#1A1A23", class: "bg-[#1A1A23]" },
  { name: "Text Primary", hex: "#F1F1F6", class: "bg-[#F1F1F6]" },
  { name: "Text Muted", hex: "#8B8BA7", class: "bg-[#8B8BA7]" },
];

const guidelines = [
  {
    title: "Logo Usage",
    items: [
      "Always use the OpenZep logo in its original form without modification.",
      "Maintain clear space around the logo equal to the height of the 'O' character.",
      "Do not rotate, stretch, or change the logo colors.",
      "On dark backgrounds, use the white version of the logo.",
      "On light backgrounds, use the dark version of the logo.",
    ],
  },
  {
    title: "Brand Name",
    items: [
      'Always capitalize "OpenZep" — never "openzep", "Openzep", or "OPENZEP".',
      'Refer to the project as "OpenZep" on first mention, then "OpenZep" or "the project" subsequently.',
      'The CLI tool and package name is "openzep" (lowercase).',
    ],
  },
  {
    title: "Attribution",
    items: [
      'When using OpenZep in your project, include: "Built with OpenZep" with a link to openzep.com.',
      "For academic papers, cite OpenZep using the provided citation format in our documentation.",
    ],
  },
];

/**
 * Press Kit page — brand assets, colors, typography, and usage guidelines.
 * Plone-style layout with card-based asset sections.
 */
export default function PressKitPage() {
  const segments = buildBreadcrumbSegments("/press-kit");

  return (
    <>
      {/* Header */}
      <section className="pt-28 pb-16">
        <div className="mx-auto max-w-4xl px-6">
          <Breadcrumbs segments={segments} />
          <h1 className="text-4xl md:text-5xl font-extrabold tracking-tight mb-4">
            Press Kit
          </h1>
          <p className="text-surface-400 text-lg max-w-2xl">
            Brand assets, logos, color palette, and usage guidelines for OpenZep.
            All assets are free to use under the Apache 2.0 license.
          </p>
        </div>
      </section>

      {/* Logo section */}
      <section className="pb-16 border-t border-surface-800 pt-16">
        <div className="mx-auto max-w-4xl px-6">
          <h2 className="text-2xl font-bold tracking-tight mb-2 flex items-center gap-3">
            <Image size={22} className="text-brand-300" />
            Logo
          </h2>
          <p className="text-surface-500 text-sm mb-8">
            SVG format, suitable for web and print.
          </p>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            {/* Default logo preview */}
            <div className="card-base p-8 flex flex-col items-center justify-center">
              <div className="text-4xl font-bold tracking-tight mb-4">
                <span className="text-brand-300">O</span>
                <span className="text-text-primary">penZep</span>
              </div>
              <p className="text-xs text-surface-500 mb-4">Default dark mode</p>
              <span className="inline-flex items-center gap-2 text-sm text-brand-300">
                <Download size={14} />
                Download SVG (dark)
              </span>
            </div>

            {/* Light logo preview */}
            <div className="card-base p-8 flex flex-col items-center justify-center bg-white">
              <div className="text-4xl font-bold tracking-tight mb-4 text-[#0F0F12]">
                <span className="text-[#6366F1]">O</span>
                <span>penZep</span>
              </div>
              <p className="text-xs text-surface-500 mb-4">Light mode variant</p>
              <span className="inline-flex items-center gap-2 text-sm text-brand-300">
                <Download size={14} />
                Download SVG (light)
              </span>
            </div>
          </div>
        </div>
      </section>

      {/* Color palette */}
      <section className="pb-16 border-t border-surface-800 pt-16">
        <div className="mx-auto max-w-4xl px-6">
          <h2 className="text-2xl font-bold tracking-tight mb-2 flex items-center gap-3">
            <Palette size={22} className="text-brand-300" />
            Color Palette
          </h2>
          <p className="text-surface-500 text-sm mb-8">
            The OpenZep brand colors. Use these consistently across all media.
          </p>

          <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
            {brandColors.map((color) => (
              <div key={color.name} className="card-base overflow-hidden">
                <div className={`h-20 ${color.class}`} />
                <div className="p-3">
                  <p className="text-sm font-medium text-text-primary">{color.name}</p>
                  <p className="text-xs text-surface-500 font-mono">{color.hex}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Typography */}
      <section className="pb-16 border-t border-surface-800 pt-16">
        <div className="mx-auto max-w-4xl px-6">
          <h2 className="text-2xl font-bold tracking-tight mb-2 flex items-center gap-3">
            <Type size={22} className="text-brand-300" />
            Typography
          </h2>
          <p className="text-surface-500 text-sm mb-8">
            OpenZep uses two typefaces across all surfaces.
          </p>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div className="card-base p-6">
              <p className="text-xs font-semibold text-surface-500 uppercase tracking-wider mb-3">
                Headings & Body
              </p>
              <p className="text-3xl font-bold mb-1" style={{ fontFamily: "Inter, sans-serif" }}>
                Inter
              </p>
              <p className="text-sm text-surface-400">
                Inter is a variable font designed for computer screens. Used for all
                headings, body text, and UI elements across the website and application.
              </p>
              <p className="text-xs text-surface-500 mt-3">
                Available on Google Fonts
              </p>
            </div>
            <div className="card-base p-6">
              <p className="text-xs font-semibold text-surface-500 uppercase tracking-wider mb-3">
                Code
              </p>
              <p className="text-3xl font-bold mb-1" style={{ fontFamily: "JetBrains Mono, monospace" }}>
                JetBrains Mono
              </p>
              <p className="text-sm text-surface-400">
                A monospace typeface for code snippets, CLI output, and technical
                documentation throughout the project.
              </p>
              <p className="text-xs text-surface-500 mt-3">
                Available on Google Fonts
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* Usage guidelines */}
      <section className="pb-20 border-t border-surface-800 pt-16">
        <div className="mx-auto max-w-4xl px-6">
          <h2 className="text-2xl font-bold tracking-tight mb-8">
            Usage Guidelines
          </h2>

          <div className="space-y-8">
            {guidelines.map((section) => (
              <div key={section.title}>
                <h3 className="text-lg font-semibold text-text-primary mb-3">{section.title}</h3>
                <ul className="space-y-2">
                  {section.items.map((item, i) => (
                    <li key={i} className="flex items-start gap-3 text-sm text-surface-400">
                      <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-brand-500" />
                      {item}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>

          <div className="mt-10 card-base p-6">
            <p className="text-sm text-surface-400">
              Need help with branding or have questions?{" "}
              <a href="mailto:hello@openzep.com" className="text-brand-300 hover:text-brand-200 transition-colors">
                Contact us
              </a>
              .
            </p>
          </div>
        </div>
      </section>

      <CtaSection />
    </>
  );
}
