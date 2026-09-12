// Example: fill the relayed Turnstile token into the high-risk browser's form
// and submit.  Run this inside the high-risk browser (userscript, automation
// script, or DevTools console) after obtaining a token from POST /solve.
//
// The relay API itself is language-agnostic; this is the browser-side half.

/**
 * Fetch a token from the relay, then submit the form.
 * @param {string} api     Relay API base, e.g. "http://relay-host:8081"
 * @param {string} url     Target page URL (hostname must match the widget's)
 * @param {string} sitekey Turnstile sitekey from the target page
 */
async function solveAndSubmit(api, url, sitekey) {
  const res = await fetch(`${api}/solve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, sitekey, timeout: 180 }),
  });
  const data = await res.json();
  if (!data.ok) throw new Error(`relay failed: HTTP ${res.status} ${data.error}`);

  // Turnstile puts the token in a hidden input named cf-turnstile-response.
  // Some sites also mirror it into #cf-chl-widget-..._response.
  const input = document.querySelector('input[name="cf-turnstile-response"]');
  if (!input) throw new Error('cf-turnstile-response input not found on this page');
  input.value = data.token;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));

  input.form.requestSubmit(); // submit without needing Cloudflare connectivity
}

// Example invocation (adjust to your flow):
// solveAndSubmit('http://relay-host:8081', 'https://target.example/login', '0x4AAAAAAAxxxxxxxxxxxxxxxx');
