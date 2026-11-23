// The pre-i18n suite asserts the established Chinese copy. Individual locale
// tests clear this preference or provide an explicit language.
localStorage.setItem('igraph-language', 'zh');

import { beforeEach, vi } from 'vitest';
// App-level suites mount the workspace directly, outside the real AuthGate.
// Supply the authenticated identity that those fixtures represent.
beforeEach(async () => {
  const api = await vi.importActual<typeof import('./api')>('./api');
  api.setAuthSession({ user: { user_id: 'admin', username: 'admin' }, csrf_token: 'test-csrf' });
});
