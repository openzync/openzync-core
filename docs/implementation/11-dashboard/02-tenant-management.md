# Tenant Management UI — Implementation Guide

> **Domain:** Admin Dashboard
> **SRS Phase:** Phase 4 — Dashboard & SDKs (Week 10-12)
> **Requirements:** DASH-02, MT-04, MT-05
> **Doc Dependencies:** [01-nextjs-setup.md](01-nextjs-setup.md), [02-auth-tenancy/01-api-key-auth.md](../02-auth-tenancy/01-api-key-auth.md), [02-auth-tenancy/03-tenant-isolation.md](../02-auth-tenancy/03-tenant-isolation.md)

---

## 1. Overview

The tenant management UI allows platform administrators to manage organisations (tenants), their API keys, and resource quotas. This is the primary interface for operational control of the OpenZep platform.

### 1.1 Pages

| Path | Page | Purpose |
|------|------|---------|
| `/dashboard/orgs` | Org list | Table of all organisations with search, pagination, quick stats |
| `/dashboard/orgs/new` | Create org | Form to create a new organisation |
| `/dashboard/orgs/[id]` | Org detail | View/edit org, manage keys, edit quotas |

### 1.2 Data Flow

```
Browser                      Next.js API Route                   FastAPI Backend
   │                              │                                   │
   │  GET /orgs                    │  GET /v1/admin/organizations     │
   │─────────────────────────────►│─────────────────────────────────►│
   │                              │  (BACKEND_API_KEY in header)     │
   │◄─── Org list (JSON) ────────│◄─── Organizations list ──────────│
   │                              │                                   │
   │  POST /api/proxy/.../keys    │  POST /v1/admin/orgs/{id}/keys   │
   │  {name}                      │─────────────────────────────────►│
   │                              │◄─── {key, prefix, id} ──────────│
   │◄─── Show-once modal ───────│ (key only returned once)          │
```

---

## 2. Organisation List Page

### 2.1 Page Component

```typescript
// src/app/dashboard/orgs/page.tsx
"use client";

import { useState, useEffect } from "react";
import Link from "next/link";
import { Building2, Plus, Search } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface Organisation {
  id: string;
  name: string;
  plan: string;
  created_at: string;
  user_count: number;
  key_count: number;
  quota_usage_pct: number;
}

export default function OrgListPage() {
  const [orgs, setOrgs] = useState<Organisation[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchOrgs();
  }, []);

  async function fetchOrgs() {
    setLoading(true);
    try {
      const res = await fetch("/api/proxy/admin/organizations");
      const data = await res.json();
      setOrgs(data.data || []);
    } finally {
      setLoading(false);
    }
  }

  const filtered = orgs.filter((o) =>
    o.name.toLowerCase().includes(search.toLowerCase())
  );

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Organisations</h1>
          <p className="text-muted-foreground">
            Manage tenants, API keys, and quotas
          </p>
        </div>
        <Link href="/dashboard/orgs/new">
          <Button>
            <Plus className="mr-2 h-4 w-4" />
            New Organisation
          </Button>
        </Link>
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-3 gap-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Total Organisations</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{orgs.length}</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Active Plans</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">
              {orgs.filter((o) => o.plan !== "free").length}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Total Users</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">
              {orgs.reduce((sum, o) => sum + o.user_count, 0).toLocaleString()}
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Search */}
      <div className="relative w-72">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
        <Input
          placeholder="Search organisations..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="pl-9"
        />
      </div>

      {/* Table */}
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Plan</TableHead>
              <TableHead>Users</TableHead>
              <TableHead>API Keys</TableHead>
              <TableHead>Quota Used</TableHead>
              <TableHead>Created</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {loading ? (
              <TableRow>
                <TableCell colSpan={6} className="text-center py-8">
                  Loading...
                </TableCell>
              </TableRow>
            ) : filtered.length === 0 ? (
              <TableRow>
                <TableCell colSpan={6} className="text-center py-8 text-muted-foreground">
                  No organisations found
                </TableCell>
              </TableRow>
            ) : (
              filtered.map((org) => (
                <TableRow key={org.id}>
                  <TableCell>
                    <Link
                      href={`/dashboard/orgs/${org.id}`}
                      className="flex items-center gap-2 hover:underline"
                    >
                      <Building2 className="h-4 w-4 text-muted-foreground" />
                      {org.name}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <Badge variant={org.plan === "free" ? "secondary" : "default"}>
                      {org.plan}
                    </Badge>
                  </TableCell>
                  <TableCell>{org.user_count.toLocaleString()}</TableCell>
                  <TableCell>{org.key_count}</TableCell>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <div className="h-2 flex-1 bg-muted rounded-full overflow-hidden">
                        <div
                          className="h-full bg-primary rounded-full"
                          style={{ width: `${org.quota_usage_pct}%` }}
                        />
                      </div>
                      <span className="text-xs text-muted-foreground">
                        {org.quota_usage_pct}%
                      </span>
                    </div>
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {new Date(org.created_at).toLocaleDateString()}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
```

---

## 3. Create Organisation Page

### 3.1 Page Component

```typescript
// src/app/dashboard/orgs/new/page.tsx
"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { toast } from "@/components/ui/use-toast";

const PLANS = [
  { value: "free", label: "Free", description: "100 users, 10k API calls/day" },
  { value: "starter", label: "Starter", description: "1,000 users, 100k API calls/day" },
  { value: "pro", label: "Pro", description: "10,000 users, 1M API calls/day" },
  { value: "enterprise", label: "Enterprise", description: "Unlimited users, custom limits" },
];

export default function CreateOrgPage() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [plan, setPlan] = useState("free");
  const [saving, setSaving] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;

    setSaving(true);
    try {
      const res = await fetch("/api/proxy/admin/organizations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim(), plan }),
      });

      if (!res.ok) throw new Error("Failed to create organisation");

      const org = await res.json();
      toast({ title: "Organisation created", description: org.name });
      router.push(`/dashboard/orgs/${org.id}`);
    } catch (err) {
      toast({
        title: "Error",
        description: err instanceof Error ? err.message : "Failed to create",
        variant: "destructive",
      });
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="max-w-xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Create Organisation</h1>
        <p className="text-muted-foreground">
          Create a new tenant with isolated data and API keys
        </p>
      </div>

      <form onSubmit={handleSubmit} className="space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>Organisation Details</CardTitle>
            <CardDescription>
              Basic information about the tenant
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="name">Organisation Name</Label>
              <Input
                id="name"
                placeholder="Acme Corp"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="plan">Plan</Label>
              <Select value={plan} onValueChange={setPlan}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {PLANS.map((p) => (
                    <SelectItem key={p.value} value={p.value}>
                      <div>
                        <span>{p.label}</span>
                        <span className="ml-2 text-xs text-muted-foreground">
                          {p.description}
                        </span>
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </CardContent>
        </Card>

        <div className="flex gap-3 justify-end">
          <Button
            type="button"
            variant="outline"
            onClick={() => router.back()}
          >
            Cancel
          </Button>
          <Button type="submit" disabled={saving || !name.trim()}>
            {saving ? "Creating..." : "Create Organisation"}
          </Button>
        </div>
      </form>
    </div>
  );
}
```

---

## 4. Organisation Detail Page

This is the most complex page — it has tabs for details, API keys, and quotas.

### 4.1 Page Structure

```typescript
// src/app/dashboard/orgs/[id]/page.tsx
"use client";

import { useState, useEffect } from "react";
import { useParams } from "next/navigation";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { OrgDetailsTab } from "@/components/orgs/org-details-tab";
import { OrgApiKeysTab } from "@/components/orgs/org-api-keys-tab";
import { OrgQuotaEditor } from "@/components/orgs/org-quota-editor";

export default function OrgDetailPage() {
  const params = useParams();
  const orgId = params.id as string;
  const [org, setOrg] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchOrg();
  }, [orgId]);

  async function fetchOrg() {
    setLoading(true);
    try {
      const res = await fetch(`/api/proxy/admin/organizations/${orgId}`);
      if (res.ok) {
        setOrg(await res.json());
      }
    } finally {
      setLoading(false);
    }
  }

  if (loading) return <div>Loading...</div>;
  if (!org) return <div>Organisation not found</div>;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{org.name}</h1>
        <p className="text-muted-foreground">ID: {org.id}</p>
      </div>

      <Tabs defaultValue="details">
        <TabsList>
          <TabsTrigger value="details">Details</TabsTrigger>
          <TabsTrigger value="keys">API Keys</TabsTrigger>
          <TabsTrigger value="quotas">Quotas</TabsTrigger>
        </TabsList>

        <TabsContent value="details" className="mt-6">
          <OrgDetailsTab org={org} onUpdate={fetchOrg} />
        </TabsContent>

        <TabsContent value="keys" className="mt-6">
          <OrgApiKeysTab orgId={orgId} />
        </TabsContent>

        <TabsContent value="quotas" className="mt-6">
          <OrgQuotaEditor orgId={orgId} quotas={org.quotas} onUpdate={fetchOrg} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
```

### 4.2 API Keys Tab

```typescript
// src/components/orgs/org-api-keys-tab.tsx
"use client";

import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Copy, Eye, EyeOff, Trash2, Plus } from "lucide-react";
import { toast } from "@/components/ui/use-toast";

interface ApiKey {
  id: string;
  prefix: string;
  name: string;
  status: "active" | "revoked";
  last_used_at: string | null;
  created_at: string;
}

export function OrgApiKeysTab({ orgId }: { orgId: string }) {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [newKeyName, setNewKeyName] = useState("");
  const [newKeyValue, setNewKeyValue] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    fetchKeys();
  }, [orgId]);

  async function fetchKeys() {
    setLoading(true);
    try {
      const res = await fetch(`/api/proxy/admin/organizations/${orgId}/keys`);
      const data = await res.json();
      setKeys(data.data || []);
    } finally {
      setLoading(false);
    }
  }

  async function handleCreateKey() {
    if (!newKeyName.trim()) return;
    setCreating(true);

    try {
      const res = await fetch(
        `/api/proxy/admin/organizations/${orgId}/keys`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: newKeyName.trim() }),
        }
      );

      if (!res.ok) throw new Error("Failed to create key");

      const result = await res.json();
      // The raw key is returned ONLY ONCE
      setNewKeyValue(result.raw_key);
      setNewKeyName("");
      await fetchKeys();
    } catch (err) {
      toast({
        title: "Error",
        description: "Failed to create API key",
        variant: "destructive",
      });
    } finally {
      setCreating(false);
    }
  }

  async function handleRevokeKey(keyId: string) {
    if (!confirm("Revoke this API key? This action cannot be undone.")) return;

    try {
      const res = await fetch(
        `/api/proxy/admin/organizations/${orgId}/keys/${keyId}`,
        { method: "DELETE" }
      );

      if (!res.ok) throw new Error("Failed to revoke key");

      toast({ title: "API key revoked" });
      await fetchKeys();
    } catch (err) {
      toast({
        title: "Error",
        description: "Failed to revoke key",
        variant: "destructive",
      });
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-medium">API Keys</h3>
          <p className="text-sm text-muted-foreground">
            Keys are shown once at creation. Keep them secure.
          </p>
        </div>
        <Dialog open={showCreateDialog} onOpenChange={setShowCreateDialog}>
          <DialogTrigger asChild>
            <Button>
              <Plus className="mr-2 h-4 w-4" />
              Generate Key
            </Button>
          </DialogTrigger>
          <DialogContent>
            {newKeyValue ? (
              /* Show-once modal — key is displayed exactly once */
              <>
                <DialogHeader>
                  <DialogTitle>API Key Created</DialogTitle>
                  <DialogDescription>
                    Copy this key now. You will not be able to see it again.
                  </DialogDescription>
                </DialogHeader>
                <div className="space-y-4">
                  <div className="flex items-center gap-2 p-3 bg-muted rounded-md font-mono text-sm">
                    <span className="flex-1 break-all">{newKeyValue}</span>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        navigator.clipboard.writeText(newKeyValue);
                        toast({ title: "Copied to clipboard" });
                      }}
                    >
                      <Copy className="h-4 w-4" />
                    </Button>
                  </div>
                  <Button
                    className="w-full"
                    onClick={() => {
                      setNewKeyValue(null);
                      setShowCreateDialog(false);
                    }}
                  >
                    Done — I've saved the key
                  </Button>
                </div>
              </>
            ) : (
              /* Create key form */
              <>
                <DialogHeader>
                  <DialogTitle>Generate API Key</DialogTitle>
                  <DialogDescription>
                    Create a new API key for this organisation.
                    The key will start with{" "}
                    <code className="text-xs bg-muted px-1 rounded">mg_live_</code>.
                  </DialogDescription>
                </DialogHeader>
                <div className="space-y-4">
                  <div className="space-y-2">
                    <Label htmlFor="key-name">Key Name</Label>
                    <Input
                      id="key-name"
                      placeholder="e.g., Production - Claude Integration"
                      value={newKeyName}
                      onChange={(e) => setNewKeyName(e.target.value)}
                    />
                  </div>
                  <Button
                    className="w-full"
                    disabled={creating || !newKeyName.trim()}
                    onClick={handleCreateKey}
                  >
                    {creating ? "Generating..." : "Generate Key"}
                  </Button>
                </div>
              </>
            )}
          </DialogContent>
        </Dialog>
      </div>

      {/* Keys table */}
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Key Prefix</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Last Used</TableHead>
              <TableHead>Created</TableHead>
              <TableHead className="w-12"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {keys.map((key) => (
              <TableRow key={key.id}>
                <TableCell className="font-medium">{key.name}</TableCell>
                <TableCell>
                  <code className="text-xs bg-muted px-1.5 py-0.5 rounded">
                    {key.prefix}...{key.id.slice(-4)}
                  </code>
                </TableCell>
                <TableCell>
                  <Badge
                    variant={key.status === "active" ? "default" : "destructive"}
                  >
                    {key.status}
                  </Badge>
                </TableCell>
                <TableCell className="text-muted-foreground">
                  {key.last_used_at
                    ? new Date(key.last_used_at).toLocaleString()
                    : "Never"}
                </TableCell>
                <TableCell className="text-muted-foreground">
                  {new Date(key.created_at).toLocaleDateString()}
                </TableCell>
                <TableCell>
                  {key.status === "active" && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => handleRevokeKey(key.id)}
                    >
                      <Trash2 className="h-4 w-4 text-destructive" />
                    </Button>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
```

### 4.3 Quota Editor Tab

```typescript
// src/components/orgs/org-quota-editor.tsx
"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { toast } from "@/components/ui/use-toast";

interface Quotas {
  max_users: number;
  max_api_calls_per_day: number;
  max_graph_nodes: number;
  enrichment_depth: 0 | 1 | 2 | 3;
  max_sessions: number;
  max_facts_per_user: number;
}

interface OrgQuotaEditorProps {
  orgId: string;
  quotas: Quotas | null;
  onUpdate: () => void;
}

const DEFAULT_QUOTAS: Quotas = {
  max_users: 100,
  max_api_calls_per_day: 10000,
  max_graph_nodes: 5000,
  enrichment_depth: 1,
  max_sessions: 500,
  max_facts_per_user: 1000,
};

export function OrgQuotaEditor({ orgId, quotas, onUpdate }: OrgQuotaEditorProps) {
  const [values, setValues] = useState<Quotas>(quotas || DEFAULT_QUOTAS);
  const [saving, setSaving] = useState(false);

  async function handleSave() {
    setSaving(true);
    try {
      const res = await fetch(`/api/proxy/admin/organizations/${orgId}/quotas`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      });

      if (!res.ok) throw new Error("Failed to update quotas");

      toast({ title: "Quotas updated" });
      onUpdate();
    } catch (err) {
      toast({
        title: "Error",
        description: "Failed to update quotas",
        variant: "destructive",
      });
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>Resource Quotas</CardTitle>
          <CardDescription>
            Set limits for this organisation. Changes take effect immediately.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* User limit */}
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label htmlFor="max_users">Max Users</Label>
              <Input
                id="max_users"
                type="number"
                min={0}
                value={values.max_users}
                onChange={(e) =>
                  setValues({ ...values, max_users: parseInt(e.target.value) || 0 })
                }
              />
              <p className="text-xs text-muted-foreground">
                0 = unlimited
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="max_api_calls">Max API Calls / Day</Label>
              <Input
                id="max_api_calls"
                type="number"
                min={0}
                value={values.max_api_calls_per_day}
                onChange={(e) =>
                  setValues({
                    ...values,
                    max_api_calls_per_day: parseInt(e.target.value) || 0,
                  })
                }
              />
            </div>
          </div>

          {/* Graph & enrichment */}
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label htmlFor="max_graph_nodes">Max Graph Nodes</Label>
              <Input
                id="max_graph_nodes"
                type="number"
                min={0}
                value={values.max_graph_nodes}
                onChange={(e) =>
                  setValues({
                    ...values,
                    max_graph_nodes: parseInt(e.target.value) || 0,
                  })
                }
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="enrichment_depth">Enrichment Depth Level</Label>
              <select
                id="enrichment_depth"
                className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                value={values.enrichment_depth}
                onChange={(e) =>
                  setValues({
                    ...values,
                    enrichment_depth: parseInt(e.target.value) as 0 | 1 | 2 | 3,
                  })
                }
              >
                <option value={0}>None (no enrichment)</option>
                <option value={1}>Basic (entity extraction)</option>
                <option value={2}>Standard (entities + facts)</option>
                <option value={3}>Full (entities + facts + classification)</option>
              </select>
              <p className="text-xs text-muted-foreground">
                Higher levels consume more LLM tokens
              </p>
            </div>
          </div>

          {/* Session & facts */}
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label htmlFor="max_sessions">Max Sessions per User</Label>
              <Input
                id="max_sessions"
                type="number"
                min={0}
                value={values.max_sessions}
                onChange={(e) =>
                  setValues({ ...values, max_sessions: parseInt(e.target.value) || 0 })
                }
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="max_facts">Max Facts per User</Label>
              <Input
                id="max_facts"
                type="number"
                min={0}
                value={values.max_facts_per_user}
                onChange={(e) =>
                  setValues({
                    ...values,
                    max_facts_per_user: parseInt(e.target.value) || 0,
                  })
                }
              />
            </div>
          </div>
        </CardContent>
      </Card>

      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={saving}>
          {saving ? "Saving..." : "Save Quotas"}
        </Button>
      </div>
    </div>
  );
}
```

---

## 5. Super Admin Features

### 5.1 Cross-Tenant View

Super admins see an additional tab on the org detail page showing cross-tenant statistics:

```typescript
// In the org detail page, conditionally render super admin features
// This is determined by the user's JWT token role claim

const { user } = useAuth();
const isSuperAdmin = user?.role === "super_admin";

// In the JSX:
{isSuperAdmin && (
  <Card>
    <CardHeader>
      <CardTitle>Super Admin Controls</CardTitle>
    </CardHeader>
    <CardContent>
      <p className="text-sm text-muted-foreground mb-4">
        You have cross-tenant access. You can view and manage data across
        all organisations.
      </p>
      <div className="flex gap-2">
        <Button variant="outline" onClick={/* impersonate org */}>
          View as this organisation
        </Button>
        <Button variant="destructive" onClick={/* suspend org */}>
          Suspend Organisation
        </Button>
      </div>
    </CardContent>
  </Card>
)}
```

### 5.2 Super Key Management

Super API keys bypass tenant isolation and provide cross-tenant access. Manage them from a dedicated settings page:

```typescript
// src/app/dashboard/settings/page.tsx
// Super keys are created/revoked from the Settings page, not per-org

"use client";

import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { toast } from "@/components/ui/use-toast";

interface SuperKey {
  id: string;
  prefix: string;
  name: string;
  status: "active" | "revoked";
  last_used_at: string | null;
  created_at: string;
}

export default function SettingsPage() {
  const [superKeys, setSuperKeys] = useState<SuperKey[]>([]);
  // ... similar to API keys tab but calls /api/proxy/admin/super-keys
}
```

---

## 6. API Integration Pattern

### 6.1 All Calls Go Through Next.js API Routes

**Never call the backend directly from the browser.** The proxy pattern:

```
Browser                   Next.js Server                  Backend
   │                          │                              │
   │ fetch(/api/proxy/...)    │ fetch(http://api:8000/v1/...) │
   │────────────────────────►│──────────────────────────────►│
   │                         │  Authorization: Bearer KEY    │
   │                         │  (server-side, not in browser)│
   │◄────────────────────────│◄──────────────────────────────│
```

### 6.2 Admin API Routes

The backend should expose these admin endpoints:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/admin/organizations` | List all orgs (paginated) |
| `POST` | `/v1/admin/organizations` | Create org |
| `GET` | `/v1/admin/organizations/{id}` | Get org details |
| `PUT` | `/v1/admin/organizations/{id}` | Update org (name, plan) |
| `DELETE` | `/v1/admin/organizations/{id}` | Delete org (cascade) |
| `PUT` | `/v1/admin/organizations/{id}/quotas` | Update quotas |
| `GET` | `/v1/admin/organizations/{id}/keys` | List API keys (no raw key values) |
| `POST` | `/v1/admin/organizations/{id}/keys` | Generate API key |
| `DELETE` | `/v1/admin/organizations/{id}/keys/{key_id}` | Revoke API key |
| `GET` | `/v1/admin/users` | List users across orgs (super admin) |
| `GET` | `/v1/admin/stats` | Aggregated platform stats |

### 6.3 Pagination (All List Endpoints)

```typescript
// Shared pagination pattern for all list pages

interface PaginatedResponse<T> {
  data: T[];
  next_cursor: string | null;
  has_more: boolean;
  total: number;
}

// Cursor-based pagination hook
function usePaginatedQuery<T>(
  url: string,
  limit: number = 20
) {
  const [items, setItems] = useState<T[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);

  async function loadMore() {
    setLoading(true);
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set("cursor", cursor);

    const res = await fetch(`${url}?${params}`);
    const data: PaginatedResponse<T> = await res.json();

    setItems((prev) => [...prev, ...data.data]);
    setCursor(data.next_cursor);
    setHasMore(data.has_more);
    setLoading(false);
  }

  useEffect(() => { loadMore(); }, [url]);

  return { items, loading, hasMore, loadMore };
}
```

---

## 7. Error Handling

### 7.1 API Error Display

```typescript
// src/lib/api-errors.ts

interface ApiError {
  error: {
    code: string;
    message: string;
    request_id: string;
  };
}

export function handleApiError(err: unknown): string {
  if (err instanceof Response) {
    // Try to parse structured error
    return "API request failed";
  }
  if (err instanceof Error) return err.message;
  return "An unexpected error occurred";
}

// Usage in components:
// catch (err) {
//   toast({ title: "Error", description: handleApiError(err), variant: "destructive" });
// }
```

---

## 8. Testing

### 8.1 Component Tests

```typescript
// __tests__/components/orgs/org-api-keys-tab.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { OrgApiKeysTab } from "@/components/orgs/org-api-keys-tab";

// Mock fetch
global.fetch = vi.fn();

describe("OrgApiKeysTab", () => {
  it("displays API keys in a table", async () => {
    (fetch as any).mockResolvedValueOnce({
      ok: true,
      json: () =>
        Promise.resolve({
          data: [
            {
              id: "key_1",
              prefix: "mg_live_",
              name: "Production Key",
              status: "active",
              last_used_at: "2026-06-04T10:00:00Z",
              created_at: "2026-05-01T00:00:00Z",
            },
          ],
        }),
    });

    render(<OrgApiKeysTab orgId="org_123" />);

    expect(await screen.findByText("Production Key")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
  });

  it("opens create key dialog on button click", async () => {
    (fetch as any).mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ data: [] }),
    });

    render(<OrgApiKeysTab orgId="org_123" />);

    await userEvent.click(screen.getByText("Generate Key"));
    expect(screen.getByText("Key Name")).toBeInTheDocument();
  });
});
```

---

## 9. Security Notes

| Concern | Mitigation |
|---------|------------|
| **API key leak** | Raw key shown only once in a show-once modal. Stored as bcrypt hash in DB. |
| **CSRF** | All dashboard API calls go through Next.js API routes with SameSite=Strict cookies. |
| **XSS** | HttpOnly cookies for tokens. React's built-in XSS protection for all rendered content. |
| **Cross-tenant access** | Enforced at the backend query layer (organization_id filter). Dashboard never exposes raw backend credentials to the browser. |
| **Rate limiting** | Applied at the backend, not the dashboard. Dashboard proxy passes through rate limit errors. |
| **Audit logging** | All admin actions (key creation, quota changes, org creation) should be logged with admin identity and timestamp. |

---

*Corresponding SRS requirements: DASH-02, MT-04, MT-05. Next: [03-user-graph-explorer.md](03-user-graph-explorer.md) for the interactive graph visualisation.*
