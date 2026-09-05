// Real browser form verification. Requires Playwright; no existing browser/profile is used.
// node tests/browser_oauth.cjs --playwright=/path/to/node_modules/playwright [--expect-origin-error]
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const args = process.argv.slice(2);
const supplied = args.find(arg => arg.startsWith('--playwright='));
const { chromium } = require(supplied ? supplied.slice('--playwright='.length) : 'playwright');
const root = path.resolve(__dirname, '..');
const configArg = args.find(arg => arg.startsWith('--config='));
const config = JSON.parse(fs.readFileSync(configArg ? configArg.slice('--config='.length) :
  path.join(root, 'config.local.json')));
const issuer = config.public_url;
const callback = config.browser_test_callback || 'https://chatgpt.com/connector_platform_oauth_redirect';
const verifier = crypto.randomBytes(48).toString('base64url');
const challenge = crypto.createHash('sha256').update(verifier).digest('base64url');
let phase = 'launch';
let cspBlocked = false;

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();
  page.on('console', message => {
    if (message.text().includes('form-action')) cspBlocked = true;
  });
  let access, credentials;
  let callbackQuery;
  try {
    phase = 'registration';
    let response = await context.request.post(issuer + '/register', { data: {
      client_name: 'OppenProject browser verification', redirect_uris: [callback],
      token_endpoint_auth_method: 'none', grant_types: ['authorization_code', 'refresh_token'],
      response_types: ['code'], scope: 'governance:read',
    }});
    assert.equal(response.status(), 201, 'DCR must succeed');
    credentials = await response.json();
    const query = new URLSearchParams({ client_id: credentials.client_id, response_type: 'code',
      redirect_uri: callback, scope: 'governance:read', state: 'isolated-browser-verification',
      resource: issuer + '/mcp', code_challenge: challenge, code_challenge_method: 'S256' });
    phase = 'consent page';
    const consent = await page.goto(issuer + '/authorize?' + query);
    const policy = consent.headers()['referrer-policy'];
    const expectError = args.includes('--expect-origin-error');
    const invalidPassword = args.includes('--invalid-password');
    if (!expectError) {
      assert(consent.headers()['content-security-policy'].includes("form-action 'self' " + callback));
      assert.equal((await consent.headersArray()).filter(h =>
        h.name.toLowerCase() === 'content-security-policy').length, 1);
    }
    assert(expectError || invalidPassword || new URL(issuer).hostname === '127.0.0.1',
      'Owner credential browser verification is restricted to an isolated loopback server');
    assert(expectError || invalidPassword || new URL(callback).hostname === '127.0.0.1',
      'The complete browser test must use a loopback callback fixture');
    const password = expectError || invalidPassword ? 'non-secret-verification-value' : fs.readFileSync(
      path.join(config.state_dir, 'owner-access.txt'), 'utf8').trim();
    phase = 'form submit';
    await page.locator('#password').fill(password);
    const submitted = page.waitForResponse(r => r.url() === issuer + '/consent' && r.request().method() === 'POST');
    await page.locator('button[value="allow"]').click({ noWaitAfter: true });
    response = await submitted;
    const sent = await response.request().allHeaders();
    if (expectError) {
      assert.equal(sent.origin, 'null');
      assert.equal(response.status(), 403);
      assert.equal((await response.json()).error, 'Untrusted Origin');
      console.log(JSON.stringify({ status: 'reproduced', referrer_policy: policy, origin: sent.origin,
        http_status: response.status(), error: 'Untrusted Origin' }));
      return;
    }
    assert.equal(sent.origin, issuer, 'Browser must generate its own same-origin Origin header');
    if (invalidPassword) {
      assert.equal(response.status(), 401, 'Browser must reach the password check without an Origin error');
      assert((await response.text()).includes('访问口令不正确'));
      console.log(JSON.stringify({ status: 'passed', transport: issuer, browser: 'chromium',
        referrer_policy: policy, browser_origin: sent.origin, invalid_password_rejected: 401,
        callback_path_allowed: true, csp_header_count: 1 }));
      return;
    }
    assert.equal(response.status(), 303, 'Valid owner consent must issue the authorization redirect');
    phase = 'callback redirect';
    await page.waitForURL(url => url.origin === new URL(callback).origin &&
      url.pathname === new URL(callback).pathname, { timeout: 10000 });
    callbackQuery = new URL(page.url()).searchParams;
    assert(!cspBlocked, 'CSP must allow the registered OAuth callback redirect');
    assert.equal(callbackQuery.get('iss'), issuer);
    assert.equal(callbackQuery.get('state'), 'isolated-browser-verification');
    response = await context.request.post(issuer + '/token', { form: {
      grant_type: 'authorization_code', client_id: credentials.client_id, redirect_uri: callback,
      code: callbackQuery.get('code'), code_verifier: verifier, resource: issuer + '/mcp',
    }});
    assert.equal(response.status(), 200, 'Browser-issued code must be redeemable with PKCE');
    access = (await response.json()).access_token;
    console.log(JSON.stringify({ status: 'passed', transport: issuer, browser: 'chromium',
      referrer_policy: policy, browser_origin: sent.origin, owner_consent: 303,
      callback_redirect: 'passed', pkce_exchange: 200 }));
  } finally {
    if (access) {
      const response = await context.request.post(issuer + '/revoke', {
        form: { client_id: credentials.client_id, token: access },
      });
      assert.equal(response.status(), 200, 'Verification authorization must be revoked');
    }
    await browser.close();
  }
})().catch(error => {
  // Do not print Playwright's request log, which can include a credential-bearing URL.
  console.error(error.name + ': phase=' + phase + ', form_action_blocked=' + cspBlocked + '; ' +
    (error instanceof assert.AssertionError ? error.message : 'Browser verification failed; no payloads logged'));
  process.exitCode = 1;
});
