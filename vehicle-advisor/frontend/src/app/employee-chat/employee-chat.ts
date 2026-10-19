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

  sessionId = signal(this.loadSid());
  messages = signal<ChatMsg[]>([]);
  comparison = signal<Comparison | null>(null);
  draft = signal('');
  busy = signal(false);
  endRef = viewChild<ElementRef>('end');

  ngOnInit() {
    this.api.state(this.sessionId()).subscribe({
      next: (s) => {
        this.messages.set(s.history);
        this.comparison.set(s.comparison);
        this.scroll();
      },
      error: () => {},
    });
  }

  send() {
    const text = this.draft().trim();
    if (!text || this.busy()) return;
    this.messages.update((m) => [...m, { role: 'user', text }]);
    this.draft.set('');
    this.busy.set(true);
    this.scroll();

    this.api.chat(this.sessionId(), text).subscribe({
      next: (r) => {
        this.messages.update((m) => [...m, { role: 'bot', text: r.reply }]);
        if (r.comparison) this.comparison.set(r.comparison);
        if (r.error) console.warn(r.error);
        this.busy.set(false);
        this.scroll();
      },
      error: () => {
        this.messages.update((m) => [
          ...m,
          { role: 'bot', text: 'לא הצלחתי להתחבר לשרת. בדקי שהשרת רץ ונסי שוב.' },
        ]);
        this.busy.set(false);
      },
    });
  }

  newChat() {
    const sid = crypto.randomUUID();
    localStorage.setItem('sid', sid);
    this.sessionId.set(sid);
    this.messages.set([]);
    this.comparison.set(null);
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
    if (!sid) {
      sid = crypto.randomUUID();
      localStorage.setItem('sid', sid);
    }
    return sid;
  }

  private scroll() {
    setTimeout(() => this.endRef()?.nativeElement.scrollIntoView({ behavior: 'smooth' }));
  }
}
