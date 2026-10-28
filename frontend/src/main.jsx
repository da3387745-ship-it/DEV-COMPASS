import React, { useLayoutEffect, useState } from 'react'
import { flushSync } from 'react-dom'
import { createRoot } from 'react-dom/client'
import '../styles.css'
import '../app.jsx'

function App() {
  const [markup, setMarkup] = useState('')

  useLayoutEffect(() => {
    window.renderDevCompassMarkup = (nextMarkup) => {
      flushSync(() => setMarkup(nextMarkup))
    }
    window.startDevCompass()
  }, [])

  return <div dangerouslySetInnerHTML={{ __html: markup }} />
}

createRoot(document.querySelector('#app')).render(<App />)
