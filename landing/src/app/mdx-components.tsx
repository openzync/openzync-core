import type { MDXComponents } from "mdx/types";

/**
 * Custom MDX components for the landing site.
 * Used by @next/mdx to render markdown content.
 */
export function useMDXComponents(components: MDXComponents): MDXComponents {
  return {
    h1: ({ children, ...props }) => (
      <h1 className="text-3xl font-bold tracking-tight mb-4" {...props}>
        {children}
      </h1>
    ),
    h2: ({ children, ...props }) => (
      <h2 className="text-2xl font-semibold tracking-tight mb-3 mt-8" {...props}>
        {children}
      </h2>
    ),
    h3: ({ children, ...props }) => (
      <h3 className="text-xl font-semibold tracking-tight mb-2 mt-6" {...props}>
        {children}
      </h3>
    ),
    p: ({ children, ...props }) => (
      <p className="text-surface-300 leading-relaxed mb-4" {...props}>
        {children}
      </p>
    ),
    ul: ({ children, ...props }) => (
      <ul className="list-disc pl-6 text-surface-300 space-y-1 mb-4" {...props}>
        {children}
      </ul>
    ),
    ol: ({ children, ...props }) => (
      <ol className="list-decimal pl-6 text-surface-300 space-y-1 mb-4" {...props}>
        {children}
      </ol>
    ),
    a: ({ children, ...props }) => (
      <a className="text-accent-300 hover:text-accent-200 underline" {...props}>
        {children}
      </a>
    ),
    code: ({ children, ...props }) => (
      <code
        className="rounded bg-surface-800 px-1.5 py-0.5 text-sm font-mono text-accent-300"
        {...props}
      >
        {children}
      </code>
    ),
    pre: ({ children, ...props }) => (
      <pre
        className="rounded-lg bg-surface-900 border border-surface-800 p-4 overflow-x-auto mb-4"
        {...props}
      >
        {children}
      </pre>
    ),
    blockquote: ({ children, ...props }) => (
      <blockquote
        className="border-l-4 border-brand-500 pl-4 italic text-surface-400 mb-4"
        {...props}
      >
        {children}
      </blockquote>
    ),
    hr: (props) => <hr className="border-surface-800 my-8" {...props} />,
    ...components,
  };
}
