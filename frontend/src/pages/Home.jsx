export function Home({ layout, icon, currentUser }) {
  return layout(`
    <main>
      <section class="hero section">
        <div class="container hero-grid">
          <div class="hero-copy">
            <span class="eyebrow" id="home-greeting"></span>
            <span class="eyebrow">A clearer way into your codebase</span>
            <h1>Find answers in your project files.</h1>
            <p>Add PDFs, notes, Markdown, or Jupyter notebooks. Ask about setup, code, or decisions and see which file the answer came from.</p>
            <div class="button-row">
              <button class="primary-button" data-route="${currentUser ? 'dashboard' : 'signup'}">
                ${currentUser ? 'Open dashboard' : 'Get started'} ${icon('arrow_forward')}
              </button>
              <button class="secondary-button" data-route="chat">
                ${currentUser ? 'Open chat' : 'Try the chat'}
              </button>
              ${currentUser ? '' : '<button class="quiet-button" data-route="login">Log in</button>'}
            </div>
          </div>
          <div class="preview panel">
            <div class="panel-title"><span>From the project README</span><code>README.md</code></div>
            <div class="question"><label>Question</label>How do I start the app locally?</div>
            <div class="answer">
              <div class="answer-title"><span>From the file</span></div>
              <p>Run <code>python other/start_app.py</code> from the project root.</p>
              <code>Source: README.md</code>
            </div>
          </div>
        </div>
      </section>
      <section class="section muted">
        <div class="container">
          <span class="eyebrow">A practical starting point</span>
          <h2>Get familiar with a codebase, one question at a time.</h2>
          <div class="steps">
            <article class="step panel"><div class="icon-box">${icon('upload_file')}</div><h3>Add your project files</h3><p>Bring in the README, setup notes, and notebooks you already use.</p></article>
            <article class="step panel"><div class="icon-box">${icon('chat')}</div><h3>Ask what you need</h3><p>Find setup steps, explain a module, or look up a project decision.</p></article>
            <article class="step panel"><div class="icon-box">${icon('fact_check')}</div><h3>Check the source</h3><p>Follow the answer back to its file and confirm the details.</p></article>
          </div>
        </div>
      </section>
      <section class="section">
        <div class="container contact-band">
          <div>
            <span class="eyebrow">Get in touch</span>
            <h2>Questions or feedback?</h2>
            <p>I’d be happy to hear from you.</p>
          </div>
          <div class="contact-details footer-contact" aria-label="Contact details">
            <a href="mailto:danis.techinican@gmail.com">
              ${icon('mail')} danis.techinican@gmail.com
            </a>
            <a href="tel:9922770195">
              ${icon('call')} 9922770195
            </a>
          </div>
        </div>
      </section>
    </main>
  `, 'home')
}
