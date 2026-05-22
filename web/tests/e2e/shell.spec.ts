import { expect, test } from '@playwright/test';

test.describe('Phase 6 commit 39 shell', () => {
  test('renders the app shell with nav links', async ({ page }) => {
    await page.goto('/ui/');
    await expect(page.getByRole('heading', { name: 'Welcome.' })).toBeVisible();

    for (const label of ['Ingest', 'Run', 'Jobs', 'Catalog', 'Search', 'Cost', 'Profiles', 'Settings']) {
      await expect(page.getByRole('link', { name: label })).toBeVisible();
    }
  });

  test('exposes the namespace badge in the header', async ({ page }) => {
    await page.goto('/ui/');
    await expect(page.getByTitle(/Engine namespace/i)).toContainText('ns: default');
  });
});
