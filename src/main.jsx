import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './styles.css'
import './currentcad-theme.css'
import './usability.css'

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
