'use strict';
const fs = require('node:fs');
const {chromium} = require('playwright-core');

async function measure() {
  const [origin, question, output, executablePath] = process.argv.slice(2);
  if (new URL(origin).hostname !== '127.0.0.1') throw new Error('A loopback workbench is required');
  const browser = await chromium.launch({executablePath, headless: true, args: [
    '--disable-background-networking', '--no-proxy-server', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1'
  ]});
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 960}, locale: 'zh-CN'});
    await context.route('**/*', route => {
      const same = new URL(route.request().url()).origin === origin;
      return same ? route.continue() : route.abort();
    });
    const page = await context.newPage();
    await page.goto(origin);
    await page.locator('#question-input:enabled').waitFor();
    await Promise.all([
      page.waitForResponse(r => new URL(r.url()).pathname === '/api/conversations' &&
        r.request().method() === 'POST' && r.ok()),
      page.locator('#new-conversation-button').click()
    ]);
    await page.waitForFunction(() => !document.querySelector('.conversation-message') &&
      document.querySelector('#question-input:enabled'));
    await page.locator('#question-input').fill(question);
    await page.evaluate(() => {
      // Observe frames the same way for all runs. The clock starts on the trusted send click.
      document.addEventListener('click', event => {
        if (!event.isTrusted || !event.target.closest('#question-form button[type=submit]')) return;
        const start = performance.now();
        window.measurement = {first: null, final: null, report_labels_in_findings: []};
        const snapshot = (element, now) => {
          const rect = element.getBoundingClientRect();
          return {seconds: (now - start) / 1000, text: element.innerText,
            in_viewport: rect.bottom > 0 && rect.top < innerHeight};
        };
        function sample() {
          const region = document.querySelector('#conversation-analysis-live');
          for (const group of region?.querySelectorAll('section[aria-label]') || []) {
            if (!/已查[實实]的[結结][論论]|Verified findings/.test(group.getAttribute('aria-label'))) continue;
            for (const row of group.querySelectorAll('li')) {
              if (!row.getClientRects().length) continue;
              const record = snapshot(row, performance.now());
              if (/MESSAGE-TEXT/i.test(record.text) && !window.measurement.report_labels_in_findings.includes(record.text)) {
                window.measurement.report_labels_in_findings.push(record.text);
              }
              if (!window.measurement.first) window.measurement.first = record;
              if (!window.measurement.first_deduction && /1-RESERVECREDIT/i.test(record.text) &&
                  /CREDIT-AMT/i.test(record.text) && /start\.cbl/.test(record.text)) {
                window.measurement.first_deduction = record;
              }
            }
          }
          const final = document.querySelector('.conversation-message.from-assistant .message-content');
          if (final?.getClientRects().length && final.innerText.trim()) {
            window.measurement.final = snapshot(final, performance.now());
            return;
          }
          requestAnimationFrame(sample);
        }
        requestAnimationFrame(sample);
      }, {capture: true, once: true});
    });
    await page.locator('#question-form button[type=submit]').click();
    await page.waitForFunction(() => window.measurement?.final, null, {timeout: 180000});
    const result = await page.evaluate(() => window.measurement);
    result.browser_version = browser.version();
    result.question = question;
    fs.writeFileSync(output, JSON.stringify(result, null, 2) + '\n');
  } finally {
    await browser.close();
  }
}
measure().catch(error => {console.error(error.message); process.exitCode=1;});
