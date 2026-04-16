"use client";

import { useEffect, useState } from "react";
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
import { Settings, LANGUAGES } from "@/lib/types";

export default function SettingsPage() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.getSettings().then((data) => setSettings(data as Settings));
  }, []);

  const handleSave = async () => {
    if (!settings) return;
    setSaving(true);
    try {
      const updated = await api.updateSettings(settings as unknown as Record<string, unknown>) as Settings;
      setSettings(updated);
      toast.success("Settings saved successfully");
    } catch {
      toast.error("Failed to save settings");
    }
    setSaving(false);
  };

  if (!settings) return <div className="text-gray-500">Loading settings...</div>;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="font-display text-3xl font-bold tracking-tight text-slate-900">Settings</h1>
        <p className="mt-1 text-sm text-slate-500">Configure your voice agent and call behavior</p>
      </div>

      <Card className="max-w-2xl">
        <CardHeader>
          <CardTitle>General Configuration</CardTitle>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="company_name">Company Name</Label>
              <Input
                id="company_name"
                value={settings.company_name}
                onChange={(e) => setSettings({ ...settings, company_name: e.target.value })}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="agent_name">Agent Name</Label>
              <Input
                id="agent_name"
                value={settings.agent_name}
                onChange={(e) => setSettings({ ...settings, agent_name: e.target.value })}
              />
            </div>
          </div>

          <div className="space-y-2">
            <Label htmlFor="language">Default Language</Label>
            <Select
              value={settings.default_language}
              onValueChange={(val) => { if (val) setSettings({ ...settings, default_language: val }); }}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {Object.entries(LANGUAGES).map(([code, name]) => (
                  <SelectItem key={code} value={code}>
                    {name} ({code})
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="days_before">Days Before Renewal</Label>
              <Input
                id="days_before"
                type="number"
                min={1}
                value={settings.days_before_renewal}
                onChange={(e) =>
                  setSettings({ ...settings, days_before_renewal: parseInt(e.target.value) || 0 })
                }
              />
              <p className="text-xs text-gray-500">Call subscribers this many days before renewal</p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="days_between">Days Between Calls</Label>
              <Input
                id="days_between"
                type="number"
                min={1}
                value={settings.days_between_calls}
                onChange={(e) =>
                  setSettings({ ...settings, days_between_calls: parseInt(e.target.value) || 0 })
                }
              />
              <p className="text-xs text-gray-500">Minimum gap between calls to same subscriber</p>
            </div>
          </div>

          <Button onClick={handleSave} disabled={saving}>
            {saving ? "Saving..." : "Save Settings"}
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
