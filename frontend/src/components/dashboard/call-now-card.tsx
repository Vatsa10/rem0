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
import { LANGUAGES } from "@/lib/types";

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
      const subscriber = {
        id: `adhoc-${Date.now()}`,
        name: form.name,
        phone: form.phone,
        email: "",
        subscription_id: form.subscription_id,
        subscription_type: form.subscription_type || "Subscription",
        renewal_date: form.renewal_date || new Date().toISOString().slice(0, 10),
        amount: form.amount,
        language: form.language,
        metadata: {},
      };
      const result = (await api.initiateCalls({ subscribers: [subscriber] })) as {
        message: string;
        results: { call_id?: string; error?: string }[];
      };
      const first = result.results[0];
      if (first?.error) {
        toast.error(`Call failed: ${first.error}`);
      } else {
        toast.success(`Call initiated (ID: ${first?.call_id})`);
        setForm(EMPTY);
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to initiate call");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-lg">Quick Call</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>Name</Label>
            <Input
              placeholder="Raj Patel"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </div>
          <div className="space-y-1.5">
            <Label>Phone</Label>
            <Input
              placeholder="+919876543210"
              value={form.phone}
              onChange={(e) => setForm({ ...form, phone: e.target.value })}
            />
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
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

        <div className="grid gap-3 sm:grid-cols-3">
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
              placeholder="649/month"
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

        <Button onClick={handleCall} disabled={!canCall || loading} className="w-full sm:w-auto">
          {loading ? "Calling..." : "Call Now"}
        </Button>
      </CardContent>
    </Card>
  );
}
