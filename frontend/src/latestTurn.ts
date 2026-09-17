import type { Message } from './types';

export function editableLatestTurn(messages: Message[]) {
  const assistant = messages[messages.length - 1];
  if (!assistant || assistant.role !== 'assistant' || assistant.streaming || !assistant.run_id
    || !['completed', 'cancelled'].includes(assistant.run_status || '')) return null;
  const user = [...messages].reverse().find(message => message.role === 'user');
  if (!user || (user.run_id && user.run_id !== assistant.run_id)) return null;
  return { user, assistant };
}
