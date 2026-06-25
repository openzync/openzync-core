"use client";
import { RequireAuth } from "../require-auth";

import { useState, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";
import {
  Plus,
  FolderKanban,
  Settings,
  Users,
  ArrowRight,
  X,
  AlertTriangle,
  MapPin,
} from "lucide-react";
import { get, post, ApiError, extractList } from "@/lib/api-client";
import { formatDate } from "@/lib/utils";
import { toast } from "sonner";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { Button } from "@/components/ui/button";
import { usePinnedProjects } from "@/hooks/use-pinned-projects";


// ─── Types ─────────────────────────────────────────────────────────────────────

interface Project {
  id: string;
  name: string;
  description: string | null;
  member_count: number;
  created_by: string;
  created_at: string;
  updated_at: string;
}

interface ProjectsApiResponse {
  data: Project[];
}

// ─── Page ──────────────────────────────────────────────────────────────────────

export default function ProjectsPage() {
  const router = useRouter();

  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState("");

  const { togglePin, isPinned, isMaxPinned } = usePinnedProjects();

  // Create dialog
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");

  const fetchProjects = useCallback(async () => {
    setLoading(true);
    setFetchError("");
    try {
      const json = await get<ProjectsApiResponse>("/v1/projects");
      setProjects(extractList<Project>(json));
    } catch (err) {
      setFetchError(
        err instanceof ApiError ? err.message : "Network error loading projects",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchProjects();
  }, [fetchProjects]);

  async function handleCreate() {
    const name = newName.trim();
    if (!name) {
      setCreateError("Project name is required");
      return;
    }
    setCreating(true);
    setCreateError("");
    try {
      const body: Record<string, unknown> = { name };
      if (newDescription.trim()) body.description = newDescription.trim();
      const project = await post<Project>("/v1/projects", body);
      setShowCreate(false);
      setNewName("");
      setNewDescription("");
      toast.success(`Project "${project.name}" created`);
      router.push(`/projects/${project.id}/sessions`);
    } catch (err) {
      setCreateError(
        err instanceof ApiError ? err.message : "Failed to create project",
      );
    } finally {
      setCreating(false);
    }
  }

  return (
    <RequireAuth>
      <div className="space-y-6">
        <PageHeader
          title="Projects"
          description="Collaborative workspaces for memory and knowledge graph data"
          actions={
            <Button
              variant="primary"
              size="sm"
              icon={<Plus size={14} />}
              onClick={() => {
                setNewName("");
                setNewDescription("");
                setCreateError("");
                setShowCreate(true);
              }}
            >
              Create Project
            </Button>
          }
        />

        {/* Project grid */}
        {loading ? (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {[1, 2, 3].map((i) => (
              <div key={i} className="card-base p-6 h-40 animate-pulse">
                <div className="h-5 w-32 bg-surface-800 rounded mb-3" />
                <div className="h-3 w-full bg-surface-800 rounded mb-2" />
                <div className="h-3 w-3/4 bg-surface-800 rounded" />
              </div>
            ))}
          </div>
        ) : fetchError ? (
          <div className="card-base p-12 flex flex-col items-center justify-center">
            <AlertTriangle size={36} className="text-error mb-3" />
            <p className="text-sm text-surface-300 mb-4">{fetchError}</p>
            <Button variant="secondary" size="sm" onClick={fetchProjects}>
              Retry
            </Button>
          </div>
        ) : projects.length === 0 ? (
          <EmptyState
            icon={FolderKanban}
            title="No projects yet"
            description="Create your first project to start organising sessions, memory, and knowledge graphs."
            action={
              <Button
                variant="primary"
                size="sm"
                icon={<Plus size={14} />}
                onClick={() => {
                  setNewName("");
                  setNewDescription("");
                  setCreateError("");
                  setShowCreate(true);
                }}
              >
                Create Project
              </Button>
            }
          />
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {projects.map((project) => (
              <button
                key={project.id}
                onClick={() => router.push(`/projects/${project.id}/sessions`)}
                className="card-interactive p-5 text-left group"
              >
                <div className="flex items-start justify-between mb-3">
                  <div className="flex items-center gap-2.5 min-w-0">
                    <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-brand-500/10 text-brand-300">
                      <FolderKanban size={18} />
                    </div>
                    <div className="min-w-0">
                      <h3 className="font-semibold text-sm truncate">
                        {project.name}
                      </h3>
                      {project.description && (
                        <p className="text-xs text-surface-500 truncate mt-0.5">
                          {project.description}
                        </p>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-0.5 shrink-0 mt-1">
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        togglePin(project.id, project.name);
                      }}
                      disabled={isMaxPinned && !isPinned(project.id)}
                      title={
                        isPinned(project.id)
                          ? "Unpin project"
                          : isMaxPinned
                            ? "Maximum 3 pinned projects"
                            : "Pin project"
                      }
                      className="p-1.5 rounded-md opacity-0 group-hover:opacity-100 transition-opacity hover:bg-surface-800 disabled:opacity-30 disabled:cursor-not-allowed"
                    >
                      <MapPin
                        size={15}
                        className={
                          isPinned(project.id)
                            ? "text-brand-400 fill-brand-400"
                            : "text-surface-500"
                        }
                      />
                    </button>
                    <ArrowRight
                      size={16}
                      className="text-surface-500 opacity-0 group-hover:opacity-100 transition-opacity"
                    />
                  </div>
                </div>
                <div className="flex items-center gap-4 text-xs text-surface-500">
                  <span className="flex items-center gap-1">
                    <Users size={12} />
                    {project.member_count} member
                    {project.member_count !== 1 ? "s" : ""}
                  </span>
                  <span>Created {formatDate(project.created_at)}</span>
                </div>
              </button>
            ))}
          </div>
        )}

        {/* ── Create Dialog ──────────────────────────────────────────────── */}
        {showCreate && (
          <>
            <div
              className="fixed inset-0 z-50 bg-black/60"
              onClick={() => !creating && setShowCreate(false)}
            />
            <div className="fixed left-1/2 top-1/2 z-50 w-full max-w-md -translate-x-1/2 -translate-y-1/2 card-base animate-slide-up">
              <div className="flex items-center justify-between px-6 py-4 border-b border-surface-800">
                <h2 className="text-base font-semibold text-text-primary">
                  Create Project
                </h2>
                <button
                  onClick={() => !creating && setShowCreate(false)}
                  className="text-surface-400 hover:text-surface-200"
                  disabled={creating}
                >
                  <X size={18} />
                </button>
              </div>
              <div className="p-6 space-y-4">
                <div>
                  <label className="block text-sm font-medium text-surface-300 mb-1.5">
                    Name <span className="text-error">*</span>
                  </label>
                  <input
                    type="text"
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    placeholder="e.g., Customer Support Bot"
                    className="input-base"
                    autoFocus
                    disabled={creating}
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-surface-300 mb-1.5">
                    Description
                  </label>
                  <textarea
                    value={newDescription}
                    onChange={(e) => setNewDescription(e.target.value)}
                    placeholder="Optional description of this project"
                    className="input-base min-h-[80px] resize-y"
                    disabled={creating}
                  />
                </div>
                {createError && (
                  <div className="rounded-md border border-error/20 bg-error/10 px-3 py-2 text-sm text-error">
                    {createError}
                  </div>
                )}
              </div>
              <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-surface-800">
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => setShowCreate(false)}
                  disabled={creating}
                >
                  Cancel
                </Button>
                <Button
                  variant="primary"
                  size="sm"
                  onClick={handleCreate}
                  loading={creating}
                  disabled={!newName.trim()}
                >
                  Create
                </Button>
              </div>
            </div>
          </>
        )}
      </div>
    </RequireAuth>
  );
}
