import { test, expect } from './fixtures';

// Signing in lands on /api/_clear_sw, which clears a stale service worker and
// then loads the dashboard. It used to load "/" and drop the page's own link,
// so signing in from #peek=worker&tab=chat put you on the home view (Ethan,
// 2026-10-26: "make sure u test deep linking and it opening up with logged in
// session"). The desktop half, the real sign-in in the Mac app, is
// desktop/test/split-button-e2e.py.
test('the sign-in landing comes back to the deep link it left from', async ({ page }) => {
  await page.goto('/api/_clear_sw#peek=w1&tab=chat');
  await expect.poll(() => new URL(page.url()).pathname + new URL(page.url()).hash, { timeout: 15_000 }).toBe('/#peek=w1&tab=chat');
});

test('a hash that is not a plain link is dropped, not carried', async ({ page }) => {
  await page.goto("/api/_clear_sw#x');alert(1)//");
  await expect.poll(() => new URL(page.url()).pathname + new URL(page.url()).search, { timeout: 15_000 }).toMatch(/^\/(\?.*)?$/);
  expect(new URL(page.url()).hash).toBe('');
});
