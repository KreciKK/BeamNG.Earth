-- Average-speed sections (odcinkowy pomiar prędkości). The server pairs each D-51a with the
-- D-51b ahead of it (speedcams.pair_sections) and sends the list once. Crossing the start
-- board announces the section; crossing the end reports the average, and plays the shutter
-- when that average is over the posted limit.
local M = {}

M.GATE_M = 30         -- m either side of the board (centreline or either shoulder)
M.HEADING = 70        -- degrees the car's heading may differ from the monitored direction
M.MIN_MS = 3          -- slower than this, a gate is not a pass
M.JUMP_M = 400        -- a teleport is not a pass and drops the open section
M.MAX_S = 30 * 60
M.GRACE_KMH = 3
M.COOLDOWN_S = 15
M.SOUND = 'event:>UI>Main>Photo'

local sections = {}
local prev = nil
local active = nil
local cool = {}

local function finite(v)
  return type(v) == 'number' and v == v and v > -math.huge and v < math.huge
end

local function angDiff(a, b)
  return math.abs((a - b + 180) % 360 - 180)
end

-- Metres east/north of a gate, then along the monitored direction and to its right.
function M.alongSide(lat, lon, glat, glon, dir)
  local mlon = 111320 * math.max(0.2, math.cos(math.rad((lat + glat) * 0.5)))
  local east = (lon - glon) * mlon
  local north = (lat - glat) * 111320
  local r = math.rad(dir)
  local fe, fn = math.sin(r), math.cos(r)
  return east * fe + north * fn, east * fn - north * fe
end

-- The segment from (a0, s0) to (a1, s1) crosses the board going forward, inside the lane band.
function M.crossed(a0, s0, a1, s1)
  if not a0 or a0 >= 0 or not a1 or a1 < 0 then return false end
  local denom = a0 - a1
  if denom == 0 then return false end
  local side = s0 + (s1 - s0) * (a0 / denom)
  return math.abs(side) <= M.GATE_M
end

function M.averageKmh(lengthM, seconds)
  if not lengthM or not seconds or seconds <= 0 then return nil end
  return (lengthM / seconds) * 3.6
end

function M.overLimit(avg, limit)
  if not avg or not limit then return false end
  return avg > limit + M.GRACE_KMH
end

local function heading(pl)
  local vx, vy = pl.vx, pl.vy
  if not vx or not vy then return nil end
  if vx * vx + vy * vy < M.MIN_MS * M.MIN_MS then return nil end
  local deg = math.deg(math.atan2(vx, vy))
  if deg < 0 then deg = deg + 360 end
  return deg
end

local function metres(lat, lon, glat, glon)
  local mlon = 111320 * math.max(0.2, math.cos(math.rad((lat + glat) * 0.5)))
  return math.sqrt(((lon - glon) * mlon) ^ 2 + ((lat - glat) * 111320) ^ 2)
end

local function nearBox(lat, lon, glat, glon)
  return math.abs(lat - glat) < 0.004 and math.abs(lon - glon) < 0.006
end

function M.onRows(rows)
  local nextSecs = {}
  if type(rows) == 'table' then
    for _, r in ipairs(rows) do
      if type(r) == 'table' and r.a and r.b and finite(r.dir) and finite(r.len) and r.len > 0 then
        nextSecs[#nextSecs + 1] = {
          id = r.id, alat = r.a[1], alon = r.a[2], blat = r.b[1], blon = r.b[2],
          dir = r.dir, limit = finite(r.limit) and r.limit or nil, length = r.len,
        }
      end
    end
  end
  sections = nextSecs
  if active then
    local keep = false
    for _, sec in ipairs(sections) do
      if sec.id == active.id then keep = true break end
    end
    if not keep then active = nil end
  end
end

function M.park()
  prev, active, cool = nil, nil, {}
end

function M.reset()
  sections, prev, active, cool = {}, nil, nil, {}
end

local function say(text)
  if ui_message then pcall(ui_message, text, 8, 'beamngEarthOpp') end
end

local function announceStart(sec)
  if sec.limit then
    say(string.format('Average speed section — limit %d km/h', math.floor(sec.limit + 0.5)))
  else
    say('Average speed section')
  end
end

local function announceEnd(sec, seconds)
  local avg = M.averageKmh(sec.length, seconds)
  if not avg then return end
  local over = M.overLimit(avg, sec.limit)
  local text
  if sec.limit then
    text = string.format('%s %d km/h (limit %d)',
      over and 'Speed camera: average' or 'Average speed',
      math.floor(avg + 0.5), math.floor(sec.limit + 0.5))
  else
    text = string.format('Average speed %d km/h', math.floor(avg + 0.5))
  end
  say(text)
  if over and Engine and Engine.Audio and Engine.Audio.playOnce then
    pcall(Engine.Audio.playOnce, 'AudioGui', M.SOUND)
  end
end

local function cooled(key, now)
  local at = cool[key]
  return at and now - at < M.COOLDOWN_S
end

-- pl: the player (lat, lon, vx, vy). dt: seconds the simulation advanced.
function M.update(pl, dt)
  if not pl or pl.camera or not finite(pl.lat) or not finite(pl.lon) then return end
  dt = tonumber(dt) or 0
  if dt < 0 then dt = 0 end
  local lat, lon = pl.lat, pl.lon
  if prev and metres(lat, lon, prev.lat, prev.lon) > M.JUMP_M then
    active, prev = nil, { lat = lat, lon = lon }
    return
  end
  if active then
    active.time = active.time + dt
    if active.time > M.MAX_S then active = nil end
  end
  local head = heading(pl)
  if prev and head and #sections > 0 then
    local now = os.clock()
    for _, sec in ipairs(sections) do
      if angDiff(head, sec.dir) <= M.HEADING then
        if nearBox(lat, lon, sec.alat, sec.alon) or nearBox(prev.lat, prev.lon, sec.alat, sec.alon) then
          local a0, s0 = M.alongSide(prev.lat, prev.lon, sec.alat, sec.alon, sec.dir)
          local a1, s1 = M.alongSide(lat, lon, sec.alat, sec.alon, sec.dir)
          if M.crossed(a0, s0, a1, s1) and not cooled('a' .. tostring(sec.id), now) then
            cool['a' .. tostring(sec.id)] = now
            active = { id = sec.id, time = 0, length = sec.length, limit = sec.limit }
            announceStart(sec)
          end
        end
        if active and active.id == sec.id
            and (nearBox(lat, lon, sec.blat, sec.blon) or nearBox(prev.lat, prev.lon, sec.blat, sec.blon)) then
          local a0, s0 = M.alongSide(prev.lat, prev.lon, sec.blat, sec.blon, sec.dir)
          local a1, s1 = M.alongSide(lat, lon, sec.blat, sec.blon, sec.dir)
          if M.crossed(a0, s0, a1, s1) and active.time > 1 and not cooled('b' .. tostring(sec.id), now) then
            cool['b' .. tostring(sec.id)] = now
            announceEnd(active, active.time)
            active = nil
          end
        end
      end
    end
  end
  prev = { lat = lat, lon = lon }
end

function M.count() return #sections end

function M.status(osmId)
  local out = { sections = #sections, active = active and active.id or nil }
  if osmId ~= nil then
    local id = tonumber(osmId) or osmId
    for _, sec in ipairs(sections) do
      if sec.id == id then
        out.sec = sec
        break
      end
    end
  end
  return out
end

return M
