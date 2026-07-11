// Reusable section block for long-form legal pages (privacy, terms).
export function LegalSection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-10">
      <h2 className="text-xl font-bold text-text-primary mb-4">{title}</h2>
      <div className="text-surface-400 leading-relaxed space-y-3 text-sm [&_ul]:space-y-2 [&_ul]:pl-5 [&_li]:list-disc [&_strong]:text-surface-300">
        {children}
      </div>
    </div>
  );
}
