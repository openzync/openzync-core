/**
 * Supported Backends & Providers section.
 * Logo grid showing OpenZep's integration partners (graph backends and LLM providers).
 */
export function SponsorsSection() {
  return (
    <section className="py-20 md:py-28 border-t border-surface-800">
      <div className="mx-auto max-w-7xl px-6 text-center">
        <h2 className="text-3xl md:text-4xl font-bold tracking-tight mb-4">
          Supported Backends &amp; Providers
        </h2>
        <p className="text-surface-400 max-w-xl mx-auto mb-12">
          OpenZep integrates with leading graph databases and LLM providers out of the box.
        </p>

        {/* Logo grid */}
        <div className="flex flex-wrap items-center justify-center gap-6 max-w-3xl mx-auto">
          {["OpenAI", "Neo4j", "Anthropic", "FalkorDB", "Memgraph", "Google"].map((name) => (
            <div
              key={name}
              className="flex items-center justify-center h-14 px-8 rounded-xl border border-surface-700/60 bg-surface-900/80 shadow-sm"
            >
              <span className="text-sm font-bold text-surface-400 tracking-wider uppercase">
                {name}
              </span>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
