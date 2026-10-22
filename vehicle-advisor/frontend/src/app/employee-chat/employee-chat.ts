import { Component, ElementRef, OnInit, inject, signal, viewChild } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { Api, ChatMsg, Comparison } from '../api.service';

@Component({
  selector: 'app-employee-chat',
  imports: [DecimalPipe],
  templateUrl: './employee-chat.html',
  styleUrl: './employee-chat.scss',
})
export class EmployeeChat implements OnInit {
  private api = inject(Api);

  readonly suggestions = [
    'אני רוצה לעבור לרכב חשמלי',
    'מה יקרה אם אוותר על הרכב?',
    'אפשר לקבל קרניבל?',
  ];

  sessionId = signal(this.loadSid());
  messages = signal<ChatMsg[]>([]);
  comparison = signal<Comparison | null>(null);
  draft = signal('');
  busy = signal(false);
  loading = signal(true);
  // הודעה שנכשלה בשליחה — מאפשרת "לנסות שוב"
  failed = signal<{ text: string; sent: string } | null>(null);
  listRef = viewChild<ElementRef<HTMLElement>>('list');
  inputRef = viewChild<ElementRef<HTMLInputElement>>('input');

  ngOnInit() {
    this.api.state(this.sessionId()).subscribe({
      next: (s) => {
        this.messages.set(s.history);
        this.comparison.set(s.comparison);
        this.loading.set(false);
        this.scroll();
      },
      error: () => this.loading.set(false),
    });
  }

  send() {
    const text = this.draft().trim();
    if (!text || this.busy()) return;
    this.failed.set(null);
    this.messages.update((m) => [...m, { role: 'user', text }]);
    this.draft.set('');
    this.busy.set(true);
    this.scroll();

    this.api.chat(this.sessionId(), text).subscribe({
      next: (r) => {
        this.messages.update((m) => [...m, { role: 'bot', text: r.reply }]);
        if (r.comparison) this.comparison.set(r.comparison);
        this.busy.set(false);
        this.scroll();
      },
      error: (e) => {
        // מגבלת קצב / מכסה / הודעה ארוכה — השרת מחזיר reply ידידותי
        const msg = e?.error?.reply ?? 'לא הצלחתי להתחבר לשרת. בדקי שהשרת רץ ונסי שוב.';
        this.failed.set({ text: msg, sent: text });
        this.busy.set(false);
        this.scroll();
      },
    });
  }

  retry() {
    const f = this.failed();
    if (!f) return;
    // מסירים את ההודעה שנכשלה ומחזירים אותה לתיבת הקלט
    this.messages.update((m) => {
      const last = m[m.length - 1];
      return last?.role === 'user' && last.text === f.sent ? m.slice(0, -1) : m;
    });
    this.failed.set(null);
    this.draft.set(f.sent);
    this.focusInput();
  }

  suggest(text: string) {
    this.draft.set(text);
    this.focusInput();
  }

  newChat() {
    const sid = crypto.randomUUID();
    localStorage.setItem('sid', sid);
    this.sessionId.set(sid);
    this.messages.set([]);
    this.comparison.set(null);
    this.failed.set(null);
  }

  clean(t: string): string {
    return t.replace(/\*\*/g, '').replace(/^\s*\*\s+/gm, '• ');
  }

  signed(n: number): string {
    const r = Math.round(n);
    return (r > 0 ? '+' : '') + r.toLocaleString('en-US');
  }

  private loadSid(): string {
    let sid = localStorage.getItem('sid');
    // אותו פורמט שהשרת מאמת (randomUUID תמיד עומד בו)
    if (!sid || !/^[A-Za-z0-9-]{8,64}$/.test(sid)) {
      sid = crypto.randomUUID();
      localStorage.setItem('sid', sid);
    }
    return sid;
  }

  private focusInput() {
    setTimeout(() => this.inputRef()?.nativeElement.focus());
  }

  private scroll() {
    // גוללים רק את אזור ההודעות, לא את כל העמוד
    setTimeout(() => {
      const el = this.listRef()?.nativeElement;
      if (!el) return;
      const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      el.scrollTo({ top: el.scrollHeight, behavior: reduce ? 'auto' : 'smooth' });
    });
  }
}
