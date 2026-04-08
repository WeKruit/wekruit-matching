const { test, expect } = require('@playwright/test');

const BASE_URL = 'http://127.0.0.1:8011';

test('jobs page renders key sections on desktop', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (error) => errors.push(String(error)));
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text());
  });

  const response = await page.goto(`${BASE_URL}/internal/jobs`, {
    waitUntil: 'networkidle',
  });

  expect(response && response.status()).toBe(200);
  await expect(page.getByRole('heading', { level: 1, name: 'Active Jobs' })).toBeVisible();
  await expect(page.getByText('Filters')).toBeVisible();
  await expect(page.getByText('Result pages')).toBeVisible();
  await page.screenshot({ path: '/tmp/wekruit-jobs.png', fullPage: true });
  expect(errors).toEqual([]);
});

test('stats page renders overview sections on desktop', async ({ page }) => {
  const response = await page.goto(`${BASE_URL}/internal/stats`, {
    waitUntil: 'networkidle',
  });

  expect(response && response.status()).toBe(200);
  await expect(page.getByRole('heading', { level: 1, name: 'Stats' })).toBeVisible();
  await expect(page.getByText('Inventory at a glance')).toBeVisible();
  await expect(page.getByText('Top industries')).toBeVisible();
  await page.screenshot({ path: '/tmp/wekruit-stats.png', fullPage: true });
});

test('pipeline page renders JD observability sections on desktop', async ({ page }) => {
  const response = await page.goto(`${BASE_URL}/internal/pipeline`, {
    waitUntil: 'networkidle',
  });

  expect(response && response.status()).toBe(200);
  await expect(page.getByRole('heading', { level: 1, name: 'Pipeline' })).toBeVisible();
  await expect(page.getByText('Jobs waiting for JD fetch')).toBeVisible();
  await expect(page.getByText('Failed JD attempts')).toBeVisible();
  await expect(page.getByText('JD coverage by source')).toBeVisible();
  await expect(page.getByText('Quality distribution')).toBeVisible();
  await page.screenshot({ path: '/tmp/wekruit-pipeline.png', fullPage: true });
});

test('jobs page keeps mobile card layout', async ({ browser }) => {
  const page = await browser.newPage({
    viewport: { width: 390, height: 844 },
  });

  const response = await page.goto(`${BASE_URL}/internal/jobs`, {
    waitUntil: 'networkidle',
  });

  expect(response && response.status()).toBe(200);
  await expect(page.locator('.job-list-mobile')).toBeVisible();
  await page.screenshot({ path: '/tmp/wekruit-jobs-mobile.png', fullPage: true });
  await page.close();
});
