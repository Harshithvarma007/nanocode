/// <reference types="vite/client" />
import type { NanoApi } from '@shared/host'

declare global {
  interface Window {
    nano?: NanoApi
  }
}
