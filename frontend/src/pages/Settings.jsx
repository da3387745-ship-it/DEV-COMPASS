export function Settings({ layout, icon }) {
  return layout(`
    <main class="container page-content">
      <div class="page-heading">
        <div>
          <span class="eyebrow">Your account</span>
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
              <span class="eyebrow">Account</span>
              <h2>Your profile</h2>
            </div>
          </div>
          <div class="avatar-editor">
            <img class="avatar profile-avatar" id="profile-avatar" src="logo.png" alt="Profile picture">
            <div>
              <strong>Profile picture</strong>
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
              <span class="eyebrow">Your library</span>
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
              <span class="eyebrow">Account control</span>
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
