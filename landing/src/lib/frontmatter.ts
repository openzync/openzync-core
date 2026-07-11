// Parse YAML-ish key: value frontmatter from MDX file content. Stops at the second "---" delimiter.
export function parseFrontmatter(fileContent: string): {
  metadata: Record<string, string>;
  content: string;
} {
  const metadata: Record<string, string> = {};
  let content = fileContent;

  if (content.startsWith("---")) {
    const endIndex = content.indexOf("---", 3);
    if (endIndex !== -1) {
      const frontmatter = content.slice(3, endIndex).trim();
      content = content.slice(endIndex + 3).trim();

      for (const line of frontmatter.split("\n")) {
        const colonIndex = line.indexOf(":");
        if (colonIndex !== -1) {
          const key = line.slice(0, colonIndex).trim();
          const value = line.slice(colonIndex + 1).trim().replace(/^"|"$/g, "");
          metadata[key] = value;
        }
      }
    }
  }

  return { metadata, content };
}
