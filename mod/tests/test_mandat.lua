-- luajit mod/tests/test_mandat.lua  (Polish speed-camera / OPP mandat, mandat.lua)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local mandat = require('beamngEarth/mandat')

mandat.reset()

------------------------------------------------------------------------------------------
-- bands / points (PL 2022 fines, Sep 2022 points)

local cases = {
  { 1, 50, 0 }, { 10, 50, 0 },
  { 11, 100, 3 }, { 15, 100, 3 },
  { 16, 200, 3 }, { 20, 200, 3 },
  { 21, 300, 6 }, { 25, 300, 6 },
  { 26, 400, 6 }, { 30, 400, 6 },
  { 31, 800, 10 }, { 40, 800, 10 },
  { 41, 1000, 13 }, { 50, 1000, 13 },
  { 51, 1500, 15 }, { 60, 1500, 15 },
  { 61, 2000, 15 }, { 70, 2000, 15 },
  { 71, 2500, 15 }, { 120, 2500, 15 },
}
for _, c in ipairs(cases) do
  local f = mandat.fine(c[1], { recidivism = false, builtUp = false })
  T.check(f and f.amount == c[2] and f.points == c[3] and f.excess == c[1],
    string.format('excess %d → %d zł / %d pkt', c[1], c[2], c[3]))
end
T.check(mandat.fine(0) == nil and mandat.fine(-1) == nil and mandat.fine(nil) == nil, 'no fine without excess')

------------------------------------------------------------------------------------------
-- recidivism: 2× only for excess > 30 when a prior severe ticket exists

mandat.reset()
local first = mandat.fine(35, { recidivism = false })
T.check(first.amount == 800 and not first.recidivism, 'first severe: base 800')
local second = mandat.fine(35, { recidivism = true })
T.check(second.amount == 1600 and second.recidivism, 'recidivism: 1600')
T.check(mandat.fine(25, { recidivism = true }).amount == 300, '≤30 never doubles')
T.check(mandat.fine(55, { recidivism = true }).amount == 3000, '51–60 recidivism 3000')
T.check(mandat.fine(75, { recidivism = true }).amount == 5000, '71+ recidivism 5000')

------------------------------------------------------------------------------------------
-- session hadSevere via payload(commit)

mandat.reset()
T.check(not mandat.hadSevere(), 'fresh session: no severe')
local p1 = mandat.payload({ speed = 90, limit = 50, source = 'fotoradar', name = 'Test', commit = true })
T.check(p1 and p1.amount == 800 and p1.points == 10 and p1.excess == 40, 'payload: 40 over 50 → 800/10')
T.check(mandat.hadSevere(), 'excess 40 marks session severe')
local p2 = mandat.payload({ speed = 95, limit = 50, source = 'opp', name = 'Test', commit = true })
T.check(p2.amount == 2000 and p2.recidivism and p2.excess == 45, 'second severe in session: 2× (45 → 2000)')
local mild = mandat.payload({ speed = 60, limit = 50, source = 'fotoradar', name = 'Test', commit = true })
T.check(mild.amount == 50 and mild.points == 0 and not mild.recidivism, 'mild ticket still base amount')

------------------------------------------------------------------------------------------
-- built-up heuristic + license hold

local hold = mandat.fine(55, { limit = 50, recidivism = false })
T.check(hold.builtUp and hold.licenseHold, 'limit 50 + excess 55 → license hold note')
local noHold = mandat.fine(55, { limit = 90, recidivism = false })
T.check(not noHold.builtUp and not noHold.licenseHold, 'limit 90: not built-up, no hold')
local under = mandat.fine(40, { limit = 50, recidivism = false })
T.check(under.builtUp and not under.licenseHold, 'built-up but excess ≤50: no hold')

------------------------------------------------------------------------------------------
-- words (złoty / złote / złotych)

T.check(mandat.words(1) == 'jeden złoty', '1 złoty')
T.check(mandat.words(2) == 'dwa złote', '2 złote')
T.check(mandat.words(4) == 'cztery złote', '4 złote')
T.check(mandat.words(5) == 'pięć złotych', '5 złotych')
T.check(mandat.words(50) == 'pięćdziesiąt złotych', '50')
T.check(mandat.words(100) == 'sto złotych', '100')
T.check(mandat.words(200) == 'dwieście złotych', '200')
T.check(mandat.words(800) == 'osiemset złotych', '800')
T.check(mandat.words(1000) == 'tysiąc złotych', '1000')
T.check(mandat.words(1600) == 'tysiąc sześćset złotych', '1600')
T.check(mandat.words(2000) == 'dwa tysiące złotych', '2000')
T.check(mandat.words(2500) == 'dwa tysiące pięćset złotych', '2500')
T.check(mandat.words(5000) == 'pięć tysięcy złotych', '5000')
T.check(mandat.currencyForm(12) == 'złotych' and mandat.currencyForm(22) == 'złote', '12 vs 22 forms')

------------------------------------------------------------------------------------------
-- show() triggers guihooks; player name fallbacks

local sent = {}
guihooks = { trigger = function(name, d) sent[#sent + 1] = { name, d } end }
mandat.reset()
local shown = mandat.show({ speed = 72, limit = 50, source = 'fotoradar', plate = 'WA 12345', date = '2026 / 11 / 11' })
T.check(shown and shown.series == 'Aa' and shown.number == '0000001', 'serial Aa / 0000001')
T.check(shown.name == 'WA 12345', 'plate as name when no nick')
T.check(shown.kind == 'zaoczny' and shown.source == 'fotoradar', 'zaoczny fotoradar')
T.check(type(shown.words) == 'string' and shown.words:find('złot', 1, true), 'words mention złot…')
T.check(#sent == 1 and sent[1][1] == mandat.EVENT and sent[1][2].amount == shown.amount, 'guihooks BeamNGEarthMandat')
T.check(shown.description:find('art%. 92a', 1) and shown.description:find('fotoradar', 1, true), 'description cites art and source')

MPConfig = { Nickname = 'JanKowalski' }
local named = mandat.payload({ speed = 60, limit = 50, plate = 'XX', commit = false })
T.check(named.name == 'JanKowalski', 'BeamMP Nickname preferred over plate')
MPConfig = nil

T.check(mandat.payload({ speed = 50, limit = 50 }) == nil, 'exact limit: no mandat')
T.check(mandat.show({ speed = 'x', limit = 50 }) == nil, 'bad speed: no show')

mandat.reset()
T.done('test_mandat')
