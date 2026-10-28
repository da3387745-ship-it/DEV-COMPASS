function library({ icon, currentUser }) {
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

export function Chat({ layout, icon, currentUser }) {
  return layout(`
    <main class="chat-layout">
      ${library({ icon, currentUser })}
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
            <input class="file-input" id="file-upload" type="file" accept=".pdf,.txt,.md,.ipynb">
            <textarea
              id="chat-input"
              placeholder="Message DevCompass… use @ to choose a file"
              rows="2"
            ></textarea>
            <div class="composer-tools">
              <label class="attach-button" for="file-upload" title="Add a PDF, TXT, Markdown, or Jupyter notebook" aria-label="Add a file">
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
