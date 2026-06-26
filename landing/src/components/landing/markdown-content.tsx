/**
 * Simple markdown-to-JSX renderer for changelog content.
 * Avoids adding a markdown library dependency for basic content rendering.
 * Supports: headings, paragraphs, lists, inline code, links, horizontal rules.
 */

interface MarkdownContentProps {
  content: string;
}

/**
 * Parse and render basic markdown content as React elements.
 * This is intentionally limited — supports the subset of markdown used in changelog entries.
 */
export function MarkdownContent({ content }: MarkdownContentProps) {
  const lines = content.split("\n");
  const elements: React.ReactNode[] = [];
  let inList: "ul" | "ol" | null = null;
  let listItems: React.ReactNode[] = [];

  function flushList() {
    if (inList && listItems.length > 0) {
      const ListTag = inList === "ul" ? "ul" : "ol";
      elements.push(
        <ListTag key={`list-${elements.length}`} className="list-disc pl-6 text-surface-300 space-y-1 mb-4">
          {listItems}
        </ListTag>,
      );
      listItems = [];
      inList = null;
    }
  }

  for (const line of lines) {
    const trimmed = line.trim();

    // Empty line
    if (!trimmed) {
      flushList();
      continue;
    }

    // Heading level 2
    if (trimmed.startsWith("## ")) {
      flushList();
      elements.push(
        <h2 key={`h2-${elements.length}`} className="text-2xl font-semibold tracking-tight mb-3 mt-8">
          {renderInline(trimmed.slice(3))}
        </h2>,
      );
      continue;
    }

    // Heading level 3
    if (trimmed.startsWith("### ")) {
      flushList();
      elements.push(
        <h3 key={`h3-${elements.length}`} className="text-xl font-semibold tracking-tight mb-2 mt-6">
          {renderInline(trimmed.slice(4))}
        </h3>,
      );
      continue;
    }

    // Unordered list item
    if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
      if (!inList) inList = "ul";
      listItems.push(
        <li key={`li-${listItems.length}`} className="text-surface-300">
          {renderInline(trimmed.slice(2))}
        </li>,
      );
      continue;
    }

    // Horizontal rule
    if (trimmed === "---") {
      flushList();
      elements.push(<hr key={`hr-${elements.length}`} className="border-surface-800 my-6" />);
      continue;
    }

    // Regular paragraph
    flushList();
    elements.push(
      <p key={`p-${elements.length}`} className="text-surface-300 leading-relaxed mb-4">
        {renderInline(trimmed)}
      </p>,
    );
  }

  flushList();

  return <>{elements}</>;
}

/** Render inline markdown: bold, code, links */
function renderInline(text: string): React.ReactNode {
  // Split by inline patterns
  const parts: React.ReactNode[] = [];
  let remaining = text;
  let key = 0;

  while (remaining.length > 0) {
    // Inline code: `code`
    const codeMatch = remaining.match(/`([^`]+)`/);
    if (codeMatch && codeMatch.index !== undefined) {
      if (codeMatch.index > 0) {
        parts.push(remaining.slice(0, codeMatch.index));
      }
      parts.push(
        <code
          key={`code-${key++}`}
          className="rounded bg-surface-800 px-1.5 py-0.5 text-sm font-mono text-accent-300"
        >
          {codeMatch[1]}
        </code>,
      );
      remaining = remaining.slice(codeMatch.index! + codeMatch[0].length);
      continue;
    }

    // Bold: **text**
    const boldMatch = remaining.match(/\*\*([^*]+)\*\*/);
    if (boldMatch && boldMatch.index !== undefined) {
      if (boldMatch.index > 0) {
        parts.push(remaining.slice(0, boldMatch.index));
      }
      parts.push(
        <strong key={`bold-${key++}`} className="font-semibold text-text-primary">
          {boldMatch[1]}
        </strong>,
      );
      remaining = remaining.slice(boldMatch.index! + boldMatch[0].length);
      continue;
    }

    // Link: [text](url)
    const linkMatch = remaining.match(/\[([^\]]+)\]\(([^)]+)\)/);
    if (linkMatch && linkMatch.index !== undefined) {
      if (linkMatch.index > 0) {
        parts.push(remaining.slice(0, linkMatch.index));
      }
      parts.push(
        <a
          key={`link-${key++}`}
          href={linkMatch[2]}
          className="text-accent-300 hover:text-accent-200 underline"
        >
          {linkMatch[1]}
        </a>,
      );
      remaining = remaining.slice(linkMatch.index! + linkMatch[0].length);
      continue;
    }

    // No more patterns found
    parts.push(remaining);
    break;
  }

  return parts.length === 1 ? parts[0] : parts;
}
