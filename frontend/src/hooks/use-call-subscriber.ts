"use client";

import { useState } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";

/**
 * Hook for initiating a call to an existing subscriber by ID.
 * Backend looks up the subscriber in the DB by the provided id.
 */
export function useCallSubscriber() {
  const [callingId, setCallingId] = useState<string | null>(null);

  const callSubscriber = async (
    subscriberId: string,
    subscriberName?: string
  ): Promise<boolean> => {
    setCallingId(subscriberId);
    try {
      const result = (await api.initiateCalls({
        subscription_ids: [subscriberId],
      })) as { results: { call_id?: string; error?: string }[] };

      const first = result.results?.[0];
      if (!first) {
        toast.error("No call initiated (subscriber may not exist in DB)");
        return false;
      }
      if (first.error) {
        toast.error(`Call failed: ${first.error}`);
        return false;
      }
      toast.success(
        `Calling ${subscriberName || "subscriber"} (ID: ${first.call_id})`
      );
      return true;
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to initiate call");
      return false;
    } finally {
      setCallingId(null);
    }
  };

  return { callSubscriber, callingId };
}
