import {
  Component, ElementRef, OnDestroy, OnInit, computed, inject, signal, viewChild,
} from '@angular/core';
import { DecimalPipe } from '@angular/common';
import {
  Api, ChatMsg, ChatState, Comparison, DemoEmployee, PendingComparison, QuestionStatus, RankResult,
} from '../api.service';

const POLL_MS = 5000;            // בדיקת תשובת נציג
const TURN_POLL_MS = 2000;       // בדיקת תור של הבוט שרץ ברקע
const TURN_MAX_WAIT_MS = 180000; // 3 דקות — אחרי זה "זה לוקח יותר מהרגיל"
const SLOW_MSG = 'זה לוקח יותר מהרגיל. אפשר לנסות שוב.';

@Component({
  selector: 'app-employee-chat',
  imports: [DecimalPipe],
  templateUrl: './employee-chat.html',
  styleUrl: './employee-chat.scss',
})
export class EmployeeChat implements OnInit, OnDestroy {
  private api = inject(Api);
  // בדיקה כל 5 שניות אם הנציגה ענתה — רק כשיש שאלה פתוחה
  private poll?: ReturnType<typeof setInterval>;
  // תור של הבוט ברקע: בדיקה כל 2 שניות כל עוד busy
  private turnPoll?: ReturnType<typeof setInterval>;
  private turnStarted = 0;
  private turnSent = '';

  readonly suggestions = [
    'אני רוצה לעבור לרכב חשמלי',
    'מה יקרה אם אוותר על הרכב?',
    'אפשר לקבל קרניבל?',
  ];

  sessionId = signal(this.loadSid());
  messages = signal<ChatMsg[]>([]);
  comparison = signal<Comparison | PendingComparison | null>(null);
  // השוואה מלאה, או "בינתיים: מצב היום" כשמחכים לנציג
  full = computed(() => {
    const c = this.comparison();
    return c && c.status !== 'pending' ? (c as Comparison) : null;
  });
  pending = computed(() => {
    const c = this.comparison();
    return c?.status === 'pending' ? (c as PendingComparison) : null;
  });
  draft = signal('');
  busy = signal(false);
  loading = signal(true);
  // הודעה שנכשלה בשליחה — מאפשרת "לנסות שוב"
  failed = signal<{ text: string; sent: string } | null>(null);
  demos = signal<DemoEmployee[]>([]);
  // "מה הכי משתלם לי?" — null = סגור
  ranking = signal<RankResult | 'loading' | 'error' | null>(null);
  listRef = viewChild<ElementRef<HTMLElement>>('list');
  inputRef = viewChild<ElementRef<HTMLInputElement>>('input');

  ngOnInit() {
    this.api.state(this.sessionId()).subscribe({
      next: (s) => {
        this.loading.set(false);
        if (s.busy) {
          // רענון באמצע תור: ההיסטוריה כבר כוללת את ההודעה שבטיפול — ממשיכים לחכות
          this.messages.set(s.history);
          this.comparison.set(s.comparison);
          this.scroll();
          this.startTurnPolling(s.pending_message ?? '');
        } else {
          this.applyState(s);
        }
      },
      error: () => this.loading.set(false),
    });
    this.api.demoEmployees().subscribe({ next: (d) => this.demos.set(d), error: () => {} });
  }

  ngOnDestroy() {
    this.stopTurnPolling();
    this.stopPolling();
  }

  // ── תור של הבוט ברקע ───────────────────────────────────────────────────────
  private startTurnPolling(sent: string) {
    this.stopPolling(); // בזמן תור לא בודקים תשובת נציג — התור עצמו יחזיר את המצב
    this.stopTurnPolling();
    this.busy.set(true);
    this.turnSent = sent;
    this.turnStarted = Date.now();
    const sid = this.sessionId();
    this.turnPoll = setInterval(() => this.checkTurn(sid), TURN_POLL_MS);
  }

  private stopTurnPolling() {
    clearInterval(this.turnPoll);
    this.turnPoll = undefined;
  }

  private checkTurn(sid: string) {
    if (sid !== this.sessionId()) return this.stopTurnPolling(); // עברנו לשיחה חדשה
    if (Date.now() - this.turnStarted > TURN_MAX_WAIT_MS) {
      this.stopTurnPolling();
      this.busy.set(false);
      this.failed.set({ text: SLOW_MSG, sent: this.turnSent });
      this.scroll();
      return;
    }
    this.api.state(sid).subscribe({
      next: (s) => {
        if (s.busy || sid !== this.sessionId() || !this.turnPoll) return;
        this.stopTurnPolling();
        this.applyState(s);
      },
      error: () => {}, // תקלה רגעית — הבדיקה הבאה תנסה שוב
    });
  }

  /** מצב מהשרת אחרי שהתור הסתיים (או כשאין תור): הודעות, השוואה, שגיאה, ובדיקת נציג. */
  private applyState(s: ChatState) {
    this.messages.set(s.history);
    this.comparison.set(s.comparison);
    this.busy.set(false);
    this.failed.set(
      s.error_message ? { text: s.error_message, sent: s.pending_message || this.turnSent } : null,
    );
    this.scroll();
    this.syncPolling(s);
  }

  // ── תשובת נציג: בדיקה אוטומטית והמשך שיחה ────────────────────────────────
  private syncPolling(s: QuestionStatus) {
    if (s.answers_ready) {
      this.resume();
    } else if (s.open_questions) {
      this.poll ??= setInterval(() => this.checkRep(), POLL_MS);
    } else {
      this.stopPolling();
    }
  }

  private stopPolling() {
    clearInterval(this.poll);
    this.poll = undefined;
  }

  private checkRep() {
    if (this.busy()) return; // העובדת באמצע שליחה — ננסה בסבב הבא
    this.api.state(this.sessionId()).subscribe({
      next: (s) => this.syncPolling(s),
      error: () => {}, // תקלה רגעית — הבדיקה הבאה תנסה שוב
    });
  }

  private resume() {
    if (this.busy()) return;
    this.stopPolling();
    this.busy.set(true);
    const sid = this.sessionId();
    this.api.resume(sid).subscribe({
      next: (r) => {
        if (r.status === 'pending') {
          // התור רץ ברקע; ההיסטוריה שתגיע תכלול את "תשובת הנציג" ואת תשובת הבוט
          this.startTurnPolling('');
        } else {
          this.busy.set(false);
          this.syncPolling(r);
        }
      },
      error: (e) => {
        if (e?.status === 409) return this.startTurnPolling(''); // תור כבר רץ — מחכים לו
        const msg = e?.error?.reply ?? 'הנציגה ענתה, אבל לא הצלחתי להמשיך את השיחה. אפשר לכתוב לי הודעה.';
        this.failed.set({ text: msg, sent: '' });
        this.busy.set(false);
      },
    });
  }

  loadDemo(id: string) {
    if (this.busy()) return;
    this.busy.set(true);
    this.failed.set(null);
    this.api.loadDemo(id, this.sessionId()).subscribe({
      next: (s) => {
        // פרופיל חדש — ההשוואה והדירוג הקודמים כבר לא רלוונטיים
        this.messages.set(s.history);
        this.comparison.set(null);
        this.ranking.set(null);
        this.busy.set(false);
        this.scroll();
      },
      error: (e) => {
        const msg = e?.status === 409 ? e.error.reply : 'לא הצלחתי לטעון את העובדת לדוגמה. אפשר לנסות שוב.';
        this.failed.set({ text: msg, sent: '' });
        this.busy.set(false);
      },
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

    // השרת חוזר מיד עם pending; התשובה מגיעה דרך /state (בדיקה כל 2 שניות)
    this.api.chat(this.sessionId(), text).subscribe({
      next: () => this.startTurnPolling(text),
      error: (e) => {
        // מגבלת קצב / הודעה ארוכה / תור שכבר רץ (409) — השרת מחזיר reply ידידותי
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

  rank() {
    this.ranking.set('loading');
    this.api.rank(this.sessionId()).subscribe({
      next: (r) => this.ranking.set(r),
      error: () => this.ranking.set('error'),
    });
  }

  rankResult(): RankResult | null {
    const r = this.ranking();
    return typeof r === 'object' ? r : null;
  }

  // עיגול ל-100 הקרוב (לפחות 100, כדי לא להציג "כ-0 ₪")
  round100(n: number): number {
    return Math.max(100, Math.round(n / 100) * 100);
  }

  suggest(text: string) {
    this.draft.set(text);
    this.focusInput();
  }

  newChat() {
    this.stopTurnPolling();
    this.stopPolling();
    this.busy.set(false);
    const sid = crypto.randomUUID();
    localStorage.setItem('sid', sid);
    this.sessionId.set(sid);
    this.messages.set([]);
    this.comparison.set(null);
    this.failed.set(null);
    this.ranking.set(null);
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
