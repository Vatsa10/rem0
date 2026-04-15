const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${url}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || "Request failed");
  }
  return res.json();
}

function qs(params?: Record<string, unknown>): string {
  if (!params) return "";
  const p = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  });
  const s = p.toString();
  return s ? `?${s}` : "";
}

export const api = {
  // Subscribers
  getSubscribers: (params?: Record<string, unknown>) =>
    request(`/api/subscribers${qs(params)}`),
  getSubscriber: (id: string) => request(`/api/subscribers/${id}`),
  createSubscriber: (data: Record<string, unknown>) =>
    request("/api/subscribers", { method: "POST", body: JSON.stringify(data) }),
  updateSubscriber: (id: string, data: Record<string, unknown>) =>
    request(`/api/subscribers/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteSubscriber: (id: string) =>
    request(`/api/subscribers/${id}`, { method: "DELETE" }),

  // Calls
  getCalls: (params?: Record<string, unknown>) =>
    request(`/api/calls${qs(params)}`),
  getCall: (id: string) => request(`/api/calls/${id}`),

  // Settings
  getSettings: () => request("/api/settings"),
  updateSettings: (data: Record<string, unknown>) =>
    request("/api/settings", { method: "PUT", body: JSON.stringify(data) }),

  // Dashboard
  getDashboard: () => request("/api/dashboard"),

  // Voice pipeline
  initiateCalls: (data: Record<string, unknown>) =>
    request("/calls/initiate", { method: "POST", body: JSON.stringify(data) }),
};
