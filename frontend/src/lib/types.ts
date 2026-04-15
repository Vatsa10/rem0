export interface Subscriber {
  id: string;
  name: string;
  phone: string;
  email: string;
  subscription_id: string;
  subscription_type: string;
  renewal_date: string;
  amount: string;
  language: string;
  metadata: Record<string, unknown>;
  status: SubscriptionStatus;
  created_at: string;
  updated_at: string;
}

export type SubscriptionStatus =
  | "NEW"
  | "CONTACTED"
  | "RENEWED"
  | "FOLLOW_UP_NEEDED"
  | "CALLBACK_SCHEDULED"
  | "NOT_INTERESTED"
  | "NO_DECISION"
  | "INVALID_CONTACT"
  | "EXPIRED"
  | "DO_NOT_CALL";

export interface CallRecord {
  id: number;
  call_id: string;
  call_sid: string;
  subscriber_id: string;
  subscriber_name: string;
  status: string;
  transcript: string;
  summary: string;
  response: string;
  justification: string;
  next_steps: string;
  duration: number;
  created_at: string;
}

export interface Settings {
  company_name: string;
  agent_name: string;
  default_language: string;
  days_before_renewal: number;
  days_between_calls: number;
}

export interface DashboardStats {
  total_subscribers: number;
  active_subscribers: number;
  calls_today: number;
  calls_this_week: number;
  renewal_rate: number;
  status_breakdown: Record<string, number>;
  recent_calls: CallRecord[];
  upcoming_renewals: Subscriber[];
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  limit: number;
}

export const LANGUAGES: Record<string, string> = {
  "hi-IN": "Hindi",
  "gu-IN": "Gujarati",
  "en-IN": "English (Indian)",
  "ta-IN": "Tamil",
  "te-IN": "Telugu",
  "bn-IN": "Bengali",
  "mr-IN": "Marathi",
  "kn-IN": "Kannada",
  "ml-IN": "Malayalam",
  "pa-IN": "Punjabi",
  "od-IN": "Odia",
};

export const STATUS_COLORS: Record<string, string> = {
  NEW: "bg-blue-100 text-blue-800",
  CONTACTED: "bg-yellow-100 text-yellow-800",
  RENEWED: "bg-green-100 text-green-800",
  FOLLOW_UP_NEEDED: "bg-orange-100 text-orange-800",
  CALLBACK_SCHEDULED: "bg-purple-100 text-purple-800",
  NOT_INTERESTED: "bg-red-100 text-red-800",
  NO_DECISION: "bg-gray-100 text-gray-800",
  INVALID_CONTACT: "bg-red-200 text-red-900",
  EXPIRED: "bg-gray-200 text-gray-600",
  DO_NOT_CALL: "bg-red-300 text-red-900",
};
