
import { Routes } from '@angular/router';
import { EmployeeChat } from './employee-chat/employee-chat';
import { AgentInbox } from './agent-inbox/agent-inbox';

export const routes: Routes = [
  { path: '', component: EmployeeChat },
  { path: 'agent', component: AgentInbox },
  { path: '**', redirectTo: '' },
];