-- Send a stamped speedcam PNG to the BeamNG.Earth BeamMP plugin (bngeSpeedcam), which posts it to the
-- server's Discord webhook. Same base64 alphabet as the share path ("-" for "Z": BeamMP's launcher aborts
-- on "Zp" in a message over 500 bytes). Offline / no BeamMP: no-op.
local M = {}

M.CHUNK = 24576
M.MAX = 1024 * 1024
M.seq = 0

-- (same alphabet as beammp/Server/BeamNGEarth/main.lua G.b64)
local B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXY-abcdefghijklmnopqrstuvwxyz0123456789+/'

function M.b64(s)
  local out, n = {}, #s
  for i = 1, n, 3 do
    local a, b, c = s:byte(i, i + 2)
    local v = a * 65536 + (b or 0) * 256 + (c or 0)
    local q1, q2 = math.floor(v / 262144), math.floor(v / 4096) % 64
    local q3, q4 = math.floor(v / 64) % 64, v % 64
    out[#out + 1] = B64:sub(q1 + 1, q1 + 1) .. B64:sub(q2 + 1, q2 + 1)
      .. (b and B64:sub(q3 + 1, q3 + 1) or '=') .. (c and B64:sub(q4 + 1, q4 + 1) or '=')
  end
  return table.concat(out)
end

local function note(msg)
  local logf = rawget(_G, 'log')
  if logf then pcall(logf, 'W', 'beamngEarth', 'speedcam_mp: ' .. tostring(msg)) end
end

local function info(msg)
  local logf = rawget(_G, 'log')
  if logf then pcall(logf, 'I', 'beamngEarth', 'speedcam_mp: ' .. tostring(msg)) end
end

local function inSession()
  local ok, mp = pcall(require, 'beamngEarth/beammp')
  return ok and mp and mp.inSession and mp.inSession() == true
end

-- Read PNG bytes (binary). Prefer absolute path + io.open; fall back to VFS readFile.
function M.readPng(path)
  if type(path) ~= 'string' or path == '' then return nil end
  local abs = path
  local okAbs, cam = pcall(require, 'beamngEarth/camshot')
  if okAbs and cam and cam.absPath then abs = cam.absPath(path) end
  local f = io.open(abs, 'rb')
  if f then
    local data = f:read('*a')
    f:close()
    if type(data) == 'string' and #data >= 8 and data:sub(1, 8) == '\137PNG\r\n\26\n' then return data end
  end
  local read = rawget(_G, 'readFile')
  if read then
    local ok, data = pcall(read, path)
    if ok and type(data) == 'string' and #data >= 8 and data:sub(1, 8) == '\137PNG\r\n\26\n' then return data end
  end
  return nil
end

local function fire(payload)
  if not rawget(_G, 'TriggerServerEvent') then return false end
  local enc = rawget(_G, 'jsonEncode')
  if type(enc) ~= 'function' then return false end
  local ok, text = pcall(enc, payload)
  if not ok or type(text) ~= 'string' then return false end
  -- never a message BeamMP's launcher aborts on (over 400 bytes with "Zp")
  if #text > 400 and text:find('Zp', 1, true) then
    note('refused: Zp in payload')
    return false
  end
  TriggerServerEvent('bngeSpeedcam', text)
  return true
end

-- Same kill switch as camshot (settings.speedcamPhotos): off → no Discord upload either.
function M.enabled()
  local ok, s = pcall(require, 'beamngEarth/settings')
  if ok and s and s.get and s.get('speedcamPhotos') == false then return false end
  return true
end

-- path: VFS or absolute PNG. meta: { speed, limit, plate, place, ... }.
function M.send(path, meta)
  if not M.enabled() then
    note('disabled (speedcamPhotos off)')
    return false
  end
  if not inSession() then return false end
  if not rawget(_G, 'TriggerServerEvent') then return false end
  meta = meta or {}
  local png = M.readPng(path)
  if not png then
    note('no png at ' .. tostring(path))
    return false
  end
  if #png > M.MAX then
    note('png too large: ' .. tostring(#png))
    return false
  end
  M.seq = M.seq + 1
  local id = string.format('%d_%d', os.time() or 0, M.seq)
  local n = math.max(1, math.ceil(#png / M.CHUNK))
  if not fire({
    v = 1, t = 'h', id = id, n = n, size = #png,
    speed = tonumber(meta.speed) or 0,
    limit = tonumber(meta.limit) or 0,
    plate = tostring(meta.plate or ''):sub(1, 24),
    place = tostring(meta.place or ''):sub(1, 48),
  }) then
    note('header send failed')
    return false
  end
  for i = 0, n - 1 do
    local from = i * M.CHUNK + 1
    local chunk = png:sub(from, from + M.CHUNK - 1)
    if not fire({ v = 1, t = 'c', id = id, i = i, n = n, d = M.b64(chunk) }) then
      note('chunk ' .. tostring(i) .. ' failed')
      return false
    end
  end
  info(string.format('sent %d bytes in %d chunks (%s)', #png, n, id))
  return true
end

return M
