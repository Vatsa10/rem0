"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";
import { api } from "@/lib/api";
import {
  Subscriber,
  PaginatedResponse,
  STATUS_COLORS,
  LANGUAGES,
} from "@/lib/types";
import { useCallSubscriber } from "@/hooks/use-call-subscriber";

const EMPTY_FORM = {
  name: "",
  phone: "",
  email: "",
  subscription_id: "",
  subscription_type: "",
  renewal_date: "",
  amount: "",
  language: "hi-IN",
};

export default function SubscribersPage() {
  const [data, setData] = useState<PaginatedResponse<Subscriber> | null>(null);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [page, setPage] = useState(1);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const { callSubscriber, callingId } = useCallSubscriber();

  const load = useCallback(() => {
    const params: Record<string, unknown> = { page, limit: 20 };
    if (search) params.search = search;
    if (statusFilter) params.status = statusFilter;
    api.getSubscribers(params).then((d) => setData(d as PaginatedResponse<Subscriber>));
  }, [page, search, statusFilter]);

  useEffect(() => { load(); }, [load]);

  const openCreate = () => {
    setEditingId(null);
    setForm(EMPTY_FORM);
    setDialogOpen(true);
  };

  const openEdit = (sub: Subscriber) => {
    setEditingId(sub.id);
    setForm({
      name: sub.name,
      phone: sub.phone,
      email: sub.email,
      subscription_id: sub.subscription_id,
      subscription_type: sub.subscription_type,
      renewal_date: sub.renewal_date,
      amount: sub.amount,
      language: sub.language,
    });
    setDialogOpen(true);
  };

  const handleSave = async () => {
    try {
      if (editingId) {
        await api.updateSubscriber(editingId, form);
        toast.success("Subscriber updated");
      } else {
        await api.createSubscriber(form);
        toast.success("Subscriber created");
      }
      setDialogOpen(false);
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to save");
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Delete this subscriber?")) return;
    try {
      await api.deleteSubscriber(id);
      toast.success("Subscriber deleted");
      load();
    } catch {
      toast.error("Failed to delete");
    }
  };

  const totalPages = data ? Math.ceil(data.total / data.limit) : 0;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-900">Subscribers</h1>
        <Button onClick={openCreate}>Add Subscriber</Button>
        <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
          <DialogContent className="max-w-lg">
            <DialogHeader>
              <DialogTitle>{editingId ? "Edit" : "Add"} Subscriber</DialogTitle>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label>Name</Label>
                  <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
                </div>
                <div className="space-y-2">
                  <Label>Phone</Label>
                  <Input value={form.phone} onChange={(e) => setForm({ ...form, phone: e.target.value })} placeholder="+91..." />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label>Email</Label>
                  <Input value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
                </div>
                <div className="space-y-2">
                  <Label>Subscription ID</Label>
                  <Input value={form.subscription_id} onChange={(e) => setForm({ ...form, subscription_id: e.target.value })} />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label>Subscription Type</Label>
                  <Input value={form.subscription_type} onChange={(e) => setForm({ ...form, subscription_type: e.target.value })} placeholder="Netflix, Gym..." />
                </div>
                <div className="space-y-2">
                  <Label>Renewal Date</Label>
                  <Input type="date" value={form.renewal_date} onChange={(e) => setForm({ ...form, renewal_date: e.target.value })} />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label>Amount</Label>
                  <Input value={form.amount} onChange={(e) => setForm({ ...form, amount: e.target.value })} placeholder="649/month" />
                </div>
                <div className="space-y-2">
                  <Label>Language</Label>
                  <Select value={form.language} onValueChange={(v) => { if (v) setForm({ ...form, language: v }); }}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {Object.entries(LANGUAGES).map(([code, name]) => (
                        <SelectItem key={code} value={code}>{name}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
              <Button onClick={handleSave} className="mt-2">
                {editingId ? "Update" : "Create"} Subscriber
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>

      <div className="flex gap-4">
        <Input
          placeholder="Search by name, phone, or ID..."
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1); }}
          className="max-w-sm"
        />
        <Select value={statusFilter} onValueChange={(v) => { setStatusFilter(!v || v === "ALL" ? "" : v); setPage(1); }}>
          <SelectTrigger className="w-48">
            <SelectValue placeholder="Filter by status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="ALL">All Statuses</SelectItem>
            {["NEW", "CONTACTED", "RENEWED", "FOLLOW_UP_NEEDED", "NOT_INTERESTED", "EXPIRED"].map((s) => (
              <SelectItem key={s} value={s}>{s.replace(/_/g, " ")}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="rounded-lg border bg-white">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Phone</TableHead>
              <TableHead>Type</TableHead>
              <TableHead>Renewal Date</TableHead>
              <TableHead>Amount</TableHead>
              <TableHead>Language</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data?.items.map((sub) => (
              <TableRow key={sub.id}>
                <TableCell className="font-medium">{sub.name}</TableCell>
                <TableCell>{sub.phone}</TableCell>
                <TableCell>{sub.subscription_type}</TableCell>
                <TableCell>{sub.renewal_date}</TableCell>
                <TableCell>{sub.amount || "-"}</TableCell>
                <TableCell className="text-xs">{LANGUAGES[sub.language] || sub.language}</TableCell>
                <TableCell>
                  <Badge className={STATUS_COLORS[sub.status] || "bg-gray-100"}>
                    {sub.status.replace(/_/g, " ")}
                  </Badge>
                </TableCell>
                <TableCell>
                  <div className="flex gap-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="text-green-700"
                      disabled={callingId === sub.id}
                      onClick={() => callSubscriber(sub.id, sub.name)}
                    >
                      {callingId === sub.id ? "..." : "Call"}
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => openEdit(sub)}>
                      Edit
                    </Button>
                    <Button variant="ghost" size="sm" className="text-red-600" onClick={() => handleDelete(sub.id)}>
                      Delete
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
            {data?.items.length === 0 && (
              <TableRow>
                <TableCell colSpan={8} className="py-8 text-center text-gray-500">
                  No subscribers found
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      {totalPages > 1 && (
        <div className="flex items-center justify-between">
          <p className="text-sm text-gray-500">
            Showing {((page - 1) * 20) + 1}-{Math.min(page * 20, data?.total || 0)} of {data?.total}
          </p>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>
              Previous
            </Button>
            <Button variant="outline" size="sm" disabled={page >= totalPages} onClick={() => setPage(page + 1)}>
              Next
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
