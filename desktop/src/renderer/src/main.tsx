import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { boot } from './lib/actions'
import { isElectron } from './lib/host'
import './styles.css'

if (isElectron) document.documentElement.classList.add('electron')

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>
)

void boot()
