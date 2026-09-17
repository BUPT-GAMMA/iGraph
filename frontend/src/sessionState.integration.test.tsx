import { useReducer } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { conversationReducer, initialConversationState, selectMessages } from './sessionState';

function initialState() {
  let state = conversationReducer(initialConversationState, { type: 'ENSURE_SESSION', sessionId: 'A' });
  state = conversationReducer(state, { type: 'ENSURE_SESSION', sessionId: 'B' });
  state = conversationReducer(state, {
    type: 'START_RUN', sessionId: 'A', generation: 'gen-a',
    userMessage: { id: 'user:gen-a', role: 'user', content: 'A question' },
    assistantMessage: { id: 'assistant:gen-a', role: 'assistant', content: '', streaming: true },
  });
  state = conversationReducer(state, {
    type: 'START_RUN', sessionId: 'B', generation: 'gen-b',
    userMessage: { id: 'user:gen-b', role: 'user', content: 'B question' },
    assistantMessage: { id: 'assistant:gen-b', role: 'assistant', content: '', streaming: true },
  });
  return conversationReducer(state, { type: 'SET_ACTIVE_SESSION', sessionId: 'A' });
}

function Harness() {
  const [state, dispatch] = useReducer(conversationReducer, undefined, initialState);
  const active = state.activeSessionId ? state.viewsBySessionId[state.activeSessionId] : null;
  const messages = selectMessages(active);
  return <>
    <div data-testid="active-session">{state.activeSessionId}</div>
    <div data-testid="assistant-content">{messages[messages.length - 1]?.content}</div>
    <button onClick={() => dispatch({ type: 'SET_ACTIVE_SESSION', sessionId: 'A' })}>A</button>
    <button onClick={() => dispatch({ type: 'SET_ACTIVE_SESSION', sessionId: 'B' })}>B</button>
    <button onClick={() => dispatch({
      type: 'APPLY_BATCH', sessionId: 'A', generation: 'gen-a',
      batch: { protocol_version: 2, session_id: 'A', run_id: 'run-a', event_index: 0, events: [{ type: 'token', message_id: 'assistant:run-a', content: 'background A' }] },
    })}>token A</button>
    <button onClick={() => dispatch({
      type: 'APPLY_BATCH', sessionId: 'B', generation: 'gen-b',
      batch: { protocol_version: 2, session_id: 'B', run_id: 'run-b', event_index: 0, events: [{ type: 'token', message_id: 'assistant:run-b', content: 'parallel B' }] },
    })}>token B</button>
  </>;
}

describe('React session switching integration', () => {
  it('keeps background stream events in their target session and restores them on return', () => {
    render(<Harness />);
    fireEvent.click(screen.getByText('B'));
    fireEvent.click(screen.getByText('token A'));
    expect(screen.getByTestId('active-session').textContent).toBe('B');
    expect(screen.getByTestId('assistant-content').textContent).toBe('');

    fireEvent.click(screen.getByText('token B'));
    expect(screen.getByTestId('assistant-content').textContent).toBe('parallel B');

    fireEvent.click(screen.getByText('A'));
    expect(screen.getByTestId('assistant-content').textContent).toBe('background A');
  });
});
