// Mount MandatOverlay.vue on document.body for the freeroam HTML overlay (not a HUD app / route).
import { createApp } from "vue"
import MandatOverlay from "./MandatOverlay.vue"

let host = null
let app = null

export function start() {
  if (app) return
  host = document.createElement("div")
  host.id = "beamng-earth-mandat-host"
  document.body.appendChild(host)
  app = createApp(MandatOverlay)
  app.mount(host)
}

export function stop() {
  if (!app) return
  try { app.unmount() } catch (e) { /* already gone */ }
  app = null
  if (host && host.parentNode) host.parentNode.removeChild(host)
  host = null
}
