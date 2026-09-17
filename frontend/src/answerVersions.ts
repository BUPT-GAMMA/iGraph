import type { Message } from './types';

/** Only explicit regeneration links group answers; repeated text is a separate turn. */
export function answerVersions(message: Message, messages: Message[]): Message[] {
  const byRun = new Map(messages.filter(item => item.role === 'assistant' && item.run_id).map(item => [item.run_id!, item]));
  const chain: Message[] = [];
  const visited = new Set<Message>();
  let current: Message | undefined = message;
  while (current && !visited.has(current)) {
    chain.unshift(current); visited.add(current);
    current = current.regenerate_of ? byRun.get(current.regenerate_of) : undefined;
  }
  return chain;
}
