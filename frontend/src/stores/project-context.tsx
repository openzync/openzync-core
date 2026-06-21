"use client";

import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  type ReactNode,
} from "react";
import { useParams } from "next/navigation";
import { get, ApiError } from "@/lib/api-client";

// ─── Types ─────────────────────────────────────────────────────────────────────

export interface ProjectInfo {
  id: string;
  name: string;
  description: string | null;
  metadata: Record<string, unknown>;
  is_archived: boolean;
  member_count: number;
  created_by: string;
  created_at: string;
  updated_at: string;
}

interface ProjectContextValue {
  project: ProjectInfo | null;
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

// ─── Context ───────────────────────────────────────────────────────────────────

const ProjectContext = createContext<ProjectContextValue>({
  project: null,
  loading: true,
  error: null,
  refetch: () => {},
});

export function useProject(): ProjectContextValue {
  return useContext(ProjectContext);
}

// ─── Provider ──────────────────────────────────────────────────────────────────

export function ProjectProvider({ children }: { children: ReactNode }) {
  const params = useParams();
  // The route is /projects/[id]/* so the param is named "id"
  const projectId = params?.id as string | undefined;

  const [project, setProject] = useState<ProjectInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchProject = useCallback(async () => {
    if (!projectId) {
      setProject(null);
      setLoading(false);
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const data = await get<ProjectInfo>(`/v1/projects/${projectId}`);
      setProject(data);
    } catch (err) {
      if (err instanceof ApiError && err.isNotFound) {
        setError("Project not found");
      } else {
        setError(
          err instanceof ApiError ? err.message : "Failed to load project",
        );
      }
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    fetchProject();
  }, [fetchProject]);

  return (
    <ProjectContext.Provider value={{ project, loading, error, refetch: fetchProject }}>
      {children}
    </ProjectContext.Provider>
  );
}
