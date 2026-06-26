import Link from "next/link";
import { GitFork, Globe, Link as LinkIcon, Play, Mail } from "lucide-react";
import { siteConfig, footerLinks, footerSocialLinks } from "@/content/site-config";

const socialIcons: Record<string, typeof GitFork> = {
  github: GitFork,
  twitter: Globe,
  linkedin: LinkIcon,
  youtube: Play,
};

/**
 * Multi-column footer following Plone.org's pattern.
 * 5 link columns + social section + bottom bar.
 */
export function Footer() {
  return (
    <footer className="border-t border-surface-800 bg-surface-900">
      {/* Plone-style "Powering the Future" sponsor highlight bar */}
      <div className="border-b border-surface-800 bg-gradient-to-r from-brand-950/50 to-surface-950/50">
        <div className="mx-auto max-w-7xl px-6 py-8 text-center">
          <h3 className="text-lg font-semibold text-text-primary mb-2">
            Powering the Future of Agent Memory
          </h3>
          <p className="text-sm text-surface-400 max-w-2xl mx-auto mb-4">
            OpenZep thrives because of organizations that believe in open, secure, and
            sovereign AI infrastructure. We are deeply grateful to our community for
            making this possible.
          </p>
          <div className="flex items-center justify-center gap-3">
            <Link
              href={siteConfig.links.github}
              className="inline-flex items-center gap-1.5 text-sm font-medium text-brand-300 hover:text-brand-200 transition-colors"
            >
              <GitFork size={16} />
              Star on GitHub
            </Link>
            <span className="text-surface-700 text-sm">·</span>
            <Link
              href="https://github.com/sponsors/openzep"
              className="inline-flex items-center gap-1.5 text-sm font-medium text-brand-300 hover:text-brand-200 transition-colors"
            >
              Become a sponsor
            </Link>
          </div>
        </div>
      </div>

      {/* Link columns */}
      <div className="mx-auto max-w-7xl px-6 py-12 md:py-16">
        <div className="grid grid-cols-2 md:grid-cols-6 gap-8">
          {/* Brand column */}
          <div className="col-span-2 md:col-span-1">
            <Link href="/" className="flex items-center gap-2 mb-3">
              <span className="text-brand-500 font-bold text-xl">O</span>
              <div>
                <div className="text-sm font-semibold text-text-primary leading-tight">
                  {siteConfig.name}
                </div>
                <div className="text-[10px] text-surface-400 leading-tight">
                  {siteConfig.tagline}
                </div>
              </div>
            </Link>
            <p className="text-sm text-surface-400 leading-relaxed max-w-xs mt-3">
              Persistent, graph-based memory infrastructure for AI agents in production.
            </p>
            <div className="flex items-center gap-3 mt-4">
              {footerSocialLinks.map((s) => {
                const Icon = socialIcons[s.icon] ?? GitFork;
                return (
                  <a
                    key={s.label}
                    href={s.href}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-surface-400 hover:text-text-primary transition-colors"
                    aria-label={s.label}
                  >
                    <Icon size={18} />
                  </a>
                );
              })}
            </div>
          </div>

          {/* Product */}
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-widest text-surface-500 mb-4">
              Product
            </h4>
            <ul className="space-y-2.5">
              {footerLinks.product.map((link) => (
                <li key={link.label}>
                  {link.href.startsWith("http") ? (
                    <a href={link.href} target="_blank" rel="noopener noreferrer" className="text-sm text-surface-400 hover:text-text-primary transition-colors">
                      {link.label}
                    </a>
                  ) : (
                    <Link href={link.href} className="text-sm text-surface-400 hover:text-text-primary transition-colors">
                      {link.label}
                    </Link>
                  )}
                </li>
              ))}
            </ul>
          </div>

          {/* Community */}
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-widest text-surface-500 mb-4">
              Community
            </h4>
            <ul className="space-y-2.5">
              {footerLinks.community.map((link) => (
                <li key={link.label}>
                  {link.href.startsWith("http") ? (
                    <a href={link.href} target="_blank" rel="noopener noreferrer" className="text-sm text-surface-400 hover:text-text-primary transition-colors">
                      {link.label}
                    </a>
                  ) : (
                    <Link href={link.href} className="text-sm text-surface-400 hover:text-text-primary transition-colors">
                      {link.label}
                    </Link>
                  )}
                </li>
              ))}
            </ul>
          </div>

          {/* Updates */}
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-widest text-surface-500 mb-4">
              Updates
            </h4>
            <ul className="space-y-2.5">
              {footerLinks.updates.map((link) => (
                <li key={link.label}>
                  {link.href.startsWith("http") ? (
                    <a href={link.href} target="_blank" rel="noopener noreferrer" className="text-sm text-surface-400 hover:text-text-primary transition-colors">
                      {link.label}
                    </a>
                  ) : (
                    <Link href={link.href} className="text-sm text-surface-400 hover:text-text-primary transition-colors">
                      {link.label}
                    </Link>
                  )}
                </li>
              ))}
            </ul>
          </div>

          {/* Company + Legal */}
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-widest text-surface-500 mb-4">
              Company
            </h4>
            <ul className="space-y-2.5">
              {footerLinks.company.map((link) => (
                <li key={link.label}>
                  {link.href.startsWith("http") || link.href.startsWith("mailto:") ? (
                    <a href={link.href} target="_blank" rel="noopener noreferrer" className="text-sm text-surface-400 hover:text-text-primary transition-colors">
                      {link.label}
                    </a>
                  ) : (
                    <Link href={link.href} className="text-sm text-surface-400 hover:text-text-primary transition-colors">
                      {link.label}
                    </Link>
                  )}
                </li>
              ))}
            </ul>
          </div>

          {/* Social */}
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-widest text-surface-500 mb-4">
              Follow Us
            </h4>
            <ul className="space-y-2.5">
              {footerSocialLinks.map((s) => (
                <li key={s.label}>
                  <a
                    href={s.href}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-sm text-surface-400 hover:text-text-primary transition-colors"
                  >
                    {s.label}
                  </a>
                </li>
              ))}
              <li className="pt-2">
                <a
                  href="mailto:hello@openzep.com"
                  className="text-sm text-surface-400 hover:text-text-primary transition-colors inline-flex items-center gap-1"
                >
                  <Mail size={14} />
                  Email Us
                </a>
              </li>
            </ul>
          </div>
        </div>

        {/* Bottom bar */}
        <div className="mt-12 pt-6 border-t border-surface-800 flex flex-col sm:flex-row items-center justify-between gap-4">
          <p className="text-xs text-surface-500">
            &copy; {new Date().getFullYear()} {siteConfig.name}. All rights reserved.
          </p>
          <div className="flex items-center gap-4">
            {footerLinks.legal.map((link) => (
              link.href.startsWith("http") ? (
                <a key={link.label} href={link.href} target="_blank" rel="noopener noreferrer" className="text-xs text-surface-500 hover:text-text-primary transition-colors">
                  {link.label}
                </a>
              ) : (
                <Link key={link.label} href={link.href} className="text-xs text-surface-500 hover:text-text-primary transition-colors">
                  {link.label}
                </Link>
              )
            ))}
          </div>
        </div>
      </div>
    </footer>
  );
}
