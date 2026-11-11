// Renders resources/icon.svg to a 1024x1024 PNG with Electron's own Chromium (keeps the alpha
// channel, which QuickLook would fill with white). Run by scripts/make-icon.mjs:
//   electron scripts/render-icon.cjs <in.svg> <out.png>
const { app, BrowserWindow } = require('electron')
const fs = require('node:fs')

const [svgPath, outPath] = process.argv.slice(-2)
app.whenReady().then(async () => {
  const win = new BrowserWindow({ show: false, width: 200, height: 200 })
  await win.loadURL('data:text/html,<html><body></body></html>')
  const svg = 'data:image/svg+xml;base64,' + fs.readFileSync(svgPath).toString('base64')
  const dataUrl = await win.webContents.executeJavaScript(`new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => {
      const c = document.createElement('canvas')
      c.width = c.height = 1024
      c.getContext('2d').drawImage(img, 0, 0, 1024, 1024)
      resolve(c.toDataURL('image/png'))
    }
    img.onerror = () => reject(new Error('could not load the SVG'))
    img.src = ${JSON.stringify(svg)}
  })`)
  fs.writeFileSync(outPath, Buffer.from(dataUrl.split(',')[1], 'base64'))
  app.quit()
})
