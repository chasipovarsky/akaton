import { TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { Api, ChatState } from '../api.service';
import { EmployeeChat } from './employee-chat';

// תור של הבוט שרץ: שלב שמוצג מיד, שלב מהשרת, ורמז אחרי 30 שניות — בלי לשנות את /state
const busyState = (stage: ChatState['stage']): ChatState => ({
  history: [], comparison: null, open_questions: 0, answers_ready: 0,
  busy: true, error_message: null, pending_message: 'אני רוצה לעבור לרכב חשמלי', stage,
});

describe('EmployeeChat — תור ארוך של הבוט', () => {
  let state: ChatState;
  let api: Record<string, ReturnType<typeof vi.fn>>;

  beforeEach(() => {
    vi.useFakeTimers();
    // jsdom בלי matchMedia (הגלילה בודקת prefers-reduced-motion)
    window.matchMedia ??= ((q: string) => ({ matches: false, media: q })) as unknown as typeof window.matchMedia;
    Element.prototype.scrollTo ??= () => {};
    state = { history: [], comparison: null, open_questions: 0, answers_ready: 0, busy: false };
    api = {
      state: vi.fn(() => of(state)),
      chat: vi.fn(() => of({ status: 'pending' })),
      demoEmployees: vi.fn(() => of([])),
      cars: vi.fn(() => of({ status: 'missing_profile', cars: [] })),
    };
    TestBed.configureTestingModule({
      imports: [EmployeeChat],
      providers: [{ provide: Api, useValue: api }],
    });
  });

  afterEach(() => vi.useRealTimers());

  function sendMessage() {
    const fixture = TestBed.createComponent(EmployeeChat);
    fixture.detectChanges();
    const chat = fixture.componentInstance;
    state = busyState('checking');
    chat.draft.set('אני רוצה לעבור לרכב חשמלי');
    chat.send();
    fixture.detectChanges();
    return { fixture, chat, text: () => (fixture.nativeElement as HTMLElement).textContent ?? '' };
  }

  it('מציג את השלב מיד, בלי לחכות לבדיקה הראשונה', () => {
    const { text } = sendMessage();
    expect(text()).toContain('בודקת את הנתונים');
    expect(text()).not.toContain('הבוט מקליד');
  });

  it('מעדכן את השלב מהשרת', () => {
    const { fixture, text } = sendMessage();
    state = busyState('writing');
    vi.advanceTimersByTime(1000);
    fixture.detectChanges();
    expect(text()).toContain('מנסחת תשובה');
  });

  it('אחרי 30 שניות מציע את ההשוואה והדירוג, והבוט ממשיך לעבוד', () => {
    const { fixture, chat, text } = sendMessage();
    vi.advanceTimersByTime(29000);
    fixture.detectChanges();
    expect(text()).not.toContain('זה לוקח יותר מהרגיל');

    vi.advanceTimersByTime(2000);
    fixture.detectChanges();
    expect(text()).toContain('זה לוקח יותר מהרגיל, אפשר גם להשתמש בהשוואה ובדירוג');
    expect(chat.busy()).toBe(true); // עדיין מחכים לתשובה — זה רק רמז

    // התשובה הגיעה — הרמז נעלם
    state = { history: [{ role: 'bot', text: 'מה קבוצת הזכאות שלך?' }], comparison: null,
      open_questions: 0, answers_ready: 0, busy: false, stage: null };
    vi.advanceTimersByTime(1000);
    fixture.detectChanges();
    expect(chat.busy()).toBe(false);
    expect(text()).not.toContain('זה לוקח יותר מהרגיל');
    expect(text()).toContain('מה קבוצת הזכאות שלך?');
  });
});
