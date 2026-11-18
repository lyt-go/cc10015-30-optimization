import { expect, test } from '@playwright/test'

test('预测模型：结果表稳定、详情一致、无重复行', async ({ page }) => {
  test.setTimeout(120_000)
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('response', (r) => { if (r.url().includes('/api/') && r.status() >= 400) errors.push(`${r.status()} ${r.url()}`) })

  await page.goto('/forecast_model')
  await expect(page.getByTestId('kpi-card').first()).toBeVisible()
  await expect(page.getByTestId('config-table')).toBeVisible()

  // 对零斜率模型训练：结果表应可解释且稳定
  const modelRow = page.locator('[data-testid="config-table"] tbody tr').filter({ hasText: 'FORE-0002' })
  await modelRow.getByTestId('train-model').click()
  await expect(page.getByTestId('prediction-table')).toBeVisible()
  const rows = page.getByTestId('prediction-row')
  await expect(rows).toHaveCount(3)
  const periodCells = await rows.evaluateAll((items) =>
    items.map((row) => row.children[3]?.textContent ?? ''))
  expect(periodCells).toEqual(['第7周期', '第8周期', '第9周期'])
  const rowText = await rows.evaluateAll((items) => items.map((row) => row.textContent ?? '').join(''))
  expect(rowText).toContain('最近值预测')
  expect(rowText).toContain('零增长边界')

  // 重复点击训练不应产生重复行，也不应让预测点不动
  await modelRow.getByTestId('train-model').click()
  await expect(page.getByTestId('prediction-row')).toHaveCount(3)
  const periodsAgain = await page.getByTestId('prediction-row').evaluateAll((items) =>
    items.map((row) => row.children[3]?.textContent ?? ''))
  expect(periodsAgain).toEqual(['第7周期', '第8周期', '第9周期'])

  // 工作台与详情页保留同一结果
  await modelRow.getByTestId('open-detail').click()
  await expect(page.getByTestId('detail-panel')).toBeVisible()
  const detailPeriods = await page.getByTestId('detail-prediction-table').locator('tbody tr').evaluateAll(
    (items) => items.map((row) => row.children[3]?.textContent ?? ''))
  expect(detailPeriods).toEqual(periodsAgain)

  expect(errors).toEqual([])
})

test('指标定义读取预测结果表并校验周期位置', async ({ page, request }) => {
  test.setTimeout(120_000)
  // 先确保 METR-0001 有一条已发布预测结果
  const train = await request.post('/api/forecast_model/run', {
    data: { values: { 模型编号: 'FORE-0001' } },
  })
  expect(train.ok()).toBeTruthy()

  await page.goto('/metric_def')
  const row = page.locator('tbody tr[data-testid^="row-"]').filter({ hasText: 'METR-0001' }).first()
  await row.getByTestId('forecast-link').click()
  await expect(page.getByTestId('forecast-link-panel')).toBeVisible()
  await expect(page.getByTestId('forecast-link-status')).toContainText('校验通过')
  await expect(page.getByTestId('forecast-link-panel')).toContainText('第8周期')
})
