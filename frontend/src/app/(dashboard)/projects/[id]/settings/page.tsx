"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { Settings, Save, Archive, AlertTriangle } from "lucide-react";
import { put, del, ApiError } from "@/lib/api-client";
import { useProject } from "@/stores/project-context";
import { toast } from "sonner";
import { PageHeader } from "@/components/shared/page-header";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";

export default function ProjectSettingsPage() {
  const router = useRouter();
  const { project, loading, refetch } = useProject();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");

  const [showArchiveConfirm, setShowArchiveConfirm] = useState(false);
  const [archiving, setArchiving] = useState(false);

  useEffect(() => {
    if (project) {
      setName(project.name);
      setDescription(project.description ?? "");
    }
  }, [project]);

  async function handleSave() {
    if (!name.trim() || !project) return;
    setSaving(true);
    setSaveError("");
    try {
      await put(`/v1/projects/${project.id}`, {
        name: name.trim(),
        description: description.trim() || null,
      });
      toast.success("Project settings saved");
      refetch();
    } catch (err) {
      setSaveError(
        err instanceof ApiError ? err.message : "Failed to save settings",
      );
    } finally {
      setSaving(false);
    }
  }

  async function handleArchive() {
    if (!project) return;
    setArchiving(true);
    try {
      await del(`/v1/projects/${project.id}`);
      setShowArchiveConfirm(false);
      toast.success(`Project "${project.name}" archived`);
      router.push("/projects");
    } catch (err) {
      toast.error(
        err instanceof ApiError ? err.message : "Failed to archive project",
      );
    } finally {
      setArchiving(false);
    }
  }

  if (loading) {
    return (
        <div className="space-y-6">
          <PageHeader title="Settings" description="Project settings" />
          <div className="card-base p-6 space-y-4 animate-pulse">
            <div className="h-5 w-32 bg-surface-800 rounded" />
            <div className="h-9 w-full bg-surface-800 rounded" />
            <div className="h-5 w-32 bg-surface-800 rounded" />
            <div className="h-20 w-full bg-surface-800 rounded" />
          </div>
        </div>
    );
  }

  if (!project) {
    return (
        <div className="card-base p-12 flex flex-col items-center justify-center">
          <AlertTriangle size={36} className="text-error mb-3" />
          <p className="text-sm text-surface-300">Project not found</p>
        </div>
    );
  }

  return (
      <div className="space-y-6">
        <PageHeader
          title="Project Settings"
          description={`Configure "${project.name}"`}
        />

        <div className="card-base p-6 space-y-5">
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1.5">
              Project Name
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="input-base max-w-md"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1.5">
              Description
            </label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="input-base max-w-md min-h-[80px] resize-y"
              placeholder="No description"
            />
          </div>

          {saveError && (
            <div className="rounded-md border border-error/20 bg-error/10 px-3 py-2 text-sm text-error max-w-md">
              {saveError}
            </div>
          )}

          <Button
            variant="primary"
            size="sm"
            icon={<Save size={14} />}
            onClick={handleSave}
            loading={saving}
            disabled={!name.trim()}
          >
            Save Changes
          </Button>
        </div>

        {/* Danger zone */}
        <div className="card-base p-6 border border-error/20">
          <h2 className="text-sm font-semibold text-error flex items-center gap-2 mb-2">
            <AlertTriangle size={16} />
            Danger Zone
          </h2>
          <p className="text-sm text-surface-400 mb-4">
            Archiving this project will soft-delete it. Sessions, memory, and
            graph data will be preserved but hidden. This action can be
            reversed by an administrator.
          </p>
          <Button
            variant="danger"
            size="sm"
            icon={<Archive size={14} />}
            onClick={() => setShowArchiveConfirm(true)}
          >
            Archive Project
          </Button>
        </div>

        <ConfirmDialog
          open={showArchiveConfirm}
          title="Archive Project"
          message={`Are you sure you want to archive "${project.name}"? All associated data will be hidden until an administrator restores it.`}
          confirmLabel="Archive"
          variant="danger"
          loading={archiving}
          onConfirm={handleArchive}
          onCancel={() => setShowArchiveConfirm(false)}
        />
      </div>
  );
}
