<template>
  <!-- Polish "Mandat karny" (Odcinek C) over freeroam: mounted on document.body from index.js, not a HUD app. -->
  <div v-if="ticket" class="mandat-root" @keydown.esc.stop="dismiss">
    <div class="mandat-scrim" @click="dismiss"></div>
    <div class="mandat-wrap" @click.stop>
    <div class="mandat" role="dialog" aria-label="Mandat karny">
      <div class="mandat-main">
        <div class="row head">
          <div class="head-left">
            <div class="title-line">
              <span class="title">MANDAT KARNY</span>
              <span class="serial">SERIA <b>{{ ticket.series }}</b> NUMER <b>{{ ticket.number }}</b></span>
            </div>
            <p class="legal">
              Na podstawie art. 15 § 1, art. 39 § 1, art. 54 § 1, art. 56 § 1, art. 57 § 1, art. 60 § 1,
              art. 63 § 2, art. 67 § 1, art. 73 § 1, art. 96 § 1, art. 97 § 1, art. 98 § 1 ustawy z dnia
              24 sierpnia 2001 r. — Kodeks postępowania w sprawach o wykroczenia (Dz. U. z 2021 r. poz. 457)
            </p>
          </div>
          <div class="stamp-box">
            <span>miejsce na pieczęć organu nakładającego mandat</span>
            <div class="stamp-fake">ITD / Policja<br/>BeamNG.Earth</div>
          </div>
        </div>

        <div class="row amount-row">
          <span class="lbl">zł</span>
          <span class="fill amount">{{ ticket.amount }}</span>
          <span class="lbl">słownie</span>
          <span class="fill words">{{ ticket.words }}</span>
        </div>

        <div class="row kind-row">
          <span class="kind" :class="{ on: ticket.kind === 'kredytowany' }">kredytowany</span>
          <span class="sep">/</span>
          <span class="kind" :class="{ on: ticket.kind === 'gotowkowy' }">gotówkowy</span>
          <span class="sep">/</span>
          <span class="kind on">zaoczny</span>
          <span class="hint">— zaznaczyć właściwy</span>
          <label class="card-pay">
            <span class="box"></span>
            płatność kartą lub innym instrumentem płatniczym
          </label>
        </div>

        <div class="field">
          <div class="line">{{ ticket.name }}</div>
          <div class="under">(imię, nazwisko, imię ojca)</div>
        </div>

        <div class="field pesel">
          <div class="pesel-boxes">
            <span v-for="i in 11" :key="'p' + i" class="cell"></span>
          </div>
          <div class="under">numer PESEL</div>
        </div>

        <div class="field">
          <div class="line muted">—</div>
          <div class="under">(nazwa i numer dokumentu, na podstawie którego ustalono tożsamość ukaranego)</div>
        </div>

        <div class="field">
          <div class="line muted">{{ placeLine }}</div>
          <div class="under">(adres miejsca zamieszkania lub pobytu, nr telefonu)</div>
        </div>

        <div class="offense">
          <div class="offense-lines">{{ ticket.description }}</div>
          <div class="points">
            <span>liczba punktów:</span>
            <span class="points-val">{{ ticket.points }}</span>
          </div>
        </div>

        <div class="footer">
          <div class="cell">
            <div class="line short">BNE-CAM</div>
            <div class="under">nr służbowy / identyfikacyjny</div>
            <div class="line short">fotoradar</div>
            <div class="under">podpis funkcjonariusza</div>
          </div>
          <div class="cell">
            <div class="date">data: {{ ticket.date }}</div>
            <div class="line short">{{ ticket.name }}</div>
            <div class="under">czytelny podpis osoby ukaranej</div>
          </div>
          <div class="cell stamp-day">
            <span>stempel dzienny</span>
          </div>
          <div class="cell pay">
            <div class="under">opłata</div>
            <div class="line short">{{ ticket.amount }} zł</div>
            <div class="under">podpis</div>
            <div class="line short"></div>
          </div>
        </div>
      </div>
      <div class="side">
        <span class="side-text">Odcinek C — dla osoby ukaranej</span>
      </div>
    </div>
    <button class="accept" type="button" @click="dismiss">Przyjmuję</button>
    </div>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref } from "vue"
import { useEvents } from "@/services/events"

const AUTO_MS = 14000
const ticket = ref(null)
const events = useEvents()
const placeLine = "—"
let timer = null

function clearTimer() {
  if (timer) { clearTimeout(timer); timer = null }
}

function dismiss() {
  clearTimer()
  ticket.value = null
}

function onMandat(data) {
  if (!data || typeof data !== "object") return
  ticket.value = data
  clearTimer()
  timer = setTimeout(dismiss, AUTO_MS)
}

function onKey(e) {
  if (e.key === "Escape" && ticket.value) {
    e.preventDefault()
    e.stopPropagation()
    dismiss()
  }
}

onMounted(() => {
  events.on("BeamNGEarthMandat", onMandat)
  window.addEventListener("keydown", onKey, true)
})

onUnmounted(() => {
  events.off("BeamNGEarthMandat", onMandat)
  window.removeEventListener("keydown", onKey, true)
  clearTimer()
})
</script>

<style scoped>
.mandat-root {
  position: fixed;
  inset: 0;
  z-index: 11000;
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
  padding: 1.5em;
  box-sizing: border-box;
}
.mandat-scrim {
  position: absolute;
  inset: 0;
  background: rgba(8, 10, 14, 0.35);
  pointer-events: auto;
}
.mandat-wrap {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.55em;
  width: min(52em, 96vw);
  max-height: 92vh;
  pointer-events: auto;
}
.mandat {
  position: relative;
  display: flex;
  flex-direction: row;
  width: 100%;
  max-height: calc(92vh - 2.5em);
  background: #f7f5f0;
  color: #111;
  border: 1px solid #222;
  box-shadow: 0 0.6em 2em rgba(0, 0, 0, 0.45);
  font-family: "Segoe UI", Tahoma, Arial, sans-serif;
  font-size: 0.85em;
  line-height: 1.25;
  overflow: hidden;
}
.mandat-main {
  flex: 1 1 auto;
  padding: 0.7em 0.85em 0.6em;
  overflow: auto;
  min-width: 0;
}
.side {
  flex: 0 0 1.6em;
  background: #a9d18e;
  border-left: 1px solid #222;
  display: flex;
  align-items: center;
  justify-content: center;
  writing-mode: vertical-rl;
  transform: rotate(180deg);
}
.side-text {
  font-size: 0.72em;
  font-weight: 600;
  letter-spacing: 0.04em;
  white-space: nowrap;
}
.row { margin-bottom: 0.45em; }
.head { display: flex; gap: 0.6em; align-items: stretch; }
.head-left { flex: 1 1 auto; min-width: 0; }
.title-line {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.75em;
  margin-bottom: 0.25em;
}
.title {
  font-size: 1.55em;
  font-weight: 800;
  letter-spacing: 0.04em;
}
.serial { font-size: 0.95em; }
.serial b { letter-spacing: 0.06em; }
.legal {
  margin: 0;
  font-size: 0.62em;
  line-height: 1.2;
  color: #333;
}
.stamp-box {
  flex: 0 0 7.5em;
  border: 1px solid #222;
  padding: 0.25em;
  font-size: 0.58em;
  text-align: center;
  color: #444;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  min-height: 5.5em;
}
.stamp-fake {
  margin-top: 0.4em;
  font-size: 1.15em;
  font-weight: 700;
  color: #1a4a8a;
  border: 1.5px solid #1a4a8a;
  border-radius: 50%;
  width: 4.6em;
  height: 4.6em;
  margin-left: auto;
  margin-right: auto;
  display: flex;
  align-items: center;
  justify-content: center;
  transform: rotate(-12deg);
  opacity: 0.85;
  line-height: 1.1;
}
.amount-row {
  display: flex;
  align-items: baseline;
  gap: 0.35em;
  flex-wrap: wrap;
}
.lbl { font-weight: 600; }
.fill {
  border-bottom: 1px dotted #222;
  min-width: 4em;
  padding: 0 0.2em 0.05em;
}
.amount { font-size: 1.25em; font-weight: 700; min-width: 3.5em; }
.words { flex: 1 1 12em; font-weight: 600; }
.kind-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.25em;
  font-size: 0.9em;
}
.kind { opacity: 0.45; text-decoration: line-through; }
.kind.on { opacity: 1; text-decoration: none; font-weight: 700; }
.sep { opacity: 0.5; }
.hint { font-size: 0.85em; color: #444; margin-left: 0.2em; }
.card-pay {
  display: inline-flex;
  align-items: center;
  gap: 0.3em;
  margin-left: auto;
  font-size: 0.8em;
}
.card-pay .box {
  width: 0.85em;
  height: 0.85em;
  border: 1px solid #222;
  display: inline-block;
}
.field { margin: 0.55em 0 0.35em; }
.line {
  border-bottom: 1px dotted #222;
  min-height: 1.15em;
  padding: 0.05em 0.15em;
  font-weight: 600;
}
.line.muted { color: #666; font-weight: 400; }
.line.short { min-height: 1.05em; margin-top: 0.35em; }
.under {
  font-size: 0.62em;
  color: #555;
  margin-top: 0.1em;
}
.pesel-boxes { display: flex; gap: 0.15em; }
.pesel-boxes .cell {
  width: 1.15em;
  height: 1.25em;
  border: 1px solid #222;
  display: inline-block;
}
.offense {
  border: 1px solid #222;
  padding: 0.45em 0.5em 0.35em;
  margin: 0.55em 0 0.45em;
  min-height: 5.5em;
  position: relative;
}
.offense-lines {
  white-space: pre-wrap;
  font-size: 0.92em;
  line-height: 1.35;
  padding-bottom: 1.6em;
}
.points {
  position: absolute;
  right: 0.45em;
  bottom: 0.35em;
  display: flex;
  align-items: baseline;
  gap: 0.35em;
  font-size: 0.85em;
}
.points-val {
  border-bottom: 1px dotted #222;
  min-width: 2.5em;
  text-align: center;
  font-weight: 800;
  font-size: 1.2em;
}
.footer {
  display: grid;
  grid-template-columns: 1.2fr 1.4fr 0.9fr 0.8fr;
  border: 1px solid #222;
  margin-top: 0.2em;
}
.footer .cell {
  border-right: 1px solid #222;
  padding: 0.35em 0.4em 0.45em;
  min-height: 5em;
}
.footer .cell:last-child { border-right: none; }
.stamp-day {
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  font-size: 0.7em;
  color: #555;
}
.date { font-size: 0.85em; margin-bottom: 0.4em; }
.accept {
  border: 1px solid #222;
  background: #e8e4dc;
  color: #111;
  font: inherit;
  font-size: 0.95em;
  font-weight: 700;
  padding: 0.35em 1.1em;
  cursor: pointer;
  pointer-events: auto;
  box-shadow: 0 0.25em 0.8em rgba(0, 0, 0, 0.35);
}
.accept:hover { background: #ddd7cc; }

@media (max-width: 700px) {
  .mandat { font-size: 0.72em; }
  .footer { grid-template-columns: 1fr 1fr; }
  .footer .cell { min-height: 3.5em; }
  .stamp-box { display: none; }
}
</style>
