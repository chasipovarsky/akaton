import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { Api, AgentQuestion } from '../api.service';

@Component({
  selector: 'app-agent-inbox',
  imports: [],
  templateUrl: './agent-inbox.html',
  styleUrl: './agent-inbox.scss',
})
export class AgentInbox implements OnInit, OnDestroy {
  private api = inject(Api);
  private timer?: ReturnType<typeof setInterval>;

  questions = signal<AgentQuestion[]>([]);
  drafts = signal<Record<number, string>>({});

  open = computed(() => this.questions().filter((q) => q.answer === null));
  done = computed(() => this.questions().filter((q) => q.answer !== null));

  ngOnInit() {
    this.load();
    this.timer = setInterval(() => this.load(), 5000);
  }

  ngOnDestroy() {
    clearInterval(this.timer);
  }

  load() {
    this.api.questions(false).subscribe({
      next: (q) => this.questions.set([...q].reverse()),
      error: () => {},
    });
  }

  setDraft(id: number, value: string) {
    this.drafts.update((d) => ({ ...d, [id]: value }));
  }

  send(q: AgentQuestion) {
    const text = (this.drafts()[q.id] ?? '').trim();
    if (!text) return;
    this.api.answer(q.id, text).subscribe(() => {
      this.drafts.update((d) => ({ ...d, [q.id]: '' }));
      this.load();
    });
  }
}