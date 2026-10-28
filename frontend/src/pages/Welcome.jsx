export function Welcome({ layout, icon }) {
  return layout(`
    <main class="auth-screen">
      <section class="auth-welcome">
        <img class="auth-brand-logo" src="logo.png" alt="DevCompass">
        <span class="eyebrow">For your project docs</span>
        <h1>Your documents, easier to understand.</h1>
        <p>Ask focused questions about your files and get answers grounded in their contents.</p>
        <div class="auth-actions">
          <button class="primary-button" data-route="login">Log in</button>
          <button class="secondary-button" data-route="signup">Create account</button>
        </div>
        <div class="auth-divider"><span>or</span></div>
        <button class="guest-button" data-route="chat">
          Continue as guest ${icon('arrow_forward')}
        </button>
        <small class="auth-caption">Guest mode opens the document chat on this device.</small>
      </section>
    </main>
  `, 'home', true)
}
