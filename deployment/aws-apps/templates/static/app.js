import configuration from '/config.js';
const $ = id => document.getElementById(id);
const say = message => { $('status').textContent = message; };
const random = () => Array.from(crypto.getRandomValues(new Uint8Array(32)), byte => byte.toString(16).padStart(2, '0')).join('');
const base64url = bytes => btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const issuer = configuration.VITE_CORE_ISSUER;
const client = configuration.VITE_CORE_BROWSER_CLIENT_ID;
const origin = configuration.VITE_APPLICATION_ORIGIN || location.origin;
const api = configuration.VITE_API_URL;
const callback = `${origin}/oauth/callback`;
let token = sessionStorage.getItem('nce.access_token') || '';

async function signIn(invitation = false) {
  if (!issuer || !client) throw new Error('Core sign-in has not been configured yet.');
  const verifier = random();
  const state = random();
  const challenge = base64url(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))));
  sessionStorage.setItem('nce.login', JSON.stringify({ verifier, state, created: Date.now() }));
  const authorization = new URL('/oauth/authorize/', issuer);
  authorization.search = new URLSearchParams({ client_id: client, redirect_uri: callback, response_type: 'code',
    scope: 'openid authorization', state, code_challenge: challenge, code_challenge_method: 'S256' }).toString();
  if (invitation) {
    const signup = new URL('/accounts/app-signup/', issuer);
    signup.searchParams.set('next', authorization.pathname + authorization.search);
    location.assign(signup.href);
  } else location.assign(authorization.href);
}

async function authenticated() {
  $('signout').hidden = !token;
  $('signin').hidden = Boolean(token);
  $('signup').hidden = Boolean(token);
  if (!token) return;
  if (!api) {
    const response = await fetch(`${issuer}/oauth/userinfo/`, { headers: { Authorization: `Bearer ${token}` }, cache: 'no-store' });
    if (!response.ok) throw new Error('Your Core session could not be verified. Sign in again.');
    say('Signed in with your shared Core account.');
    return;
  }
  const response = await fetch(`${api}/notes`, { headers: { Authorization: `Bearer ${token}` }, cache: 'no-store' });
  if (!response.ok) {
    if (response.status === 403) throw new Error('Your account is signed in. Ask your administrator for application access.');
    throw new Error('Application access could not be verified. Sign in again.');
  }
  const result = await response.json();
  $('note').value = result.note;
  $('notes').hidden = false;
  say('Signed in. Your application read permission is working.');
}

async function initialize() {
  if (location.pathname.startsWith('/oauth/callback')) {
    const query = new URLSearchParams(location.search);
    const pending = JSON.parse(sessionStorage.getItem('nce.login') || 'null');
    sessionStorage.removeItem('nce.login');
    history.replaceState({}, '', '/');
    if (query.has('error')) throw new Error('Sign-in was cancelled or denied.');
    if (!pending || pending.state !== query.get('state') || Date.now() - pending.created > 600000 || !query.get('code'))
      throw new Error('This sign-in has expired. Start again.');
    const response = await fetch(`${issuer}/oauth/token/`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ grant_type: 'authorization_code', code: query.get('code'), client_id: client, redirect_uri: callback, code_verifier: pending.verifier }) });
    if (!response.ok) throw new Error('Core could not complete sign-in. Please try again.');
    const result = await response.json();
    if (typeof result.access_token !== 'string' || result.token_type?.toLowerCase() !== 'bearer') throw new Error('Core returned an invalid sign-in response.');
    token = result.access_token;
    sessionStorage.setItem('nce.access_token', token);
  }
  if (new URLSearchParams(location.search).get('account') === 'created') say('Your shared account is ready. Ask your administrator to grant app access, then sign in.');
  await authenticated();
}

$('signin').onclick = () => signIn().catch(error => say(error.message));
$('signup').onclick = () => signIn(true).catch(error => say(error.message));
$('signout').onclick = () => { sessionStorage.removeItem('nce.access_token'); token = ''; location.assign('/'); };
$('save').onclick = async () => {
  $('save').disabled = true;
  try {
    const response = await fetch(`${api}/notes`, { method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, body: JSON.stringify({ note: $('note').value }) });
    if (!response.ok) throw new Error(response.status === 403 ? 'Ask your administrator for app-write permission.' : 'Your note could not be saved.');
    say('Saved. Your application write permission is working.');
  } catch (error) { say(error.message); } finally { $('save').disabled = false; }
};
initialize().catch(error => say(error.message));
