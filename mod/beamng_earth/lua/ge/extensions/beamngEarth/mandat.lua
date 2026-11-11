-- Polish "Mandat karny" for speed cameras and OPP (speedtraps.lua / speedsections.lua).
-- Tariff: PL from 2022 (fines) / 17 Sep 2022 (points). Session recidivism doubles fines for
-- excess > 30 km/h. Built-up heuristic: posted limit <= 50 → license-hold note when excess > 50.
-- UI: guihooks 'BeamNGEarthMandat' → MandatOverlay.vue (HTML overlay, not a HUD app).
local M = {}

M.EVENT = 'BeamNGEarthMandat'
M.SERIES = 'Aa'
M.BUILT_UP_LIMIT = 50
M.RECIDIVISM_EXCESS = 30
M.LICENSE_HOLD_EXCESS = 50

-- { maxExcess, amount, points } — first match where excess <= maxExcess wins.
M.BANDS = {
  { 10, 50, 0 },
  { 15, 100, 3 },
  { 20, 200, 3 },
  { 25, 300, 6 },
  { 30, 400, 6 },
  { 40, 800, 10 },
  { 50, 1000, 13 },
  { 60, 1500, 15 },
  { 70, 2000, 15 },
  { math.huge, 2500, 15 },
}

local serial = 0
local hadSevere = false   -- session "recidivism": a prior excess > 30

local ONES = {
  [0] = '', 'jeden', 'dwa', 'trzy', 'cztery', 'pięć', 'sześć', 'siedem', 'osiem', 'dziewięć',
  'dziesięć', 'jedenaście', 'dwanaście', 'trzynaście', 'czternaście', 'piętnaście',
  'szesnaście', 'siedemnaście', 'osiemnaście', 'dziewiętnaście',
}
local TENS = {
  [2] = 'dwadzieścia', [3] = 'trzydzieści', [4] = 'czterdzieści', [5] = 'pięćdziesiąt',
  [6] = 'sześćdziesiąt', [7] = 'siedemdziesiąt', [8] = 'osiemdziesiąt', [9] = 'dziewięćdziesiąt',
}
local HUNDS = {
  [1] = 'sto', [2] = 'dwieście', [3] = 'trzysta', [4] = 'czterysta', [5] = 'pięćset',
  [6] = 'sześćset', [7] = 'siedemset', [8] = 'osiemset', [9] = 'dziewięćset',
}

-- Pure: under-1000 Polish cardinal words (no currency).
local function under1000(n)
  if n == 0 then return 'zero' end
  local parts = {}
  local h = math.floor(n / 100)
  local r = n % 100
  if h > 0 then parts[#parts + 1] = HUNDS[h] end
  if r > 0 then
    if r < 20 then
      parts[#parts + 1] = ONES[r]
    else
      parts[#parts + 1] = TENS[math.floor(r / 10)]
      local o = r % 10
      if o > 0 then parts[#parts + 1] = ONES[o] end
    end
  end
  return table.concat(parts, ' ')
end

-- Pure: złoty / złote / złotych for integer amount.
function M.currencyForm(n)
  n = math.floor(math.abs(tonumber(n) or 0))
  local mod100 = n % 100
  local mod10 = n % 10
  if n == 1 then return 'złoty' end
  if mod10 >= 2 and mod10 <= 4 and not (mod100 >= 12 and mod100 <= 14) then return 'złote' end
  return 'złotych'
end

-- Pure: "osiemset złotych", "dwa tysiące złotych".
function M.words(amount)
  amount = math.floor(math.abs(tonumber(amount) or 0) + 0.5)
  local form = M.currencyForm(amount)
  if amount == 0 then return 'zero ' .. form end
  if amount < 1000 then return under1000(amount) .. ' ' .. form end
  local thousands = math.floor(amount / 1000)
  local rest = amount % 1000
  local head
  if thousands == 1 then
    head = 'tysiąc'
  elseif thousands >= 2 and thousands <= 4 then
    head = under1000(thousands) .. ' tysiące'
  else
    head = under1000(thousands) .. ' tysięcy'
  end
  if rest == 0 then return head .. ' ' .. form end
  return head .. ' ' .. under1000(rest) .. ' ' .. form
end

-- Pure: band for a rounded excess (km/h). excess <= 0 → nil.
function M.band(excessKmh)
  excessKmh = tonumber(excessKmh)
  if not excessKmh or excessKmh ~= excessKmh or excessKmh <= 0 then return nil end
  excessKmh = math.floor(excessKmh + 0.5)
  if excessKmh < 1 then return nil end
  for _, b in ipairs(M.BANDS) do
    if excessKmh <= b[1] then
      return { max = b[1], amount = b[2], points = b[3], excess = excessKmh }
    end
  end
  return nil
end

-- Pure: fine table. opts.recidivism / opts.builtUp override session heuristics when set.
function M.fine(excessKmh, opts)
  opts = opts or {}
  local b = M.band(excessKmh)
  if not b then return nil end
  local severe = b.excess > M.RECIDIVISM_EXCESS
  local recidivism = opts.recidivism
  if recidivism == nil then recidivism = hadSevere and severe end
  local amount = b.amount
  if recidivism and severe then amount = amount * 2 end
  local builtUp = opts.builtUp
  if builtUp == nil then builtUp = (tonumber(opts.limit) or 0) > 0 and (tonumber(opts.limit) or 0) <= M.BUILT_UP_LIMIT end
  local licenseHold = builtUp and b.excess > M.LICENSE_HOLD_EXCESS
  return {
    excess = b.excess,
    amount = amount,
    baseAmount = b.amount,
    points = b.points,
    recidivism = recidivism and severe or false,
    builtUp = builtUp and true or false,
    licenseHold = licenseHold and true or false,
    words = M.words(amount),
  }
end

function M.reset()
  serial, hadSevere = 0, false
end

function M.hadSevere() return hadSevere end

-- Best-effort player display name for the blank.
function M.playerName(opts)
  opts = opts or {}
  if type(opts.name) == 'string' and opts.name ~= '' then return opts.name end
  local cfg = rawget(_G, 'MPConfig')
  if type(cfg) == 'table' then
    if type(cfg.getNickname) == 'function' then
      local ok, n = pcall(cfg.getNickname)
      if ok and type(n) == 'string' and n ~= '' then return n end
    end
    if type(cfg.Nickname) == 'string' and cfg.Nickname ~= '' then return cfg.Nickname end
    if type(cfg.nickname) == 'string' and cfg.nickname ~= '' then return cfg.nickname end
  end
  if type(opts.plate) == 'string' and opts.plate ~= '' then return opts.plate end
  return 'Kierowca'
end

local function today()
  return os.date('%Y / %m / %d')
end

local function nextSerial()
  serial = serial + 1
  return serial
end

-- Build the UI payload (no side effects besides serial++ / hadSevere when commit).
-- opts: speed, limit (km/h), source ('fotoradar'|'opp'), plate, name, place, commit (default true)
function M.payload(opts)
  opts = opts or {}
  local speed = tonumber(opts.speed)
  local limit = tonumber(opts.limit)
  if not speed or not limit or speed ~= speed or limit ~= limit or limit <= 0 then return nil end
  local excess = math.floor(speed - limit + 0.5)
  local f = M.fine(excess, { limit = limit, recidivism = opts.recidivism, builtUp = opts.builtUp })
  if not f then return nil end
  local source = opts.source == 'opp' and 'opp' or 'fotoradar'
  local sourceLabel = source == 'opp' and 'odcinkowy pomiar prędkości (OPP)' or 'fotoradar'
  local desc = string.format(
    'Przekroczenie dopuszczalnej prędkości o %d km/h (pomiar %d km/h, limit %d km/h) — %s — art. 92a k.w.',
    f.excess, math.floor(speed + 0.5), math.floor(limit + 0.5), sourceLabel)
  if f.recidivism then desc = desc .. ' Recydywa (2×).' end
  if f.licenseHold then desc = desc .. ' Zatrzymanie prawa jazdy na 3 miesiące (obszar zabudowany).' end
  if type(opts.place) == 'string' and opts.place ~= '' then
    desc = desc .. ' Miejsce: ' .. opts.place .. '.'
  end
  local num = opts.number or nextSerial()
  local out = {
    series = M.SERIES,
    number = string.format('%07d', num),
    name = M.playerName(opts),
    amount = f.amount,
    words = f.words,
    points = f.points,
    excess = f.excess,
    speed = math.floor(speed + 0.5),
    limit = math.floor(limit + 0.5),
    source = source,
    description = desc,
    recidivism = f.recidivism,
    licenseHold = f.licenseHold,
    builtUp = f.builtUp,
    date = opts.date or today(),
    kind = 'zaoczny',
  }
  if opts.commit ~= false and f.excess > M.RECIDIVISM_EXCESS then hadSevere = true end
  return out
end

function M.show(opts)
  local p = M.payload(opts)
  if not p then return nil end
  local gh = rawget(_G, 'guihooks')
  if gh and gh.trigger then pcall(gh.trigger, M.EVENT, p) end
  return p
end

return M
