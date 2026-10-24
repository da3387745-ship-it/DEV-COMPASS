const routes = ['home', 'dashboard', 'chat', 'settings', 'docs', 'welcome', 'login', 'signup']
let activeDocument = null
let currentUser = null
let chatHistoryEntries = []
let activeChatId = null
let availableDocuments = []
let editingTurn = null
const API_PROTOCOL = window.location.protocol === 'https:' ? 'https' : 'http'
const API_HOST = window.location.hostname || '127.0.0.1'
const renderBackendBase = window.location.hostname === 'dev-compass-1.onrender.com'
  ? 'https://dev-compass.onrender.com'
  : ''
const API_BASE = renderBackendBase || (window.location.protocol === 'file:'
  || ['5173', '8001'].includes(window.location.port)
  ? `${API_PROTOCOL}://${API_HOST}:8000`
  : window.location.origin)

function apiFetch(path, options = {}) {
  return fetch(`${API_BASE}${path}`, { ...options, credentials: 'include' })
}

async function ensureGuestSession() {
  if (currentUser) return
  const response = await apiFetch('/guest/session')
  if (!response.ok) throw new Error('Could not start a guest session.')
}

async function switchToGuestMode() {
  currentUser = null
  activeChatId = null
  activeDocument = null
  availableDocuments = []
  const sidebarUsername = document.querySelector('#sidebar-username')
  if (sidebarUsername) sidebarUsername.textContent = 'Guest'
  const sidebarProfileLabel = document.querySelector('#sidebar-profile-label')
  if (sidebarProfileLabel) sidebarProfileLabel.textContent = 'Sign in to save chats'
  const sidebarAvatar = document.querySelector('#sidebar-avatar')
  if (sidebarAvatar) {
    sidebarAvatar.textContent = 'G'
    sidebarAvatar.style.backgroundImage = ''
    sidebarAvatar.classList.remove('has-avatar')
  }
  const sidebarProfile = document.querySelector('.sidebar-profile')
  if (sidebarProfile) sidebarProfile.dataset.route = 'login'
  syncActiveDocumentUI()
  await ensureGuestSession()
  await Promise.all([loadChatHistory(), loadDocuments()])
}

async function loadDashboardData() {
  const response = await apiFetch('/dashboard')
  if (response.status === 401) {
    go('login')
    return
  }
  if (!response.ok) {
    const result = await response.json().catch(() => ({}))
    throw new Error(result.detail || `Could not load account dashboard (HTTP ${response.status}).`)
  }
  const dashboard = await response.json()
  const queryMetric = document.querySelector('#metric-queries')
  if (queryMetric) queryMetric.textContent = Number(dashboard.question_count || 0).toLocaleString()
  const documentMetric = document.querySelector('#metric-documents')
  if (documentMetric) documentMetric.textContent = Number(dashboard.document_count || 0).toLocaleString()
  const bridgeNote = document.querySelector('#metric-documents-note')
  if (bridgeNote) bridgeNote.textContent = 'Your library'
  const activityRoot = document.querySelector('#dashboard-activity')
  const activity = dashboard.activity || []
  if (activityRoot) {
    activityRoot.replaceChildren()
    if (!activity.length) {
      activityRoot.innerHTML =
        '<p class="empty-state">Your recent questions and uploads will appear here.</p>'
    } else {
      for (const item of activity) {
        const row = document.createElement('div')
        row.className = 'activity-row'
        const badge = document.createElement('span')
        badge.className = 'activity-icon'
        badge.innerHTML = icon(item.type === 'upload' ? 'upload_file' : 'search')
        const text = document.createElement('div')
        const title = document.createElement('b')
        title.textContent = item.title
        const detail = document.createElement('small')
        detail.textContent =
          item.detail ||
          (item.type === 'upload'
            ? 'File added to your library'
            : 'Question asked in chat')
        text.append(title, detail)
        const time = document.createElement('time')
        time.textContent = item.created_at ? new Date(item.created_at).toLocaleString() : ''
        row.append(badge, text, time)
        activityRoot.append(row)
      }
    }
  }
  const activityCount = document.querySelector('#dashboard-activity-count')
  if (activityCount) activityCount.textContent = `${activity.length} recent events`
}

async function loadChatHistory() {
  const historyRoot = document.querySelector('#chat-history-list')
  if (!historyRoot) return
  const count = document.querySelector('#chat-history-count')
  if (!currentUser) {
    historyRoot.innerHTML =
      '<p class="library-empty">Guest chats are not saved. Log in to keep your history.</p>'
    if (count) count.textContent = 'Guest'
    return
  }

  try {
    const response = await apiFetch('/chat/history?limit=20')
    if (response.status === 401) {
      historyRoot.innerHTML =
        '<p class="library-empty">Your session expired. Log in to see saved chats.</p>'
      if (count) count.textContent = 'Sign in'
      return
    }
    if (!response.ok) {
      const result = await response.json().catch(() => ({}))
      throw new Error(result.detail || `Could not load chat history (HTTP ${response.status}).`)
    }
    const data = await response.json()
    chatHistoryEntries = data.sessions || []
    historyRoot.replaceChildren()
    if (count) count.textContent = String(chatHistoryEntries.length)
    if (!chatHistoryEntries.length) {
      historyRoot.innerHTML = '<p class="library-empty">No saved chats yet.</p>'
      return
    }
    chatHistoryEntries.forEach((entry) => {
      const row = document.createElement('div')
      row.className = 'history-row'
      const button = document.createElement('button')
      button.type = 'button'
      button.className = 'history-item'
      button.dataset.chatId = entry._id
      const title = document.createElement('strong')
      title.textContent = entry.title || 'New chat'
      button.append(title)
      const actions = document.createElement('div')
      actions.className = 'history-actions'
      actions.innerHTML =
        '<button type="button" data-rename-chat aria-label="Rename chat">edit</button>' +
        '<button type="button" data-delete-chat aria-label="Delete chat">delete</button>'
      row.append(button, actions)
      historyRoot.append(row)
    })
  } catch (error) {
    historyRoot.innerHTML = ''
    const message = document.createElement('p')
    message.className = 'library-empty'
    message.textContent = error.message
    historyRoot.append(message)
  }
}

async function loadSettingsData() {
  const status = document.querySelector('#settings-index-status')
  try {
    const profileResponse = await apiFetch('/profile')
    if (profileResponse.status === 401) {
      go('login')
      return
    }
    if (!profileResponse.ok) {
      const result = await profileResponse.json().catch(() => ({}))
      throw new Error(result.detail || `Could not load your profile (HTTP ${profileResponse.status}).`)
    }
    const profile = await profileResponse.json()
    currentUser = profile
    const nameField = document.querySelector('#profile-display-name')
    const emailField = document.querySelector('#profile-email')
    const themeField = document.querySelector('#profile-theme')
    const answerStyleField = document.querySelector('#profile-answer-style')
    const avatar = document.querySelector('#profile-avatar')
    if (nameField) nameField.value = profile.username || ''
    if (emailField) emailField.value = profile.email || ''
    if (themeField) themeField.value = profile.settings?.theme || 'light'
    if (answerStyleField) answerStyleField.value = profile.settings?.answer_style || 'concise'
    applyTheme(profile.settings?.theme || 'light')
    if (avatar) {
      avatar.src = profile.has_avatar
        ? `${API_BASE}/profile/avatar?updated=${Date.now()}`
        : 'logo.png'
    }
    const response = await apiFetch('/documents')
    if (!response.ok) {
      const result = await response.json().catch(() => ({}))
      throw new Error(result.detail || `Could not load the document index (HTTP ${response.status}).`)
    }
    const data = await response.json()
    const documents = data.documents || []
    const documentCount = document.querySelector('#settings-document-count')
    if (documentCount) documentCount.textContent = documents.length.toLocaleString()
    if (status) status.textContent = 'Profile and preferences saved to your account.'
  } catch (error) {
    if (status) status.textContent = `Could not load profile/settings: ${error.message}`
  }
}

const icon = (name) => `<span class="material-symbols-outlined">${name}</span>`
const go = (page) => { window.location.hash = `#/${page}` }

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme === 'dark' ? 'dark' : 'light'
}

function currentPage() {
  const page = window.location.hash.replace('#/', '') || 'welcome'
  if (currentUser && ['welcome', 'login', 'signup'].includes(page)) return 'dashboard'
  return routes.includes(page) ? page : 'home'
}

function header(activePage) {
  const links = ['home', 'dashboard', 'chat', 'docs', 'settings']
    .map((page) => {
      const activeClass = activePage === page ? 'active' : ''
      const label = page[0].toUpperCase() + page.slice(1)
      return `<button class="nav-link ${activeClass}" data-route="${page}">${label}</button>`
    })
    .join('')
  const accountActions = currentUser
    ? '<span class="signed-in-name" id="signed-in-name"></span>' +
      '<button class="secondary-button small" data-logout>Log out</button>'
    : '<button class="secondary-button small" data-route="login">Log in</button>' +
      '<button class="primary-button small" data-route="signup">Create account</button>'
  return `
    <header class="site-header">
      <div class="header-inner">
        <button class="logo" data-route="home">
          <img class="brand-logo" src="logo.png" alt="DevCompass logo">
          <span>DevCompass</span>
        </button>
        <nav class="desktop-nav">${links}</nav>
        <div class="header-actions">${accountActions}</div>
      </div>
    </header>
  `
}

function footer() {
  return `
    <footer class="site-footer">
      <span>${icon('explore')} DevCompass · Answers grounded in your files</span>
    </footer>
  `
}

function layout(content, active, plain = false) {
  const pageHeader = plain ? '' : header(active)
  const pageFooter = plain ? '' : footer()
  const pageClass = plain ? 'plain-page' : ''
  return `<div class="app ${pageClass}">${pageHeader}${content}${pageFooter}</div>`
}

function home() {
  return layout(`
    <main>
      <section class="hero section">
        <div class="container hero-grid">
          <div class="hero-copy">
            <span class="eyebrow" id="home-greeting"></span>
            <span class="eyebrow">${icon('description')} Document Intelligence System</span>
            <h1>Ask questions, get answers sourced from your own documents</h1>
            <p>DevCompass indexes PDF, TXT, and Markdown files so you can ask questions about technical notes and guides, with source-file references when document text is used.</p>
            <div class="button-row">
              <button class="primary-button" data-route="${currentUser ? 'dashboard' : 'signup'}">
                ${currentUser ? 'Open dashboard' : 'Get Started'} ${icon('arrow_forward')}
              </button>
              <button class="secondary-button" data-route="chat">
                ${currentUser ? 'Open chat' : 'Try as guest'}
              </button>
              ${currentUser ? '' : '<button class="quiet-button" data-route="login">Log In</button>'}
            </div>
          </div>
          <div class="preview panel">
            <div class="panel-title"><span>${icon('menu_book')} Example</span><code>Document chat</code></div>
            <div class="question"><label>Question:</label>What should I know from this document?</div>
            <div class="answer">
              <div class="answer-title"><span>${icon('description')} Document answer</span><code>Example only</code></div>
              <p>Ask about an uploaded file and DevCompass will use its extracted text to prepare an answer.</p>
              <code>Source-file references appear when available.</code>
            </div>
          </div>
        </div>
      </section>
      <section class="section muted">
        <div class="container">
          <span class="eyebrow">HOW IT WORKS</span>
          <h2>Three simple steps to document-grounded answers</h2>
          <div class="steps">
            <article class="step panel"><div class="icon-box">${icon('upload_file')}</div><h3>1. Upload</h3><p>Drop your PDF, TXT, or Markdown documents.</p></article>
            <article class="step panel"><div class="icon-box blue">${icon('chat')}</div><h3>2. Ask</h3><p>Type natural language questions about your docs.</p></article>
            <article class="step panel"><div class="icon-box ochre">${icon('fact_check')}</div><h3>3. Review the answer</h3><p>Document-based responses can show the source file they used.</p></article>
          </div>
        </div>
      </section>
      <section class="section">
        <div class="container contact-band">
          <div>
            <span class="eyebrow">GET IN TOUCH</span>
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

function welcome() {
  return layout(`
    <main class="auth-screen">
      <section class="auth-welcome">
        <img class="auth-brand-logo" src="logo.png" alt="DevCompass">
        <span class="eyebrow">DOCUMENT INTELLIGENCE</span>
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

function accountPage(mode) {
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
        <span class="eyebrow">ACCOUNT ACCESS</span>
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

function dashboard() {
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
          <span class="eyebrow">YOUR ACCOUNT</span>
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
              <span class="eyebrow">RECENT</span>
              <h2>Recent activity</h2>
            </div>
            <span id="dashboard-activity-count">Account history</span>
          </div>
          <div class="activity-list panel" id="dashboard-activity">
            <p class="empty-state">Your recent questions and uploads will appear here.</p>
          </div>
        </section>
        <aside class="dashboard-side panel">
          <span class="eyebrow">QUICK ACTION</span>
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

function library() {
  return `
    <aside class="library">
      <button class="primary-button full" data-new-chat>
        ${icon('edit_square')} New Chat
      </button>
      <span class="library-label history-label">
        RECENT CHATS <b id="chat-history-count">—</b>
      </span>
      <div id="chat-history-list">
        <p class="library-empty">Guest chats are not saved. Log in to keep your history.</p>
      </div>
      <button class="sidebar-profile" data-route="${currentUser ? 'settings' : 'login'}">
        <span class="avatar avatar-small" id="sidebar-avatar">D</span>
        <span><strong id="sidebar-username">Account</strong><small id="sidebar-profile-label">Profile & settings</small></span>
        ${icon('settings')}
      </button>
    </aside>
  `
}

function chat() {
  return layout(`
    <main class="chat-layout">
      ${library()}
      <section class="chat-main">
        <div class="chat-head">
          <div>
            <span class="icon-box">${icon('auto_awesome')}</span>
            <div>
              <h1 id="chat-title">New chat</h1>
              <small>Ask about your files or start anywhere.</small>
            </div>
          </div>
          <button class="quiet-button" data-route="home">Home</button>
          <button class="icon-button mobile-library-toggle" data-toggle-library aria-label="Toggle chat history">
            ${icon('menu')}
          </button>
        </div>
        <div class="messages" id="messages">
          <div class="welcome-message">
            <span class="icon-box">${icon('auto_awesome')}</span>
            <h2>What would you like to explore?</h2>
            <p>Ask a question or use @ to bring one of your files into the chat.</p>
          </div>
        </div>
        <div class="composer">
          <div class="composer-editing" id="edit-indicator" hidden>
            <span>Editing your message</span>
            <button type="button" data-cancel-edit>Cancel</button>
          </div>
          <div class="composer-box">
            <div class="composer-document-row" data-document-context-row hidden>
              <div class="document-context-block" id="active-document-context" hidden></div>
              <button type="button" class="clear-document-button" data-clear-document aria-label="Clear selected file" title="Use all files">
                ${icon('close')}
              </button>
            </div>
            <div class="attachment-preview" id="attachment-preview" hidden></div>
            <div class="mention-picker" id="mention-picker" hidden></div>
            <input class="file-input" id="file-upload" type="file" accept=".pdf,.txt,.md">
            <textarea
              id="chat-input"
              placeholder="Message DevCompass… use @ to choose a file"
              rows="2"
            ></textarea>
            <div class="composer-tools">
              <label class="attach-button" for="file-upload" title="Add a PDF, TXT, or Markdown file" aria-label="Add a file">
                ${icon('add')}
              </label>
              <span class="composer-hint">Enter to send · Shift + Enter for a new line</span>
              <button class="send-button" data-send aria-label="Send message">${icon('arrow_upward')}</button>
            </div>
          </div>
        </div>
      </section>
    </main>
  `, 'chat', true)
}

function settings() {
  return layout(`
    <main class="container page-content">
      <div class="page-heading">
        <div>
          <span class="eyebrow">YOUR ACCOUNT</span>
          <h1>Settings</h1>
          <p>Make DevCompass feel like yours.</p>
        </div>
        <button class="secondary-button" data-refresh-settings>
          ${icon('refresh')} Refresh
        </button>
      </div>
      <div class="settings-cards">
        <section class="settings-card panel profile-card">
          <div class="settings-card-heading">
            <span class="settings-icon">${icon('account_circle')}</span>
            <div>
              <span class="eyebrow">ACCOUNT</span>
              <h2>Your profile</h2>
            </div>
          </div>
          <div class="avatar-editor">
            <img class="avatar profile-avatar" id="profile-avatar" src="logo.png" alt="Profile picture">
            <div>
              <strong>Profile picture</strong>
              <small>PNG, JPEG, or WebP · up to 2 MB</small>
              <label class="secondary-button small" for="avatar-upload">Change photo</label>
              <input class="file-input" id="avatar-upload" type="file" accept="image/png,image/jpeg,image/webp">
            </div>
          </div>
          <form class="profile-form" id="profile-form">
            <label for="profile-display-name">Username</label>
            <input id="profile-display-name" type="text" maxlength="80" autocomplete="nickname" required>
            <label for="profile-email">Email</label>
            <input id="profile-email" type="email" readonly>
            <label for="profile-theme">Appearance</label>
            <select id="profile-theme">
              <option value="light">Light</option>
              <option value="dark">Dark</option>
            </select>
            <label for="profile-answer-style">Answer style</label>
            <select id="profile-answer-style">
              <option value="concise">Concise</option>
              <option value="detailed">Detailed</option>
            </select>
            <button class="primary-button" type="submit">Save profile & settings</button>
            <p class="form-message" id="profile-message" role="status" hidden></p>
          </form>
        </section>
        <section class="settings-card panel index-card">
          <div class="settings-card-heading">
            <span class="settings-icon ochre">${icon('folder_data')}</span>
            <div>
              <span class="eyebrow">YOUR LIBRARY</span>
              <h2>Files</h2>
            </div>
          </div>
          <div class="index-summary">
            <div>
              <strong id="settings-document-count">—</strong>
              <span>documents</span>
            </div>
          </div>
          <p class="settings-note" id="settings-index-status">Your documents are ready when you are.</p>
        </section>
        <section class="settings-card panel danger-zone">
          <div class="settings-card-heading">
            <span class="settings-icon">${icon('manage_accounts')}</span>
            <div>
              <span class="eyebrow">ACCOUNT CONTROL</span>
              <h2>Delete account</h2>
            </div>
          </div>
          <p>This permanently removes your profile, files, and saved conversations.</p>
          <form id="delete-account-form" class="delete-account-form">
            <label for="delete-account-password">Confirm with your password</label>
            <input id="delete-account-password" type="password" autocomplete="current-password" required>
            <button class="danger-button" type="submit">Delete my account</button>
            <p class="form-message" id="delete-account-message" role="status" hidden></p>
          </form>
        </section>
      </div>
    </main>
  `, 'settings')
}

function docs() {
  return layout(`
    <main class="container page-content">
      <div class="page-heading">
        <div>
          <span class="eyebrow">${icon('menu_book')} DOCUMENTATION</span>
          <h1>Your document library</h1>
          <p>
            Add PDFs, text files, or Markdown in chat, then ask questions with
            source references.
          </p>
        </div>
        <button class="primary-button" data-route="chat">
          Open document chat ${icon('arrow_forward')}
        </button>
      </div>
    </main>
  `, 'home')
}

function syncActiveDocumentUI() {
  const activeChatBlock = document.querySelector('#active-document-context')
  const contextRow = document.querySelector('[data-document-context-row]')
  const activeName = activeDocument?.filename || 'No file selected'
  if (activeChatBlock) {
    activeChatBlock.hidden = !activeDocument
    activeChatBlock.textContent = activeDocument
      ? `Using ${activeName}`
      : ''
  }
  if (contextRow) contextRow.hidden = !activeDocument
}

async function loadDocuments() {
  if (!document.querySelector('#chat-input')) return

  try {
    await ensureGuestSession()
    const response = await apiFetch('/documents')
    if (!response.ok) throw new Error('Could not load your documents.')
    const data = await response.json()
    availableDocuments = data.documents || []
    const selectedFile = availableDocuments.find((file) => file.active)
    if (!activeDocument && selectedFile) {
      activeDocument = {
        filename: selectedFile.filename,
        documentId: selectedFile.document_id,
      }
    }
    syncActiveDocumentUI()
  } catch (error) {
    console.error('Could not load documents for chat:', error)
  }
}

function render() {
  const page = currentPage()
  const pageRenderers = {
    welcome,
    login: () => accountPage('login'),
    signup: () => accountPage('signup'),
    dashboard,
    chat,
    settings,
    docs,
    home,
  }

  document.body.classList.toggle('chat-page', page === 'chat')
  document.body.classList.toggle(
    'auth-flow',
    ['welcome', 'login', 'signup'].includes(page),
  )
  window.renderDevCompassMarkup(pageRenderers[page]())
  const signedInName = document.querySelector('#signed-in-name')
  if (signedInName) signedInName.textContent = currentUser?.username || 'Account'
  const homeGreeting = document.querySelector('#home-greeting')
  if (homeGreeting) {
    homeGreeting.textContent = currentUser?.username
      ? `Welcome back, ${currentUser.username}`
      : ''
  }
  const sidebarUsername = document.querySelector('#sidebar-username')
  if (sidebarUsername) sidebarUsername.textContent = currentUser?.username || 'Guest'
  const sidebarProfileLabel = document.querySelector('#sidebar-profile-label')
  if (sidebarProfileLabel) {
    sidebarProfileLabel.textContent = currentUser ? 'Profile & settings' : 'Sign in to save chats'
  }
  const sidebarAvatar = document.querySelector('#sidebar-avatar')
  if (sidebarAvatar) {
    sidebarAvatar.textContent = (currentUser?.username || 'G').slice(0, 1).toUpperCase()
    if (currentUser?.has_avatar) {
      sidebarAvatar.style.backgroundImage = `url("${API_BASE}/profile/avatar")`
      sidebarAvatar.classList.add('has-avatar')
    }
  }
  syncActiveDocumentUI()
  if (page === 'chat') loadDocuments()
  if (page === 'chat') loadChatHistory()
  if (page === 'dashboard') loadDashboardData().catch((error) => {
    const note = document.querySelector('#metric-documents-note')
    if (note) note.textContent = error.message
  })
  if (page === 'settings') loadSettingsData()
}

function createThinkingPanel() {
  const labels = [
    'Understanding your question...',
    'Checking the selected files...',
    'Putting together a clear answer...',
  ]
  const details = document.createElement('details')
  details.className = 'thinking-accordion'
  details.open = true
  const steps = labels
    .map((label, index) => `
      <li class="step-item" data-step="${index}">
        <span class="step-mark">schedule</span>
        <span>${label}</span>
      </li>
    `)
    .join('')
  details.innerHTML = `
    <summary class="thinking-trigger">
      <span class="thinking-spinner"></span>
      <span class="thinking-label">Thinking...</span>
    </summary>
    <ol class="thinking-steps">${steps}</ol>
  `

  function update(activeIndex) {
    details.querySelectorAll('.step-item').forEach((step, index) => {
      const complete = index < activeIndex
      step.classList.toggle('done', complete)
      step.classList.toggle('active', index === activeIndex)
      const mark = complete
        ? 'check'
        : index === activeIndex
          ? 'arrow_right'
          : 'schedule'
      step.querySelector('.step-mark').textContent = mark
    })
  }

  function finish(activeIndex, seconds, failed = false) {
    update(failed ? activeIndex : 2)
    if (!failed) {
      details.querySelectorAll('.step-item').forEach((step) => {
        step.classList.remove('active')
        step.classList.add('done')
        step.querySelector('.step-mark').textContent = 'check'
      })
    }
    const current = details.querySelector(`[data-step="${activeIndex}"]`)
    if (current && failed) {
      current.classList.remove('active')
      current.classList.toggle('done', !failed)
      if (failed) current.classList.add('failed')
      current.querySelector('.step-mark').textContent = failed ? 'error' : 'check'
    }
    details.querySelector('.thinking-spinner').hidden = true
    details.querySelector('.thinking-label').textContent =
      `Thinking process (${seconds.toFixed(1)}s)`
    details.open = false
  }

  return { element: details, update, finish }
}

async function readUploadResponse(response) {
  const body = await response.text()
  let data = null
  if (body.trim()) {
    try {
      data = JSON.parse(body)
    } catch {
      if (!response.ok) throw new Error(body.slice(0, 240))
      throw new Error(`The server returned an invalid response (HTTP ${response.status}).`)
    }
  }
  if (!response.ok) {
    throw new Error(data?.detail || `Upload failed (HTTP ${response.status}).`)
  }
  if (!data) throw new Error(`The server returned an empty response (HTTP ${response.status}).`)
  return data
}
function createUploadProgress(file) {
  const message = document.createElement('div')
  message.className = 'upload-composer-card'
  message.innerHTML = `
    <div class="upload-card">
      <div class="upload-file"><strong></strong><small></small></div>
      <div class="upload-progress"><span></span></div>
      <div class="upload-status">
        <span class="pulse-dot"></span>
        <span></span>
      </div>
    </div>
  `
  message.querySelector('.upload-file strong').textContent = file.name
  message.querySelector('.upload-file small').textContent =
    `${Math.ceil(file.size / 1024)} KB`
  message.querySelector('.upload-status span:last-child').textContent = 'Adding to your files…'
  return message
}

function updateUploadProgress(message, activeIndex) {
  message.querySelector('.upload-progress span').style.width = `${Math.min(35 + activeIndex * 25, 85)}%`
}

function finishUploadProgress(message, detail) {
  message.classList.add('upload-complete')
  message.querySelector('.upload-progress span').style.width = '100%'
  message.querySelector('.upload-status').innerHTML =
    '<span class="material-symbols-outlined">check_circle</span><span></span>'
  message.querySelector('.upload-status span:last-child').textContent = `Ready · ${detail}`
}

function failUploadProgress(message, error) {
  message.classList.add('upload-failed')
  message.querySelector('.upload-status').innerHTML =
    '<span class="material-symbols-outlined">error</span><span></span>'
  message.querySelector('.upload-status span:last-child').textContent = `Upload failed · ${error}`
}

function appendInlineText(parent, text) {
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)/g
  let offset = 0
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(offset, match.index)))
    const marker = match[0]
    const element = document.createElement(
      marker.startsWith('**') ? 'strong' : marker.startsWith('`') ? 'code' : 'em',
    )
    element.textContent = marker.startsWith('**') ? marker.slice(2, -2) : marker.slice(1, -1)
    parent.append(element)
    offset = match.index + marker.length
  }
  parent.append(document.createTextNode(text.slice(offset)))
}

function renderAnswer(content, root) {
  const lines = String(content || '').split(/\r?\n/)
  let list = null
  let inCode = false
  let codeLines = []
  const closeList = () => { list = null }

  for (const line of lines) {
    if (line.trim().startsWith('```')) {
      closeList()
      if (inCode) {
        const code = document.createElement('code')
        code.textContent = codeLines.join('\n')
        const pre = document.createElement('pre')
        pre.append(code)
        root.append(pre)
        codeLines = []
      }
      inCode = !inCode
      continue
    }
    if (inCode) {
      codeLines.push(line)
      continue
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/)
    const bullet = line.match(/^\s*[-*]\s+(.+)$/)
    const numbered = line.match(/^\s*\d+[.)]\s+(.+)$/)
    if (heading) {
      closeList()
      const title = document.createElement(`h${Math.min(heading[1].length + 2, 4)}`)
      appendInlineText(title, heading[2])
      root.append(title)
    } else if (bullet || numbered) {
      const kind = bullet ? 'ul' : 'ol'
      if (!list || list.tagName.toLowerCase() !== kind) {
        closeList()
        list = document.createElement(kind)
        root.append(list)
      }
      const item = document.createElement('li')
      appendInlineText(item, (bullet || numbered)[1])
      list.append(item)
    } else if (line.trim()) {
      closeList()
      const paragraph = document.createElement('p')
      appendInlineText(paragraph, line)
      root.append(paragraph)
    } else {
      closeList()
    }
  }
  if (inCode && codeLines.length) {
    const pre = document.createElement('pre')
    const code = document.createElement('code')
    code.textContent = codeLines.join('\n')
    pre.append(code)
    root.append(pre)
  }
}

function appendChatTurn(questionText, answerText, sources = [], turnId = '') {
  const messages = document.querySelector('#messages')
  if (!messages) return
  document.querySelector('.welcome-message')?.remove()
  const user = document.createElement('div')
  user.className = 'message you'
  user.dataset.turnId = turnId
  const userBody = document.createElement('div')
  const question = document.createElement('p')
  question.textContent = questionText
  const actions = document.createElement('div')
  actions.className = 'message-actions'
  actions.innerHTML =
    '<button type="button" data-edit-message title="Edit prompt">edit</button>' +
    '<button type="button" data-copy-message title="Copy prompt">content_copy</button>'
  userBody.append(question, actions)
  user.append(userBody)

  const bot = document.createElement('div')
  bot.className = 'message bot'
  bot.innerHTML =
    `<span class="icon-box">${icon('auto_awesome')}</span>` +
    '<div class="message-body"><small>DevCompass</small></div>'
  const answer = document.createElement('div')
  answer.className = 'bot-answer'
  renderAnswer(answerText, answer)
  if (sources.length) {
    const source = document.createElement('small')
    source.className = 'chat-sources'
    source.textContent = `Sources: ${sources.join(', ')}`
    answer.append(source)
  }
  bot.querySelector('.message-body').append(answer)
  messages.append(user, bot)
  messages.scrollTop = messages.scrollHeight
}

async function ensureChatSession() {
  if (!currentUser || activeChatId) return
  const response = await apiFetch('/chat/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: 'New chat' }),
  })
  if (response.status === 401) {
    await switchToGuestMode()
    return
  }
  const data = await response.json()
  if (!response.ok) throw new Error(data.detail || 'Could not start a new chat.')
  activeChatId = data.id
  editingTurn = null
  const input = document.querySelector('#chat-input')
  if (input) input.value = ''
  const editIndicator = document.querySelector('#edit-indicator')
  if (editIndicator) editIndicator.hidden = true
  const title = document.querySelector('#chat-title')
  if (title) title.textContent = data.title
  await loadChatHistory()
}

async function startNewChat() {
  activeChatId = null
  editingTurn = null
  const input = document.querySelector('#chat-input')
  if (input) input.value = ''
  const editIndicator = document.querySelector('#edit-indicator')
  if (editIndicator) editIndicator.hidden = true
  const messages = document.querySelector('#messages')
  if (messages) {
    messages.innerHTML = `
      <div class="welcome-message">
        <span class="icon-box">${icon('auto_awesome')}</span>
        <h2>What’s on your mind?</h2>
        <p>Ask a question, or use @ to bring a file into the conversation.</p>
      </div>
    `
  }
  const title = document.querySelector('#chat-title')
  if (title) title.textContent = 'New chat'
  if (currentUser) await ensureChatSession()
}

async function openChatSession(chatId) {
  const response = await apiFetch(`/chat/session/${encodeURIComponent(chatId)}`)
  const data = await response.json()
  if (!response.ok) throw new Error(data.detail || 'Could not open this chat.')
  activeChatId = data.id
  editingTurn = null
  const input = document.querySelector('#chat-input')
  if (input) input.value = ''
  const editIndicator = document.querySelector('#edit-indicator')
  if (editIndicator) editIndicator.hidden = true
  const title = document.querySelector('#chat-title')
  if (title) title.textContent = data.title
  const messages = document.querySelector('#messages')
  messages.replaceChildren()
  if (!data.turns.length) {
    messages.innerHTML = '<div class="welcome-message"><h2>Pick up where you left off</h2><p>Your next message will continue this chat.</p></div>'
  } else {
    data.turns.forEach((turn) => appendChatTurn(turn.question, turn.answer, turn.sources || [], turn.id))
  }
  document.querySelector('.library')?.classList.remove('library-open')
}

async function sendChat() {
  const input = document.querySelector('#chat-input')
  const text = input.value.trim()
  if (!text) return

  const messages = document.querySelector('#messages')
  let questionRow = editingTurn?.row || null
  if (!questionRow) {
    questionRow = document.createElement('div')
    questionRow.className = 'message you'
    const questionBody = document.createElement('div')
    const question = document.createElement('p')
    question.textContent = text
    const actions = document.createElement('div')
    actions.className = 'message-actions'
    actions.innerHTML =
      '<button type="button" data-edit-message title="Edit prompt">edit</button>' +
      '<button type="button" data-copy-message title="Copy prompt">content_copy</button>'
    questionBody.append(question, actions)
    questionRow.append(questionBody)
    messages.append(questionRow)
  }
  let bot = questionRow?.nextElementSibling || null
  if (!bot || !bot.classList.contains('bot')) {
    bot = document.createElement('div')
    bot.className = 'message bot'
    bot.innerHTML =
      `<span class="icon-box">${icon('auto_awesome')}</span>` +
      '<div class="message-body"><small>DevCompass</small></div>'
    messages.append(bot)
  } else {
    bot.querySelector('.bot-answer')?.remove()
    bot.querySelector('.thinking-accordion')?.remove()
  }
  const botBody = bot.querySelector('.message-body')
  const thinking = createThinkingPanel()
  const answer = document.createElement('div')
  answer.className = 'bot-answer'
  answer.hidden = true
  botBody.append(thinking.element, answer)
  input.value = ''
  document.querySelector('#mention-picker').hidden = true
  messages.scrollTop = messages.scrollHeight

  const startedAt = performance.now()
  let activeStep = 0
  thinking.update(activeStep)
  const progressTimer = setInterval(() => {
    activeStep = Math.min(Math.floor((performance.now() - startedAt) / 700), 2)
    thinking.update(activeStep)
  }, 250)

  try {
    let response
    if (editingTurn?.id && activeChatId) {
      response = await apiFetch(
        `/chat/session/${encodeURIComponent(activeChatId)}/turn/${encodeURIComponent(editingTurn.id)}`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question: text }),
        },
      )
      if (response.status === 401) {
        await switchToGuestMode()
        response = await apiFetch('/ask', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question: text }),
        })
      }
    } else {
      await ensureChatSession()
      await ensureGuestSession()
      const payload = { question: text }
      if (activeChatId) payload.chat_id = activeChatId
      if (activeDocument?.documentId) payload.document_id = activeDocument.documentId
      response = await apiFetch('/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (response.status === 401 && payload.chat_id) {
        await switchToGuestMode()
        delete payload.chat_id
        delete payload.document_id
        response = await apiFetch('/ask', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        })
      }
    }
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || 'RAG request failed')

    const answerText = data.answer || 'I could not find an answer this time.'
    renderAnswer(answerText, answer)
    if (data.sources?.length) {
      const source = document.createElement('small')
      source.className = 'chat-sources'
      source.textContent = `Sources: ${data.sources.join(', ')}`
      answer.append(source)
    }
    questionRow.querySelector('p').textContent = text
    if (data.turn_id) questionRow.dataset.turnId = data.turn_id
    editingTurn = null
    const editIndicator = document.querySelector('#edit-indicator')
    if (editIndicator) editIndicator.hidden = true
    await loadChatHistory()
    thinking.finish(activeStep, (performance.now() - startedAt) / 1000)
    answer.hidden = false
  } catch (error) {
    if (editingTurn?.row) editingTurn.row.querySelector('p').textContent = editingTurn.original
    const errorText = document.createElement('p')
    errorText.className = 'chat-error'
    errorText.textContent = `I couldn't finish that reply. ${error.message}`
    answer.append(errorText)
    thinking.finish(activeStep, (performance.now() - startedAt) / 1000, true)
    answer.hidden = false
  } finally {
    clearInterval(progressTimer)
    messages.scrollTop = messages.scrollHeight
  }
}

document.addEventListener('click', async (event) => {
  const passwordToggle = event.target.closest('[data-toggle-password]')
  if (passwordToggle) {
    const passwordInput = document.getElementById(passwordToggle.dataset.togglePassword)
    if (passwordInput) {
      const isVisible = passwordInput.type === 'password'
      passwordInput.type = isVisible ? 'text' : 'password'
      passwordToggle.textContent = isVisible ? 'Hide' : 'Show'
      passwordToggle.setAttribute('aria-pressed', String(isVisible))
      passwordToggle.setAttribute(
        'aria-label',
        `${isVisible ? 'Hide' : 'Show'} ${passwordInput.id === 'account-confirm' ? 'confirmation password' : 'password'}`,
      )
    }
  }
  const route = event.target.closest('[data-route]')
  if (route) go(route.dataset.route)
  if (event.target.closest('[data-send]')) sendChat()
  if (event.target.closest('[data-cancel-edit]')) {
    editingTurn = null
    const editIndicator = document.querySelector('#edit-indicator')
    if (editIndicator) editIndicator.hidden = true
    document.querySelector('#chat-input').value = ''
  }
  if (event.target.closest('[data-source]')) {
    document.querySelector('#source').classList.toggle('open')
  }
  const suggestion = event.target.closest('[data-suggestion]')
  if (suggestion) document.querySelector('#chat-input').value = suggestion.dataset.suggestion
  if (event.target.closest('[data-new-chat]')) {
    startNewChat().catch((error) => window.alert(error.message))
  }
  const historyItem = event.target.closest('.history-item[data-chat-id]')
  if (historyItem && !event.target.closest('[data-rename-chat], [data-delete-chat]')) {
    openChatSession(historyItem.dataset.chatId).catch((error) => window.alert(error.message))
  }
  const renameButton = event.target.closest('[data-rename-chat]')
  if (renameButton) {
    const chatId = renameButton.closest('.history-row').querySelector('[data-chat-id]').dataset.chatId
    const existing = chatHistoryEntries.find((entry) => entry._id === chatId)
    const title = window.prompt('Give this chat a name', existing?.title || '')
    if (title?.trim()) {
      const response = await apiFetch(`/chat/session/${encodeURIComponent(chatId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: title.trim() }),
      })
      if (!response.ok) window.alert('Could not rename this chat.')
      else {
        if (activeChatId === chatId) document.querySelector('#chat-title').textContent = title.trim()
        await loadChatHistory()
      }
    }
  }
  const deleteButton = event.target.closest('[data-delete-chat]')
  if (deleteButton) {
    const row = deleteButton.closest('.history-row')
    const chatId = row.querySelector('[data-chat-id]').dataset.chatId
    if (window.confirm('Delete this chat and its messages? This cannot be undone.')) {
      const response = await apiFetch(`/chat/session/${encodeURIComponent(chatId)}`, { method: 'DELETE' })
      if (!response.ok) window.alert('Could not delete this chat.')
      else {
        if (activeChatId === chatId) await startNewChat()
        await loadChatHistory()
      }
    }
  }
  const editButton = event.target.closest('[data-edit-message]')
  if (editButton) {
    const row = editButton.closest('.message.you')
    const question = row.querySelector('p').textContent
    editingTurn = { row, id: row.dataset.turnId, original: question }
    const editIndicator = document.querySelector('#edit-indicator')
    if (editIndicator) editIndicator.hidden = false
    const input = document.querySelector('#chat-input')
    input.value = question
    input.focus()
    input.setSelectionRange(input.value.length, input.value.length)
  }
  const copyButton = event.target.closest('[data-copy-message]')
  if (copyButton) {
    const question = copyButton.closest('.message.you').querySelector('p').textContent
    try {
      await navigator.clipboard.writeText(question)
      copyButton.textContent = 'check'
      window.setTimeout(() => { copyButton.textContent = 'content_copy' }, 1200)
    } catch {
      window.alert('Could not copy this prompt. Check your browser clipboard permissions.')
    }
  }
  const mentionFile = event.target.closest('[data-mention-file]')
  if (mentionFile) {
    const file = availableDocuments.find((item) => item.document_id === mentionFile.dataset.mentionFile)
    if (file) {
      activeDocument = { filename: file.filename, documentId: file.document_id }
      const input = document.querySelector('#chat-input')
      input.value = input.value.replace(/@[\w.-]*$/, `@${file.filename} `)
      input.focus()
      document.querySelector('#mention-picker').hidden = true
      syncActiveDocumentUI()
    }
  }
  if (event.target.closest('[data-clear-document]')) {
    activeDocument = null
    syncActiveDocumentUI()
  }
  if (event.target.closest('[data-toggle-library]')) {
    document.querySelector('.library')?.classList.toggle('library-open')
  }
  if (event.target.closest('[data-refresh-dashboard]')) {
    loadDashboardData().catch(() => {
      const note = document.querySelector('#metric-documents-note')
      if (note) note.textContent = 'Local bridge unavailable'
    })
  }
  if (event.target.closest('[data-refresh-settings]')) loadSettingsData()
  if (event.target.closest('[data-logout]')) {
    try {
      const response = await apiFetch('/auth/logout', { method: 'POST' })
      if (!response.ok) throw new Error('Could not log out.')
      currentUser = null
      activeDocument = null
      activeChatId = null
      go('welcome')
      render()
    } catch (error) {
      window.alert(error.message)
    }
  }

})

document.addEventListener('submit', async (event) => {
  if (event.target.id === 'account-form') {
    event.preventDefault()
    const form = event.target
    const message = document.querySelector('#auth-message')
    const mode = form.dataset.authMode
    const payload = {
      email: document.querySelector('#account-email').value.trim(),
      password: document.querySelector('#account-password').value,
    }
    if (mode === 'signup') {
      payload.display_name = document.querySelector('#account-name').value.trim()
      const confirmation = document.querySelector('#account-confirm')
      if (confirmation && confirmation.value !== payload.password) {
        message.hidden = false
        message.textContent = 'The passwords do not match.'
        return
      }
    }
    try {
      const response = await apiFetch(`/auth/${mode}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Account request failed.')
      currentUser = data.user
      activeChatId = null
      applyTheme(currentUser.settings?.theme || 'light')
      go('dashboard')
      render()
    } catch (error) {
      message.hidden = false
      message.textContent = error.message
    }
  }

  if (event.target.id === 'profile-form') {
    event.preventDefault()
    const message = document.querySelector('#profile-message')
    const payload = {
      display_name: document.querySelector('#profile-display-name').value.trim(),
      theme: document.querySelector('#profile-theme').value,
      answer_style: document.querySelector('#profile-answer-style').value,
    }
    try {
      const response = await apiFetch('/profile', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Could not save profile.')
      currentUser = data
      applyTheme(data.settings.theme)
      const signedInName = document.querySelector('#signed-in-name')
      if (signedInName) signedInName.textContent = data.username
      const sidebarUsername = document.querySelector('#sidebar-username')
      if (sidebarUsername) sidebarUsername.textContent = data.username
      message.hidden = false
      message.textContent = 'Your profile and preferences are saved.'
    } catch (error) {
      message.hidden = false
      message.textContent = error.message
    }
  }

  if (event.target.id === 'delete-account-form') {
    event.preventDefault()
    const confirmed = window.confirm(
      'Delete your account, uploaded files, and saved chats permanently?',
    )
    if (!confirmed) return
    const message = document.querySelector('#delete-account-message')
    try {
      const response = await apiFetch('/profile', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          password: document.querySelector('#delete-account-password').value,
        }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Could not delete your account.')
      currentUser = null
      activeChatId = null
      activeDocument = null
      go('welcome')
      render()
    } catch (error) {
      message.hidden = false
      message.textContent = error.message
    }
  }
})

document.addEventListener('change', async (event) => {
  if (event.target.id === 'avatar-upload' && event.target.files[0]) {
    const form = new FormData()
    form.append('file', event.target.files[0])
    const message = document.querySelector('#profile-message')
    try {
      const response = await apiFetch('/profile/avatar', { method: 'POST', body: form })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Could not update your profile picture.')
      const avatar = document.querySelector('#profile-avatar')
      if (avatar) avatar.src = `${API_BASE}/profile/avatar?updated=${Date.now()}`
      if (currentUser) currentUser.has_avatar = true
      if (message) {
        message.hidden = false
        message.textContent = 'Profile picture updated.'
      }
    } catch (error) {
      if (message) {
        message.hidden = false
        message.textContent = error.message
      }
    }
    event.target.value = ''
    return
  }
  if (event.target.id !== 'file-upload' || !event.target.files[0]) return
  const file = event.target.files[0]
  const preview = document.querySelector('#attachment-preview')
  if (preview) {
    preview.hidden = false
    preview.replaceChildren()
    preview.insertAdjacentHTML('afterbegin', icon('description'))
    const previewName = document.createElement('span')
    previewName.textContent = file.name
    const previewSize = document.createElement('small')
    previewSize.textContent = `${Math.ceil(file.size / 1024)} KB`
    preview.append(previewName, previewSize)
  }
  const message = createUploadProgress(file)
  if (preview) {
    preview.classList.add('uploading')
    preview.replaceChildren(message)
    preview.hidden = false
  }
  let stageIndex = 0
  updateUploadProgress(message, stageIndex)
  const progressTimer = setInterval(() => {
    stageIndex = Math.min(stageIndex + 1, 2)
    updateUploadProgress(message, stageIndex)
  }, 900)

  const form = new FormData()
  form.append('file', file)
  try {
    if (file.size > 10 * 1024 * 1024) {
      throw new Error('Files must be 10 MB or smaller.')
    }
    await ensureGuestSession()
    const response = await apiFetch('/upload', { method: 'POST', body: form })
    const data = await readUploadResponse(response)
    activeDocument = {
      filename: data.filename,
      documentId: data.document_id || data.filename,
    }
    finishUploadProgress(message, 'available to your chats')
    await loadDocuments()
    syncActiveDocumentUI()
  } catch (error) {
    failUploadProgress(message, error.message)
  } finally {
    clearInterval(progressTimer)
    event.target.value = ''
  }
})

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    const picker = document.querySelector('#mention-picker')
    if (picker) picker.hidden = true
  }
  const isChatEnter =
    event.key === 'Enter'
    && event.target.id === 'chat-input'
    && !event.shiftKey
  if (!isChatEnter) return

  const firstFile = document.querySelector('#mention-picker:not([hidden]) [data-mention-file]')
  if (firstFile) {
    event.preventDefault()
    firstFile.click()
    return
  }
  event.preventDefault()
  sendChat()
})

document.addEventListener('input', (event) => {
  if (event.target.id !== 'chat-input') return
  const picker = document.querySelector('#mention-picker')
  const mention = event.target.value.slice(0, event.target.selectionStart).match(/@([\w.-]*)$/)
  if (!picker || !mention) {
    if (picker) picker.hidden = true
    return
  }
  const search = mention[1].toLowerCase()
  const matches = availableDocuments.filter((file) => file.filename.toLowerCase().includes(search))
  picker.replaceChildren()
  if (!matches.length) {
    const empty = document.createElement('p')
    empty.textContent = currentUser
      ? 'No matching files yet. Upload one from here.'
      : 'No matching files yet. Add a file to get started.'
    picker.append(empty)
  } else {
    matches.forEach((file) => {
      const button = document.createElement('button')
      button.type = 'button'
      button.dataset.mentionFile = file.document_id
      button.innerHTML = `${icon('description')}<span></span>`
      button.querySelector('span').textContent = file.filename
      picker.append(button)
    })
  }
  picker.hidden = false
})
async function initializeAuth() {
  try {
    const response = await apiFetch('/auth/me')
    if (response.ok) {
      const data = await response.json()
      currentUser = data.user
      if (currentUser) applyTheme(currentUser.settings?.theme || 'light')
    }
  } catch (error) {
    console.error('Could not restore the account session:', error)
  }
  render()
}

window.startDevCompass = () => {
  window.addEventListener('hashchange', render)
  initializeAuth()
}
