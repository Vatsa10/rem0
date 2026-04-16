"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { DashboardStats, STATUS_COLORS } from "@/lib/types";
import { CallNowCard } from "@/components/dashboard/call-now-card";
import { useCallSubscriber } from "@/hooks/use-call-subscriber";

export default function DashboardPage() {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [loading, setLoading] = useState(true);
  const { callSubscriber, callingId } = useCallSubscriber();

  useEffect(() => {
    api.getDashboard().then((data) => {
      setStats(data as DashboardStats);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, []);

  if (loading) return <div className="text-gray-500">Loading dashboard...</div>;
  if (!stats) return <div className="text-red-500">Failed to load dashboard</div>;

  const cards = [
    { title: "Total Subscribers", value: stats.total_subscribers, sub: `${stats.active_subscribers} active` },
    { title: "Calls Today", value: stats.calls_today, sub: `${stats.calls_this_week} this week` },
    { title: "Renewal Rate", value: `${stats.renewal_rate}%`, sub: "confirmed renewals" },
    { title: "Status Breakdown", value: Object.keys(stats.status_breakdown).length, sub: "unique statuses" },
  ];

  return (
    <div className="space-y-8">
      <h1 className="text-2xl font-bold text-gray-900">Dashboard</h1>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {cards.map((card) => (
          <Card key={card.title}>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-gray-500">{card.title}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{card.value}</div>
              <p className="text-xs text-gray-500">{card.sub}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      <CallNowCard />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Recent Calls</CardTitle>
          </CardHeader>
          <CardContent>
            {stats.recent_calls.length === 0 ? (
              <p className="text-sm text-gray-500">No calls yet</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Subscriber</TableHead>
                    <TableHead>Response</TableHead>
                    <TableHead>Date</TableHead>
                    <TableHead className="w-24"></TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {stats.recent_calls.map((call) => (
                    <TableRow key={call.call_id}>
                      <TableCell className="font-medium">{call.subscriber_name}</TableCell>
                      <TableCell>
                        <Badge variant="outline" className="text-xs">
                          {call.response || call.status}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-sm text-gray-500">
                        {new Date(call.created_at).toLocaleDateString()}
                      </TableCell>
                      <TableCell>
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={callingId === call.subscriber_id}
                          onClick={() => callSubscriber(call.subscriber_id, call.subscriber_name)}
                        >
                          {callingId === call.subscriber_id ? "..." : "Call"}
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Upcoming Renewals</CardTitle>
          </CardHeader>
          <CardContent>
            {stats.upcoming_renewals.length === 0 ? (
              <p className="text-sm text-gray-500">No upcoming renewals</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Name</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead>Renewal Date</TableHead>
                    <TableHead>Status</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {stats.upcoming_renewals.map((sub) => (
                    <TableRow key={sub.id}>
                      <TableCell className="font-medium">{sub.name}</TableCell>
                      <TableCell>{sub.subscription_type}</TableCell>
                      <TableCell>{sub.renewal_date}</TableCell>
                      <TableCell>
                        <Badge className={STATUS_COLORS[sub.status] || "bg-gray-100"}>
                          {sub.status}
                        </Badge>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
