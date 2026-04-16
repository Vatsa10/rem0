"use client";

import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { LANGUAGES, Subscriber } from "@/lib/types";

const EMPTY = {
  name: "",
  phone: "",
  subscription_id: "",
  subscription_type: "",
  renewal_date: "",
  amount: "",
  language: "hi-IN",
};

export function CallNowCard() {
  const [form, setForm] = useState(EMPTY);
  const [loading, setLoading] = useState(false);

  const canCall =
    form.name.trim() && form.phone.trim() && form.subscription_id.trim();

  const handleCall = async () => {
    if (!canCall) return;
    setLoading(true);
    try {
      // Step 1: Persist the subscriber so the call shows up in history + can be recalled.
      const payload = {
        name: form.name,
        phone: form.phone,
        email: "",
        subscription_id: form.subscription_id,
        subscription_type: form.subscription_type || "Subscription",
        renewal_date: form.renewal_date || new Date().toISOString().slice(0, 10),
        amount: form.amount,
        language: form.language,
        metadata: { source: "quick_call" },
      };

      let subscriber: Subscriber;
      try {
        subscriber = (await api.createSubscriber(payload)) as Subscriber;
      } catch (err) {
        // If subscription_id already exists, look it up and reuse it.
        const message = err instanceof Error ? err.message : String(err);
        if (message.toLowerCase().includes("unique") || message.includes("409")) {
          const list = (await api.getSubscribers({ search: form.subscription_id })) as {
            items: Subscriber[];
          };
          const found = list.items.find(
            (s) => s.subscription_id === form.subscription_id
          );
          if (!found) throw err;
          subscriber = found;
          toast.info(`Reusing existing subscriber: ${found.name}`);
        } else {
          throw err;
        }
      }

      // Step 2: Initiate the call via DB-backed subscriber_id so it's tracked.
      const result = (await api.initiateCalls({
        subscription_ids: [subscriber.id],
      })) as {
        results: { call_id?: string; error?: string }[];
      };

      const first = result.results?.[0];
      if (first?.error) {
        toast.error(`Call failed: ${first.error}`);
      } else {
        toast.success(
          `Calling ${subscriber.name} — call saved as ${first?.call_id}`
        );
        setForm(EMPTY);
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to initiate call");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Card className="border-blue-100 bg-gradient-to-br from-white via-white to-blue-50/40">
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle className="flex items-center gap-2">
            <span className="flex size-7 items-center justify-center rounded-lg bg-blue-600 text-white shadow-sm shadow-blue-600/30">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="size-4">
                <path d="M15.05 5A5 5 0 0119 8.95M15.05 1A9 9 0 0123 8.94m-1 7.98v3a2 2 0 01-2.18 2 19.79 19.79 0 01-8.63-3.07 19.5 19.5 0 01-6-6 19.79 19.79 0 01-3.07-8.67A2 2 0 014.11 2h3a2 2 0 012 1.72 12.84 12.84 0 00.7 2.81 2 2 0 01-.45 2.11L8.09 9.91a16 16 0 006 6l1.27-1.27a2 2 0 012.11-.45 12.84 12.84 0 002.81.7A2 2 0 0122 16.92z" />
              </svg>
            </span>
            Quick Call
          </CardTitle>
          <p className="mt-1 text-sm text-slate-500">
            Saves the subscriber and places the call — callable again from history.
          </p>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>Name</Label>
            <Input
              placeholder="Raj Patel"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </div>
          <div className="space-y-1.5">
            <Label>Phone Number</Label>
            <Input
              placeholder="+919876543210"
              value={form.phone}
              onChange={(e) => setForm({ ...form, phone: e.target.value })}
            />
          </div>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>Subscription ID</Label>
            <Input
              placeholder="SUB-12345"
              value={form.subscription_id}
              onChange={(e) =>
                setForm({ ...form, subscription_id: e.target.value })
              }
            />
          </div>
          <div className="space-y-1.5">
            <Label>Subscription Type</Label>
            <Input
              placeholder="Netflix, Gym..."
              value={form.subscription_type}
              onChange={(e) =>
                setForm({ ...form, subscription_type: e.target.value })
              }
            />
          </div>
        </div>

        <div className="grid gap-4 sm:grid-cols-3">
          <div className="space-y-1.5">
            <Label>Renewal Date</Label>
            <Input
              type="date"
              value={form.renewal_date}
              onChange={(e) =>
                setForm({ ...form, renewal_date: e.target.value })
              }
            />
          </div>
          <div className="space-y-1.5">
            <Label>Amount</Label>
            <Input
              placeholder="₹649/month"
              value={form.amount}
              onChange={(e) => setForm({ ...form, amount: e.target.value })}
            />
          </div>
          <div className="space-y-1.5">
            <Label>Language</Label>
            <Select
              value={form.language}
              onValueChange={(v) => {
                if (v) setForm({ ...form, language: v });
              }}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {Object.entries(LANGUAGES).map(([code, name]) => (
                  <SelectItem key={code} value={code}>
                    {name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        <div className="flex items-center justify-between pt-2">
          <p className="text-xs text-slate-500">
            {canCall ? "Ready to call" : "Fill name, phone, and subscription ID"}
          </p>
          <Button onClick={handleCall} disabled={!canCall || loading} size="lg">
            {loading ? (
              <>
                <svg className="size-4 animate-spin" viewBox="0 0 24 24" fill="none">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
                Calling...
              </>
            ) : (
              <>
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="size-4">
                  <path d="M22 16.92v3a2 2 0 01-2.18 2 19.79 19.79 0 01-8.63-3.07 19.5 19.5 0 01-6-6 19.79 19.79 0 01-3.07-8.67A2 2 0 014.11 2h3a2 2 0 012 1.72 12.84 12.84 0 00.7 2.81 2 2 0 01-.45 2.11L8.09 9.91a16 16 0 006 6l1.27-1.27a2 2 0 012.11-.45 12.84 12.84 0 002.81.7A2 2 0 0122 16.92z" />
                </svg>
                Call Now
              </>
            )}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
