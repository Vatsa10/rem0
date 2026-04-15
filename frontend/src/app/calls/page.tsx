"use client";

import { useCallback, useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
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
import { api } from "@/lib/api";
import { CallRecord, PaginatedResponse } from "@/lib/types";

const RESPONSE_COLORS: Record<string, string> = {
  "Confirmed Renewal": "bg-green-100 text-green-800",
  Interested: "bg-blue-100 text-blue-800",
  Reschedule: "bg-purple-100 text-purple-800",
  "Not Interested": "bg-red-100 text-red-800",
  "No Decision": "bg-gray-100 text-gray-800",
  "Invalid Contact": "bg-red-200 text-red-900",
};

export default function CallsPage() {
  const [data, setData] = useState<PaginatedResponse<CallRecord> | null>(null);
  const [statusFilter, setStatusFilter] = useState("");
  const [page, setPage] = useState(1);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const load = useCallback(() => {
    const params: Record<string, unknown> = { page, limit: 20 };
    if (statusFilter) params.status = statusFilter;
    api.getCalls(params).then((d) => setData(d as PaginatedResponse<CallRecord>));
  }, [page, statusFilter]);

  useEffect(() => { load(); }, [load]);

  const totalPages = data ? Math.ceil(data.total / data.limit) : 0;

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-gray-900">Call History</h1>

      <div className="flex gap-4">
        <Select value={statusFilter} onValueChange={(v) => { setStatusFilter(!v || v === "ALL" ? "" : v); setPage(1); }}>
          <SelectTrigger className="w-48">
            <SelectValue placeholder="Filter by status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="ALL">All Statuses</SelectItem>
            <SelectItem value="initiated">Initiated</SelectItem>
            <SelectItem value="completed">Completed</SelectItem>
            <SelectItem value="failed">Failed</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <div className="rounded-lg border bg-white">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-8"></TableHead>
              <TableHead>Date</TableHead>
              <TableHead>Subscriber</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Response</TableHead>
              <TableHead>Summary</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data?.items.map((call) => (
              <>
                <TableRow
                  key={call.call_id}
                  className="cursor-pointer hover:bg-gray-50"
                  onClick={() => setExpandedId(expandedId === call.call_id ? null : call.call_id)}
                >
                  <TableCell className="text-gray-400">
                    {expandedId === call.call_id ? "▼" : "▶"}
                  </TableCell>
                  <TableCell className="text-sm">
                    {new Date(call.created_at).toLocaleString()}
                  </TableCell>
                  <TableCell className="font-medium">{call.subscriber_name}</TableCell>
                  <TableCell>
                    <Badge
                      variant="outline"
                      className={
                        call.status === "completed"
                          ? "border-green-300 text-green-700"
                          : call.status === "failed"
                            ? "border-red-300 text-red-700"
                            : "border-yellow-300 text-yellow-700"
                      }
                    >
                      {call.status}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    {call.response && (
                      <Badge className={RESPONSE_COLORS[call.response] || "bg-gray-100"}>
                        {call.response}
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell className="max-w-xs truncate text-sm text-gray-500">
                    {call.summary || "-"}
                  </TableCell>
                </TableRow>
                {expandedId === call.call_id && (
                  <TableRow key={`${call.call_id}-detail`}>
                    <TableCell colSpan={6} className="bg-gray-50 p-0">
                      <Card className="m-4 border-0 shadow-none">
                        <CardContent className="space-y-4 pt-4">
                          {call.summary && (
                            <div>
                              <h4 className="text-sm font-semibold text-gray-700">Summary</h4>
                              <p className="text-sm text-gray-600">{call.summary}</p>
                            </div>
                          )}
                          {call.justification && (
                            <div>
                              <h4 className="text-sm font-semibold text-gray-700">Justification</h4>
                              <p className="text-sm text-gray-600">{call.justification}</p>
                            </div>
                          )}
                          {call.next_steps && (
                            <div>
                              <h4 className="text-sm font-semibold text-gray-700">Next Steps</h4>
                              <p className="text-sm text-gray-600">{call.next_steps}</p>
                            </div>
                          )}
                          {call.transcript && (
                            <div>
                              <h4 className="text-sm font-semibold text-gray-700">Transcript</h4>
                              <div className="mt-2 max-h-64 overflow-y-auto rounded bg-white p-3 text-sm">
                                {call.transcript.split("\n").map((line, i) => (
                                  <p
                                    key={i}
                                    className={
                                      line.startsWith("Agent:")
                                        ? "mb-1 text-blue-700"
                                        : line.startsWith("Customer:")
                                          ? "mb-1 text-gray-700"
                                          : "mb-1 text-gray-500"
                                    }
                                  >
                                    {line}
                                  </p>
                                ))}
                              </div>
                            </div>
                          )}
                          {!call.transcript && !call.summary && (
                            <p className="text-sm text-gray-400">No call details available yet</p>
                          )}
                        </CardContent>
                      </Card>
                    </TableCell>
                  </TableRow>
                )}
              </>
            ))}
            {data?.items.length === 0 && (
              <TableRow>
                <TableCell colSpan={6} className="py-8 text-center text-gray-500">
                  No calls recorded yet
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
