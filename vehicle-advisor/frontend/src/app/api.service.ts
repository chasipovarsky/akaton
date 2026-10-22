import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

const BASE = '/api';

export interface Scenario {
  net: number;
  tax: number;
  ni_health: number;
  taxable: number;
  electric_gross: number;
}
export interface ScenarioDetails {
  label: string;
  cost: number;
  participation: number;
  value_of_use: number;
  maintenance: number;
  electric_refund: number;
  family_share: number;
}

export interface Details {
  group: string;
  budget: number;
  today: ScenarioDetails;
  alt: ScenarioDetails;
}

export interface Comparison {
  status: string;
  reason?: string;
  today: Scenario;
  alt: Scenario;
  diff: number;
  explanation?: string;
  details?: Details;
}

export interface ChatMsg {
  role: 'user' | 'bot';
  text: string;
}

export interface ChatReply {
  reply: string;
  comparison?: Comparison | null;
  error?: string;
}

export interface ChatState {
  history: ChatMsg[];
  comparison: Comparison | null;
}

export interface RankOption {
  target: string;
  label: string;
  drive: string | null;
  participation: number;
  net: number;
  diff: number;
}

export interface RankResult {
  status: 'ok' | 'missing' | 'missing_profile';
  today_net?: number | null;
  today_label?: string;
  options?: RankOption[];
  missing?: string | string[];
  // העלאת ברוטו מינימלית שמשנה את האפשרות הראשונה; null = אין שינוי עד +100%
  turning_point?: { extra_gross: number; new_best: string; new_best_target: string } | null;
}

export interface DemoEmployee {
  id: string;
  title: string;
}

export interface AgentQuestion {
  id: number;
  session_id: string;
  question: string;
  answer: string | null;
  delivered: number;
  created: string;
}

@Injectable({ providedIn: 'root' })
export class Api {
  private http = inject(HttpClient);

  chat(sessionId: string, message: string): Observable<ChatReply> {
    return this.http.post<ChatReply>(`${BASE}/chat`, { session_id: sessionId, message });
  }

  state(sessionId: string): Observable<ChatState> {
    return this.http.get<ChatState>(`${BASE}/chat/${sessionId}/state`);
  }

  demoEmployees(): Observable<DemoEmployee[]> {
    return this.http.get<DemoEmployee[]>(`${BASE}/demo/employees`);
  }

  loadDemo(empId: string, sessionId: string): Observable<ChatState> {
    return this.http.post<ChatState>(`${BASE}/demo/${encodeURIComponent(empId)}`, {
      session_id: sessionId,
    });
  }

  rank(sessionId: string): Observable<RankResult> {
    return this.http.post<RankResult>(`${BASE}/rank`, { session_id: sessionId });
  }

  questions(openOnly = false): Observable<AgentQuestion[]> {
    return this.http.get<AgentQuestion[]>(`${BASE}/agent/questions`, {
      params: { open_only: openOnly },
    });
  }

  answer(questionId: number, answer: string): Observable<{ ok: boolean }> {
    return this.http.post<{ ok: boolean }>(`${BASE}/agent/answer`, {
      question_id: questionId,
      answer,
    });
  }
}