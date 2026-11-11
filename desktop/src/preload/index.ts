// The only bridge between the renderer (untrusted web content) and the main process.
import { contextBridge, ipcRenderer, webUtils, type IpcRendererEvent } from 'electron'
import type { NanoApi } from '../shared/host'

function subscribe<T>(channel: string, cb: (value: T) => void): () => void {
  const listener = (_e: IpcRendererEvent, value: T) => cb(value)
  ipcRenderer.on(channel, listener)
  return () => ipcRenderer.removeListener(channel, listener)
}

const api: NanoApi = {
  context: () => ipcRenderer.invoke('app:context'),
  pickFolder: () => ipcRenderer.invoke('app:pickFolder'),
  openProject: (path) => ipcRenderer.invoke('app:openProject', path),
  recents: () => ipcRenderer.invoke('app:recents'),
  removeRecent: (path) => ipcRenderer.invoke('app:removeRecent', path),
  getSettings: () => ipcRenderer.invoke('settings:get'),
  setSettings: (patch) => ipcRenderer.invoke('settings:set', patch),
  setKey: (env, value) => ipcRenderer.invoke('settings:setKey', env, value),
  engineStart: () => ipcRenderer.invoke('engine:start'),
  engineRestart: () => ipcRenderer.invoke('engine:restart'),
  engineSend: (message) => ipcRenderer.send('engine:send', message),
  pickEngine: () => ipcRenderer.invoke('engine:pick'),
  onEngineMessage: (cb) => subscribe('engine:message', cb),
  onEngineState: (cb) => subscribe('engine:state', cb),
  onMenu: (cb) => subscribe('menu', cb),
  onProject: (cb) => subscribe('app:project', cb),
  notify: (title, body) => ipcRenderer.send('app:notify', title, body),
  setTheme: (theme) => ipcRenderer.send('app:setTheme', theme),
  setTitle: (title) => ipcRenderer.send('app:setTitle', title),
  pathForFile: (file) => webUtils.getPathForFile(file),
  reveal: (path) => ipcRenderer.send('app:reveal', path),
  openExternal: (url) => ipcRenderer.send('app:openExternal', url),
  openLogs: () => ipcRenderer.send('app:openLogs')
}

contextBridge.exposeInMainWorld('nano', api)
