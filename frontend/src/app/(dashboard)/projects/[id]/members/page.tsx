"use client";
import { RequireAuth } from "../../../require-auth";

import { useState, useEffect, useCallback } from "react";
import {
  Users,
  Plus,
  X,
  Trash2,
  AlertTriangle,
  User as UserIcon,
} from "lucide-react";
import {
  get,
  post,
  del as apiDel,
  ApiError,
  extractList,
} from "@/lib/api-client";
import { useProject } from "@/stores/project-context";
import { toast } from "sonner";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { TableSkeleton } from "@/components/shared/skeleton";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface Member {
  id: string;
  user_id: string;
  role: "owner" | "member";
  created_at: string;
}

interface UserItem {
  id: string;
  email?: string;
  name?: string;
  external_id?: string;
}

// ─── Helpers ───────────────────────────────────────────────────────────────────

function getUserLabel(user: UserItem): string {
  return user.email ?? user.name ?? user.external_id ?? user.id.slice(0, 8);
}

// ─── Page ──────────────────────────────────────────────────────────────────────

export default function ProjectMembersPage() {
  const { project, loading: projectLoading } = useProject();
  const projectId = project?.id;

  const [members, setMembers] = useState<Member[]>([]);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState("");

  // Add member
  const [showAdd, setShowAdd] = useState(false);
  const [users, setUsers] = useState<UserItem[]>([]);
  const [usersLoading, setUsersLoading] = useState(false);
  const [selectedUserId, setSelectedUserId] = useState("");
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState("");

  // Remove member
  const [removeTarget, setRemoveTarget] = useState<Member | null>(null);
  const [removing, setRemoving] = useState(false);

  const fetchMembers = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    setFetchError("");
    try {
      const json = await get<{ data: Member[] }>(
        `/v1/projects/${projectId}/members`,
      );
      setMembers(json.data ?? []);
    } catch (err) {
      setFetchError(
        err instanceof ApiError ? err.message : "Failed to load members",
      );
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    fetchMembers();
  }, [fetchMembers]);

  async function handleOpenAdd() {
    setAddError("");
    setSelectedUserId("");
    setAdding(false);
    setUsersLoading(true);
    try {
      const data = await get<{ data: UserItem[] }>("/v1/users?limit=200");
      setUsers(extractList<UserItem>(data));
    } catch {
      setAddError("Failed to load user list");
    } finally {
      setUsersLoading(false);
    }
    setShowAdd(true);
  }

  async function handleAddMember() {
    if (!projectId || !selectedUserId) return;
    setAdding(true);
    setAddError("");
    try {
      await post(`/v1/projects/${projectId}/members`, {
        user_id: selectedUserId,
        role: "member",
      });
      setShowAdd(false);
      toast.success("Member added");
      fetchMembers();
    } catch (err) {
      setAddError(
        err instanceof ApiError ? err.message : "Failed to add member",
      );
    } finally {
      setAdding(false);
    }
  }

  async function handleRemoveMember() {
    if (!projectId || !removeTarget) return;
    setRemoving(true);
    try {
      await apiDel(
        `/v1/projects/${projectId}/members/${removeTarget.user_id}`,
      );
      setMembers((prev) => prev.filter((m) => m.id !== removeTarget.id));
      setRemoveTarget(null);
      toast.success("Member removed");
    } catch (err) {
      toast.error(
        err instanceof ApiError ? err.message : "Failed to remove member",
      );
    } finally {
      setRemoving(false);
    }
  }

  const isOwner = (role: string) => role === "owner";
  const ownerCount = members.filter((m) => isOwner(m.role)).length;

  if (projectLoading) {
    return (
      <RequireAuth>
        <div className="space-y-6">
          <PageHeader title="Members" description="Project members" />
          <TableSkeleton rows={4} cols={3} colWidths={["w-32", "w-20", "w-16"]} />
        </div>
      </RequireAuth>
    );
  }

  return (
    <RequireAuth>
      <div className="space-y-6">
        <PageHeader
          title="Members"
          description={
            project
              ? `Manage who has access to "${project.name}"`
              : "Project member management"
          }
          actions={
            <Button
              variant="primary"
              size="sm"
              icon={<Plus size={14} />}
              onClick={handleOpenAdd}
            >
              Add Member
            </Button>
          }
        />

        {loading ? (
          <TableSkeleton
            rows={4}
            cols={3}
            colWidths={["w-32", "w-20", "w-16"]}
          />
        ) : fetchError ? (
          <div className="card-base p-12 flex flex-col items-center justify-center">
            <AlertTriangle size={36} className="text-error mb-3" />
            <p className="text-sm text-surface-300 mb-4">{fetchError}</p>
            <Button variant="secondary" size="sm" onClick={fetchMembers}>
              Retry
            </Button>
          </div>
        ) : members.length === 0 ? (
          <EmptyState
            icon={Users}
            title="No members yet"
            description="Add members to collaborate on this project."
            action={
              <Button
                variant="secondary"
                size="sm"
                icon={<Plus size={14} />}
                onClick={handleOpenAdd}
              >
                Add Member
              </Button>
            }
          />
        ) : (
          <div className="card-base overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-surface-800">
                  <th className="text-left text-xs font-medium text-surface-400 px-4 py-3">
                    User ID
                  </th>
                  <th className="text-left text-xs font-medium text-surface-400 px-4 py-3">
                    Role
                  </th>
                  <th className="text-left text-xs font-medium text-surface-400 px-4 py-3">
                    Added
                  </th>
                  <th className="text-right text-xs font-medium text-surface-400 px-4 py-3">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-surface-800">
                {members.map((member) => (
                  <tr
                    key={member.id}
                    className="transition-colors hover:bg-surface-800/50"
                  >
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <UserIcon size={14} className="text-surface-500" />
                        <span className="font-mono text-xs text-surface-200">
                          {member.user_id.slice(0, 8)}...
                        </span>
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <Badge
                        variant={isOwner(member.role) ? "brand" : "default"}
                        size="sm"
                      >
                        {member.role}
                      </Badge>
                    </td>
                    <td className="px-4 py-3 text-surface-400">
                      {new Date(member.created_at).toLocaleDateString()}
                    </td>
                    <td className="px-4 py-3 text-right">
                      {!isOwner(member.role) && (
                        <button
                          onClick={() => setRemoveTarget(member)}
                          className="btn-ghost p-1.5 text-surface-400 hover:text-error"
                          title="Remove member"
                        >
                          <Trash2 size={15} />
                        </button>
                      )}
                      {isOwner(member.role) && ownerCount <= 1 && (
                        <span className="text-xs text-surface-500">
                          Last owner
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* ── Add Member Dialog ──────────────────────────────────────────── */}
        {showAdd && (
          <>
            <div
              className="fixed inset-0 z-50 bg-black/60"
              onClick={() => !adding && setShowAdd(false)}
            />
            <div className="fixed left-1/2 top-1/2 z-50 w-full max-w-md -translate-x-1/2 -translate-y-1/2 card-base animate-slide-up">
              <div className="flex items-center justify-between px-6 py-4 border-b border-surface-800">
                <h2 className="text-base font-semibold text-[#F2F2F2]">
                  Add Member
                </h2>
                <button
                  onClick={() => !adding && setShowAdd(false)}
                  className="text-surface-400 hover:text-surface-200"
                  disabled={adding}
                >
                  <X size={18} />
                </button>
              </div>
              <div className="p-6 space-y-4">
                <div>
                  <label className="block text-sm font-medium text-surface-300 mb-1.5">
                    User
                  </label>
                  {usersLoading ? (
                    <div className="h-9 rounded-md bg-surface-800 animate-pulse" />
                  ) : (
                    <select
                      value={selectedUserId}
                      onChange={(e) => setSelectedUserId(e.target.value)}
                      className="input-base appearance-none cursor-pointer w-full"
                      disabled={adding || users.length === 0}
                    >
                      <option value="">
                        {users.length === 0
                          ? "No users available"
                          : "Select a user..."}
                      </option>
                      {users.map((user) => (
                        <option key={user.id} value={user.id}>
                          {getUserLabel(user)}
                        </option>
                      ))}
                    </select>
                  )}
                </div>
                {addError && (
                  <div className="rounded-md border border-error/20 bg-error/10 px-3 py-2 text-sm text-error">
                    {addError}
                  </div>
                )}
              </div>
              <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-surface-800">
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => setShowAdd(false)}
                  disabled={adding}
                >
                  Cancel
                </Button>
                <Button
                  variant="primary"
                  size="sm"
                  onClick={handleAddMember}
                  loading={adding}
                  disabled={!selectedUserId}
                >
                  Add Member
                </Button>
              </div>
            </div>
          </>
        )}

        {/* ── Remove Confirm ────────────────────────────────────────────── */}
        <ConfirmDialog
          open={!!removeTarget}
          title="Remove Member"
          message={`Are you sure you want to remove this member from the project?`}
          confirmLabel="Remove"
          variant="danger"
          loading={removing}
          onConfirm={handleRemoveMember}
          onCancel={() => setRemoveTarget(null)}
        />
      </div>
    </RequireAuth>
  );
}
