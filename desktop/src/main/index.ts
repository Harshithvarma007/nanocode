// The Electron main process: windows, native menus, settings, and one engine process per project
// window. It never runs agent logic: it relays JSON lines between the renderer and `nanocode serve`.
import { app, BrowserWindow, dialog, ipcMain, Menu, nativeTheme, Notification, shell, type MenuItemConstructorOptions } from 'electron'
import { execFile } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import type { AppSettings, EngineState, MenuAction, Theme, WindowContext } from '../shared/host'
import { discoverEngine, enginePath, EngineProcess } from './engine'
import { log, logsDir } from './log'
import * as store from './store'

app.setName('nanocode')

interface ProjectWindow {
  win: BrowserWindow
  project: string | null
  engine: EngineProcess | null
}

const windows = new Map<number, ProjectWindow>()

function byEvent(e: Electron.IpcMainInvokeEvent | Electron.IpcMainEvent): ProjectWindow | undefined {
  return windows.get(e.sender.id)
}

function focusedProjectWindow(): ProjectWindow | undefined {
  const win = BrowserWindow.getFocusedWindow()
  return win ? windows.get(win.webContents.id) : undefined
}

// ------------------------------------------------------------------ windows

function createWindow(project: string | null): ProjectWindow {
  const offset = BrowserWindow.getFocusedWindow()?.getPosition()
  const win = new BrowserWindow({
    width: project ? 1320 : 980,
    height: project ? 860 : 660,
    minWidth: 760,
    minHeight: 520,
    ...(offset ? { x: offset[0] + 28, y: offset[1] + 28 } : {}),
    show: false,
    title: project ? path.basename(project) : 'nanocode',
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 18, y: 18 },
    vibrancy: 'sidebar',
    visualEffectState: 'followWindow',
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, '../preload/index.js'),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: true
    }
  })
  const pw: ProjectWindow = { win, project, engine: null }
  const id = win.webContents.id
  windows.set(id, pw)
  if (project) store.addRecent(project)

  win.once('ready-to-show', () => {
    win.show()
    snapshot(win)
  })
  win.on('closed', () => {
    windows.delete(id)
    void pw.engine?.stop()
    rebuildMenu()
  })
  // Links in the transcript open in the browser, never inside the app.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/.test(url)) void shell.openExternal(url)
    return { action: 'deny' }
  })
  win.webContents.on('will-navigate', (event, url) => {
    if (url !== win.webContents.getURL()) {
      event.preventDefault()
      if (/^https?:/.test(url)) void shell.openExternal(url)
    }
  })

  if (process.env.ELECTRON_RENDERER_URL) void win.loadURL(process.env.ELECTRON_RENDERER_URL)
  else void win.loadFile(path.join(__dirname, '../renderer/index.html'))
  rebuildMenu()
  return pw
}

/** Test hook: NANOCODE_SNAPSHOT=/path/shot.png captures this window (with its native chrome) a few
 *  seconds after it opens, using macOS `screencapture -l <window id>`. */
function snapshot(win: BrowserWindow): void {
  const target = process.env.NANOCODE_SNAPSHOT
  if (!target || process.platform !== 'darwin') return
  const delay = Number(process.env.NANOCODE_SNAPSHOT_DELAY || 8000)
  setTimeout(() => {
    if (win.isDestroyed()) return
    const id = win.getMediaSourceId().split(':')[1]
    execFile('screencapture', ['-x', '-o', `-l${id}`, target], (err) => log(err ? `snapshot failed: ${err.message}` : `snapshot saved to ${target}`))
  }, delay)
}

function sendMenu(action: MenuAction): void {
  focusedProjectWindow()?.win.webContents.send('menu', action)
}

/** Open a project: focus its window if it's already open, reuse an empty welcome window, or make a new one. */
function openProject(dir: string, prefer?: ProjectWindow, notify = true): 'bound' | 'opened' | 'focused' {
  for (const pw of windows.values()) {
    if (pw.project === dir) {
      if (pw.win.isMinimized()) pw.win.restore()
      pw.win.focus()
      return 'focused'
    }
  }
  const target = prefer && !prefer.project ? prefer : undefined
  if (target) {
    target.project = dir
    store.addRecent(dir)
    target.win.setTitle(path.basename(dir))
    if (notify) target.win.webContents.send('app:project', dir)
    const [w, h] = target.win.getSize()
    if (w < 1200) {
      const [x, y] = target.win.getPosition()
      target.win.setBounds({ x: Math.max(0, x - 170), y: Math.max(0, y - 100), width: 1320, height: Math.max(h, 860) }, true)
    }
    rebuildMenu()
    return 'bound'
  }
  createWindow(dir)
  return 'opened'
}

async function pickFolder(parent?: BrowserWindow): Promise<string | null> {
  const options: Electron.OpenDialogOptions = {
    title: 'Open a project folder',
    buttonLabel: 'Open',
    properties: ['openDirectory', 'createDirectory']
  }
  const result = parent ? await dialog.showOpenDialog(parent, options) : await dialog.showOpenDialog(options)
  return result.canceled || !result.filePaths[0] ? null : result.filePaths[0]
}

async function openFolderFlow(): Promise<void> {
  const current = focusedProjectWindow()
  const dir = await pickFolder(current?.win)
  if (dir) openProject(dir, current)
}

// ------------------------------------------------------------------ engine

function sendState(pw: ProjectWindow, state: EngineState): void {
  if (!pw.win.isDestroyed()) pw.win.webContents.send('engine:state', state)
}

async function startEngine(pw: ProjectWindow): Promise<void> {
  if (!pw.project) return
  if (pw.engine) {
    const old = pw.engine
    pw.engine = null
    await old.stop()
  }
  const settings = store.getSettings()
  const { found, searched } = await discoverEngine(settings)
  if (!found) {
    log(`engine not found; searched: ${searched.join(' | ')}`)
    sendState(pw, { state: 'not-found', searched })
    return
  }
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    PATH: await enginePath(),
    PYTHONUNBUFFERED: '1',
    PYTHONIOENCODING: 'utf-8',
    NANOCODE_EFFORT: settings.effort,
    ...store.keyEnv()
  }
  delete env.ELECTRON_RUN_AS_NODE
  delete env.ELECTRON_RENDERER_URL
  if (settings.ollamaHost.trim()) env.OLLAMA_HOST = settings.ollamaHost.trim()
  if (!settings.think) env.NANOCODE_THINK = '0'
  const engine = new EngineProcess({
    cwd: pw.project,
    cmd: found,
    env,
    model: settings.defaultModel.trim() || undefined,
    onMessage: (message) => {
      if (!pw.win.isDestroyed() && pw.engine === engine) pw.win.webContents.send('engine:message', message)
    },
    onState: (state) => {
      if (pw.engine === engine) sendState(pw, state)
    }
  })
  pw.engine = engine
  engine.start()
}

async function restartAllEngines(reason: string): Promise<void> {
  log(`restarting engines: ${reason}`)
  await Promise.all(
    [...windows.values()]
      .filter((pw) => pw.project && pw.engine)
      .map(async (pw) => {
        sendState(pw, { state: 'restarting', reason })
        await startEngine(pw)
      })
  )
}

// ------------------------------------------------------------------ IPC

ipcMain.handle('app:context', (e): WindowContext => {
  const pw = byEvent(e)
  return { project: pw?.project ?? null, version: app.getVersion(), isPackaged: app.isPackaged, platform: process.platform, home: os.homedir() }
})
ipcMain.handle('app:pickFolder', (e) => pickFolder(byEvent(e)?.win))
ipcMain.handle('app:openProject', (e, dir: string) => openProject(dir, byEvent(e), false))
ipcMain.handle('app:recents', () => store.recents())
ipcMain.handle('app:removeRecent', (_e, dir: string) => {
  const list = store.removeRecent(dir)
  rebuildMenu()
  return list
})
ipcMain.handle('settings:get', () => store.settingsState())
ipcMain.handle('settings:set', async (_e, patch: Partial<AppSettings>) => {
  const before = store.getSettings()
  const after = store.setSettings(patch)
  if (patch.theme) nativeTheme.themeSource = patch.theme
  if (before.enginePath !== after.enginePath || before.ollamaHost !== after.ollamaHost) {
    void restartAllEngines(before.enginePath !== after.enginePath ? 'Engine path changed' : 'Ollama host changed')
  }
  return store.settingsState()
})
ipcMain.handle('settings:setKey', async (_e, env: string, value: string | null) => {
  store.setKey(env, value)
  void restartAllEngines('API keys changed')
  return store.settingsState()
})
ipcMain.handle('engine:start', async (e) => {
  const pw = byEvent(e)
  if (pw) await startEngine(pw)
})
ipcMain.handle('engine:restart', async (e) => {
  const pw = byEvent(e)
  if (pw) await startEngine(pw)
})
ipcMain.on('engine:send', (e, message: unknown) => byEvent(e)?.engine?.send(message))
ipcMain.handle('engine:pick', async (e) => {
  const pw = byEvent(e)
  const options: Electron.OpenDialogOptions = {
    title: 'Choose the nanocode engine',
    message: 'Pick a nanocode executable, a Python interpreter that has nanocode installed, or a nanocode checkout folder.',
    buttonLabel: 'Use This Engine',
    properties: ['openFile', 'openDirectory', 'showHiddenFiles', 'treatPackageAsDirectory']
  }
  const result = pw ? await dialog.showOpenDialog(pw.win, options) : await dialog.showOpenDialog(options)
  const picked = result.canceled ? null : result.filePaths[0] ?? null
  if (picked) {
    store.setSettings({ enginePath: picked })
    if (pw) await startEngine(pw)
  }
  return picked
})
ipcMain.on('app:notify', (e, title: string, body: string) => {
  const pw = byEvent(e)
  if (!Notification.isSupported()) return
  const n = new Notification({ title, body, silent: false })
  n.on('click', () => {
    if (pw && !pw.win.isDestroyed()) {
      pw.win.show()
      pw.win.focus()
    }
  })
  n.show()
})
ipcMain.on('app:setTheme', (_e, theme: Theme) => {
  nativeTheme.themeSource = theme
})
ipcMain.on('app:setTitle', (e, title: string) => {
  const pw = byEvent(e)
  if (pw && !pw.win.isDestroyed()) pw.win.setTitle(title)
})
ipcMain.on('app:reveal', (_e, p: string) => shell.showItemInFolder(p))
ipcMain.on('app:openExternal', (_e, url: string) => {
  if (/^https?:/.test(url)) void shell.openExternal(url)
})
ipcMain.on('app:openLogs', () => void shell.openPath(logsDir()))

// ------------------------------------------------------------------ menus

function rebuildMenu(): void {
  const recents = store.recents()
  const isMac = process.platform === 'darwin'
  const template: MenuItemConstructorOptions[] = [
    ...(isMac
      ? [
          {
            label: 'nanocode',
            submenu: [
              { role: 'about' },
              { type: 'separator' },
              { label: 'Settings…', accelerator: 'CmdOrCtrl+,', click: () => sendMenu('settings') },
              { type: 'separator' },
              { role: 'services' },
              { type: 'separator' },
              { role: 'hide' },
              { role: 'hideOthers' },
              { role: 'unhide' },
              { type: 'separator' },
              { role: 'quit' }
            ]
          } satisfies MenuItemConstructorOptions
        ]
      : []),
    {
      label: 'File',
      submenu: [
        { label: 'New Session', accelerator: 'CmdOrCtrl+N', click: () => sendMenu('new-session') },
        { label: 'New Window', accelerator: 'CmdOrCtrl+Shift+N', click: () => createWindow(null) },
        { type: 'separator' },
        { label: 'Open Folder…', accelerator: 'CmdOrCtrl+O', click: () => void openFolderFlow() },
        {
          label: 'Open Recent',
          submenu: [
            ...recents.map((r) => ({ label: r.name, sublabel: r.path, click: () => openProject(r.path, focusedProjectWindow()) })),
            ...(recents.length ? [{ type: 'separator' as const }] : []),
            {
              label: 'Clear Menu',
              enabled: recents.length > 0,
              click: () => {
                for (const r of store.recents()) store.removeRecent(r.path)
                app.clearRecentDocuments()
                rebuildMenu()
              }
            }
          ]
        },
        { type: 'separator' },
        { role: 'close' }
      ]
    },
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        { label: 'Toggle Sidebar', accelerator: 'CmdOrCtrl+\\', click: () => sendMenu('toggle-sidebar') },
        { label: 'Toggle Inspector', accelerator: 'Alt+CmdOrCtrl+I', click: () => sendMenu('toggle-inspector') },
        { type: 'separator' },
        { label: 'Focus Message Box', accelerator: 'CmdOrCtrl+L', click: () => sendMenu('focus-composer') },
        { label: 'Stop Response', accelerator: 'CmdOrCtrl+.', click: () => sendMenu('stop') },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        ...(app.isPackaged ? [] : [{ role: 'reload' as const }, { role: 'toggleDevTools' as const }, { type: 'separator' as const }]),
        { role: 'togglefullscreen' }
      ]
    },
    { role: 'windowMenu' },
    {
      role: 'help',
      submenu: [
        { label: 'Keyboard Shortcuts', accelerator: 'CmdOrCtrl+/', click: () => sendMenu('shortcuts') },
        { label: 'Open Logs Folder', click: () => void shell.openPath(logsDir()) },
        { type: 'separator' },
        { label: 'Design reference: OpenCode', click: () => void shell.openExternal('https://github.com/anomalyco/opencode') }
      ]
    }
  ]
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

// ------------------------------------------------------------------ lifecycle

let quitting = false

// Folders can be opened from outside: `open -a nanocode ~/code/shop`, dropping a folder on the Dock
// icon (open-file), or `nanocode.app/Contents/MacOS/nanocode ~/code/shop`.
const pendingOpen: string[] = []

function isDirectory(p: string): boolean {
  try {
    return fs.statSync(p).isDirectory()
  } catch {
    return false
  }
}

function folderArgs(argv: string[]): string[] {
  return argv
    .slice(app.isPackaged ? 1 : 2)
    .filter((a) => !a.startsWith('-') && isDirectory(a) && !a.endsWith('.app'))
    .map((a) => path.resolve(a))
}

app.on('open-file', (event, file) => {
  if (!isDirectory(file)) return
  event.preventDefault()
  if (app.isReady()) openProject(file, focusedProjectWindow())
  else pendingOpen.push(file)
})

app.whenReady().then(() => {
  log(`nanocode ${app.getVersion()} starting (packaged: ${app.isPackaged}, electron ${process.versions.electron})`)
  nativeTheme.themeSource = store.getSettings().theme
  app.setAboutPanelOptions({
    applicationName: 'nanocode',
    applicationVersion: app.getVersion(),
    credits: 'A desktop app for the nanocode coding agent.\nDesign reference: OpenCode (MIT).',
    copyright: 'MIT licensed'
  })
  const toOpen = [...new Set([...pendingOpen, ...folderArgs(process.argv)])]
  if (toOpen.length) for (const dir of toOpen) openProject(dir)
  else createWindow(null)
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow(null)
  })
})

app.on('before-quit', (event) => {
  if (quitting) return
  const running = [...windows.values()].filter((pw) => pw.engine?.running)
  if (!running.length) return
  event.preventDefault()
  quitting = true
  log(`quitting: stopping ${running.length} engine(s)`)
  void Promise.all(running.map((pw) => pw.engine?.stop())).finally(() => app.quit())
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
