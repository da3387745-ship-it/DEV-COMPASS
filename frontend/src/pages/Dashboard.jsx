export function Dashboard({ layout, icon }) {
  const metrics = [
    ['description', 'Indexed docs', 'metric-documents', 'Available in chat'],
    ['forum', 'Questions sent', 'metric-queries', 'Saved to your account'],
  ]
  const metricCards = metrics.map(([symbol, label, id, note]) => `
    <article class="stat panel">
      <div class="stat-topline">
        <span class="stat-icon">${icon(symbol)}</span>
        <span class="stat-label">${label}</span>
      </div>
      <div class="stat-number" id="${id}">—</div>
      <span class="stat-foot" id="${id}-note">${note}</span>
    </article>
  `).join('')

  return layout(`
    <main class="container page-content">
      <div class="page-heading">
        <div>
          <span class="eyebrow">Your account</span>
          <h1>Your library</h1>
          <p>A calm overview of your documents and recent conversations.</p>
        </div>
        <button class="secondary-button" data-refresh-dashboard>
          ${icon('refresh')} Refresh
        </button>
      </div>
      <div class="stats">${metricCards}</div>
      <div class="dashboard-columns">
        <section class="activity">
          <div class="section-head">
            <div>
              <span class="eyebrow">Recent</span>
              <h2>Recent activity</h2>
            </div>
            <span id="dashboard-activity-count">Account history</span>
          </div>
          <div class="activity-list panel" id="dashboard-activity">
            <p class="empty-state">Your recent questions and uploads will appear here.</p>
          </div>
        </section>
        <aside class="dashboard-side panel">
          <span class="eyebrow">Quick action</span>
          <h2>Keep working</h2>
          <p>Ask a question against an indexed document or add another source.</p>
          <button class="primary-button" data-route="chat">
            Open document chat ${icon('arrow_forward')}
          </button>
          <button class="secondary-button" data-route="settings">Profile & settings</button>
        </aside>
      </div>
    </main>
  `, 'dashboard')
}
