export function AccountPage(mode, { layout, icon }) {
  const isSignup = mode === 'signup'
  const title = isSignup ? 'Create your account' : 'Welcome back'
  const action = isSignup ? 'Create account' : 'Log in'
  const signupFields = isSignup
    ? `
      <label for="account-name">Username</label>
      <input id="account-name" type="text" maxlength="80" autocomplete="name" required>
    `
    : ''
  const passwordPlaceholder = isSignup ? 'At least 12 characters' : 'Enter your password'
  const passwordMinLength = isSignup ? '12' : '1'
  const passwordAutocomplete = isSignup ? 'new-password' : 'current-password'
  const confirmationField = isSignup
    ? `
      <label for="account-confirm">Confirm password</label>
      <div class="password-input-wrap">
        <input
          id="account-confirm"
          type="password"
          minlength="12"
          maxlength="256"
          autocomplete="new-password"
          required
        >
        <button class="password-toggle" type="button" data-toggle-password="account-confirm" aria-label="Show confirmation password" aria-pressed="false">Show</button>
      </div>
    `
    : ''
  const switchLabel = isSignup ? 'Already have an account?' : 'New to DevCompass?'
  const switchRoute = isSignup ? 'login' : 'signup'
  const switchAction = isSignup ? 'Log in' : 'Create an account'

  return layout(`
    <main class="auth-screen">
      <section class="auth-card">
        <button class="auth-back" data-route="welcome">${icon('arrow_back')} Back</button>
        <img class="auth-brand-logo" src="logo.png" alt="DevCompass">
        <span class="eyebrow">Account access</span>
        <h1>${title}</h1>
        <p class="auth-subtitle">
          ${isSignup
            ? 'Create an account to save your profile, settings, and activity.'
            : 'Log in to open your saved chats and files.'}
        </p>
        <form class="auth-form" id="account-form" data-auth-mode="${mode}">
          ${signupFields}
          <label for="account-email">Email</label>
          <input
            id="account-email"
            type="email"
            placeholder="you@example.com"
            autocomplete="email"
            required
          >
          <label for="account-password">Password</label>
          <div class="password-input-wrap">
            <input
              id="account-password"
              type="password"
              placeholder="${passwordPlaceholder}"
              minlength="${passwordMinLength}"
              maxlength="256"
              autocomplete="${passwordAutocomplete}"
              required
            >
            <button class="password-toggle" type="button" data-toggle-password="account-password" aria-label="Show password" aria-pressed="false">Show</button>
          </div>
          ${confirmationField}
          <button class="primary-button" type="submit">${action}</button>
          <p class="form-message" id="auth-message" role="alert" hidden></p>
        </form>
        <p class="auth-note">
          Your password is securely hashed on the backend; it is never stored as plain text.
        </p>
        <button class="secondary-button full" data-route="chat">
          Continue as guest ${icon('arrow_forward')}
        </button>
        <p class="auth-switch">
          ${switchLabel}
          <button type="button" data-route="${switchRoute}">${switchAction}</button>
        </p>
      </section>
    </main>
  `, 'home', true)
}
