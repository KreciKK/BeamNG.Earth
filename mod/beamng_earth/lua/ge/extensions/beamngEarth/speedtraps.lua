-- Point speed cameras. The server sends the fixed cameras that have a bearing and a posted
-- limit (speedcams.fixed_rows). Near the car this arms the game's own speed trap: a BeamNGTrigger
-- whose +Y is the monitored direction, and a light the game flashes when the car is over the
-- limit plus the game's own tolerance. The yellow camera mesh stays scenery. Average-speed
-- gantries are not traps (speedsections.lua times those).
local M = {}

M.SPAWN_M = 160
M.DROP_M = 320
M.LANE_M = 8          -- the device stands on the shoulder; the box sits this far into the lanes
M.EYE_H = 2.6         -- photo eye height above ground at the device (housing, not a bird's-eye mast)
M.EYE_IN_M = 0.6      -- nudge from the OSM point toward the lanes so the eye clears the pole mesh
M.LOOK_AHEAD = 22     -- fallback along-road aim when the car pose is unknown
M.LOOK_LANE = 3.5     -- metres into the carriageway from the eye (fallback only)
M.LOOK_PAST = 3       -- aim a few metres past the car so it sits mid-frame, not on the bottom edge
M.SCAN_S = 0.25

local cameras = {}
local live = {}       -- id -> { trig, light }
local lastScan = -math.huge

local function finite(v)
  return type(v) == 'number' and v == v and v > -math.huge and v < math.huge
end

local function metres(lat, lon, glat, glon)
  local mlon = 111320 * math.max(0.2, math.cos(math.rad((lat + glat) * 0.5)))
  return math.sqrt(((lon - glon) * mlon) ^ 2 + ((lat - glat) * 111320) ^ 2)
end

-- World XY of the monitored direction at (x, y): 0° is true north there, clockwise.
function M.travel(anchor, x, y, dir)
  local nx, ny = anchor:northAt(x, y)
  local s, c = math.sin(math.rad(dir)), math.cos(math.rad(dir))
  return s * ny + c * nx, -s * nx + c * ny
end

function M.onCameras(rows)
  local nextCams = {}
  if type(rows) == 'table' then
    for _, r in ipairs(rows) do
      if type(r) == 'table' and r.at and finite(r.at[1]) and finite(r.at[2])
          and finite(r.dir) and finite(r.limit) and r.limit > 0 then
        nextCams[#nextCams + 1] = {
          id = r.id, lat = r.at[1], lon = r.at[2], dir = r.dir, limit = r.limit,
        }
      end
    end
  end
  local keep = {}
  for _, cam in ipairs(nextCams) do keep[cam.id] = true end
  for id, e in pairs(live) do
    if not keep[id] then M.drop(e); live[id] = nil end
  end
  cameras = nextCams
end

function M.drop(e)
  if not e then return end
  for _, id in ipairs({ e.trig, e.light }) do
    if id then
      local o = scenetree and scenetree.findObjectById and scenetree.findObjectById(id)
      if o then pcall(function() o:delete() end) end
    end
  end
end

local function retire(name)
  if not scenetree or not scenetree.findObject then return end
  local o = scenetree.findObject(name)
  if o then pcall(function() o:delete() end) end
end

local function groundZ(x, y, hint)
  local ray = rawget(_G, 'castRayStatic')
  local base = hint or 0
  if not ray then return base end
  local ok, d = pcall(ray, vec3(x, y, base + 80), vec3(0, 0, -1), 160)
  if ok and type(d) == 'number' and d > 0 and d < 160 then return base + 80 - d end
  return base
end

local function spawn(cam, anchor, hintZ)
  if not createObject or not quatFromDir then return nil end
  -- OSM device on the shoulder (photo eye). Trigger/light are moved into the lanes below.
  local dx, dy, z0 = anchor:geodeticToWorld(cam.lat, cam.lon, 0)
  local tx, ty = M.travel(anchor, dx, dy, cam.dir)
  local len = math.sqrt(tx * tx + ty * ty)
  if len < 1e-6 then return nil end
  tx, ty = tx / len, ty / len
  local gzDev = groundZ(dx, dy, hintZ or z0)
  -- nudge toward the carriageway (left of travel) so the eye is not inside the pole mesh
  local eyeX = dx - ty * M.EYE_IN_M
  local eyeY = dy + tx * M.EYE_IN_M
  local eyeZ = gzDev + M.EYE_H
  -- left of travel, over the carriageway (right-hand traffic, the camera is on the right shoulder)
  local x = dx - ty * M.LANE_M
  local y = dy + tx * M.LANE_M
  local gz = groundZ(x, y, hintZ or z0)
  local q = quatFromDir(vec3(tx, ty, 0), vec3(0, 0, 1))
  local suffix = tostring(cam.id)
  local lightName, trigName = 'bneCamL' .. suffix, 'bneCamT' .. suffix
  retire(lightName)
  retire(trigName)

  -- Stock West Coast cameras use a SpotLight with a flare; PointLight still flashes.
  local light = createObject('SpotLight')
  if not light then return nil end
  light.canSave = false
  light:setField('range', 0, '50')
  light:setField('outerAngle', 0, '60')
  light:setField('brightness', 0, '2.5')
  light:setField('color', 0, '1 1 1 1')
  light:setField('castShadows', 0, 'false')
  light:setField('isEnabled', 0, 'false')
  light:setField('flareType', 0, 'BNG_Sunflare_3')
  light:setField('flareScale', 0, '10')
  light:registerObject(lightName)
  light:setPosRot(x, y, gz + 3.2, q.x, q.y, q.z, q.w)

  local trig = createObject('BeamNGTrigger')
  if not trig then M.drop({ light = light:getID() }); return nil end
  trig.loadMode = 1
  trig.canSave = false
  -- Stock level.json: luaFunction is what makes enter/exit call GE Lua. Without it the
  -- box exists and findObject finds it, but nothing reaches gameplay_speedTraps.
  trig:setField('triggerType', 0, 'Box')
  trig:setField('luaFunction', 0, 'onBeamNGTrigger')
  local limitMs = string.format('%.4f', cam.limit / 3.6)
  trig:setField('speedTrapType', 0, 'speed')
  trig:setField('speedLimit', 0, limitMs)
  trig:setField('speedTrapLightName', 0, lightName)
  trig.speedTrapType = 'speed'
  trig.speedLimit = limitMs
  trig.speedTrapLightName = lightName
  trig.luaFunction = 'onBeamNGTrigger'
  trig:registerObject(trigName)
  trig:setPosRot(x, y, gz + 1.5, q.x, q.y, q.z, q.w)
  trig:setScale(vec3(12, 10, 8))
  if scenetree and scenetree.MissionGroup then
    scenetree.MissionGroup:addObject(light)
    scenetree.MissionGroup:addObject(trig)
  end
  return {
    id = cam.id, trig = trig:getID(), light = light:getID(), name = trigName,
    x = x, y = y, z = gz + 1.5, limit = cam.limit,
    -- photo POV: device on the shoulder, not the mid-lane trigger/light
    eyeX = eyeX, eyeY = eyeY, eyeZ = eyeZ, tx = tx, ty = ty,
  }
end

-- Live trap for a gameplay_speedTraps triggerName ("bneCamT" .. id), or nil.
function M.findLive(triggerName)
  if type(triggerName) ~= 'string' or triggerName == '' then return nil end
  for _, e in pairs(live) do
    if e.name == triggerName then return e end
  end
  return nil
end

-- Refresh eyeX/Y/Z + travel from the OSM camera row (keeps photo POV in sync with EYE_* constants).
local function ensureEye(e, anchor)
  if not e or not anchor or not e.id then return e end
  local cam
  for _, c in ipairs(cameras) do if c.id == e.id then cam = c break end end
  if not cam then return e end
  local dx, dy, z0 = anchor:geodeticToWorld(cam.lat, cam.lon, 0)
  local tx, ty = M.travel(anchor, dx, dy, cam.dir)
  local len = math.sqrt(tx * tx + ty * ty)
  if len < 1e-6 then return e end
  tx, ty = tx / len, ty / len
  local gzDev = groundZ(dx, dy, e.z or z0)
  e.eyeX = dx - ty * M.EYE_IN_M
  e.eyeY = dy + tx * M.EYE_IN_M
  e.eyeZ = gzDev + M.EYE_H
  e.tx, e.ty = tx, ty
  return e
end

-- World eye for the photo: OSM device on the shoulder (not the mid-lane flash light).
function M.eyeOf(e)
  if not e then return nil end
  if finite(e.eyeX) and finite(e.eyeY) and finite(e.eyeZ) then
    return { x = e.eyeX, y = e.eyeY, z = e.eyeZ }
  end
  -- Legacy live entries / fallback: reconstruct shoulder from the lane trigger
  -- (trigger = device + left-of-travel * LANE_M).
  if finite(e.x) and finite(e.y) and finite(e.tx) and finite(e.ty) then
    return {
      x = e.x + e.ty * M.LANE_M - e.ty * M.EYE_IN_M,
      y = e.y - e.tx * M.LANE_M + e.tx * M.EYE_IN_M,
      z = (e.z or 0) - 1.5 + M.EYE_H,
    }
  end
  return nil
end

-- Look-at for the photo: the car, from the shoulder eye (shallow enough), a few metres past
-- it along travel so the body sits mid-frame. Aiming far down the road left the car clipped.
function M.lookTarget(e, veh)
  local eye = M.eyeOf(e)
  if not eye then return nil end
  local tx, ty = e.tx, e.ty
  if veh and finite(veh.x) and finite(veh.y) and finite(tx) and finite(ty) then
    local along = (veh.x - eye.x) * tx + (veh.y - eye.y) * ty
    local sign = along >= 0 and 1 or -1
    return {
      x = veh.x + tx * sign * M.LOOK_PAST,
      y = veh.y + ty * sign * M.LOOK_PAST,
      z = (finite(veh.z) and (veh.z + 0.45)) or (eye.z - 1.0),
    }
  end
  if not finite(tx) or not finite(ty) then return nil end
  return {
    x = eye.x + tx * M.LOOK_AHEAD - ty * M.LOOK_LANE,
    y = eye.y + ty * M.LOOK_AHEAD + tx * M.LOOK_LANE,
    z = eye.z - 1.0,
  }
end

-- Resolve a camera eye from the trigger event when findLive misses (reload / race).
function M.eyeFromData(data)
  local e = data and M.findLive(data.triggerName) or nil
  local eye = M.eyeOf(e)
  if eye then return eye end
  if not data or not scenetree then return nil end
  -- Last resort: from the trigger, step back to the right of travel (shoulder).
  if data.triggerID and scenetree.findObjectById then
    local trig = scenetree.findObjectById(data.triggerID)
    if trig and trig.getPosition and quat then
      local p = trig:getPosition()
      local q = quat(trig:getRotation())
      local forward = q * vec3(0, 1, 0)
      local right = q * vec3(1, 0, 0)
      if p and forward and right then
        -- RHT: device on the right shoulder → from lane centre go +right by ~LANE_M
        return {
          x = p.x + right.x * M.LANE_M,
          y = p.y + right.y * M.LANE_M,
          z = p.z - 1.5 + M.EYE_H,
        }
      end
      if p then return { x = p.x, y = p.y, z = p.z - 1.5 + M.EYE_H } end
    end
  end
  return nil
end

-- The origin moved: the traps were in the old frame. The next scan places the nearby ones again.
function M.transform()
  for id, e in pairs(live) do M.drop(e); live[id] = nil end
end

function M.park()
  M.transform()
end

function M.reset()
  M.transform()
  cameras = {}
  pcall(function() require('beamngEarth/mandat').reset() end)
end

function M.update(pl, anchor)
  if not pl or pl.camera or not anchor or not finite(pl.lat) or not finite(pl.lon) then return end
  local now = os.clock()
  if now - lastScan < M.SCAN_S then return end
  lastScan = now
  for _, cam in ipairs(cameras) do
    local d = metres(pl.lat, pl.lon, cam.lat, cam.lon)
    local have = live[cam.id]
    if d <= M.SPAWN_M then
      if not have then live[cam.id] = spawn(cam, anchor, pl.z) end
    elseif d >= M.DROP_M and have then
      M.drop(have)
      live[cam.id] = nil
    end
  end
end

function M.count() return #cameras end

-- Stock freeroam/career only toast in those game states. BeamMP (and anything else)
-- is not freeroam, so the trap fires with no message. We show a Polish "Mandat karny"
-- (mandat.lua) instead of a toast; freeroam may also say its own.
function M.onSpeedTrapTriggered(data, playerSpeed, overSpeed)
  if not data or not data.speedLimit then return end
  local vid = be and be.getPlayerVehicleID and be:getPlayerVehicleID(0)
  if data.subjectID ~= nil and vid ~= nil and data.subjectID ~= vid then return end
  local limit = tonumber(data.speedLimit) or 0
  local speed = tonumber(playerSpeed) or 0
  local plate = ''
  if core_vehicles and core_vehicles.getVehicleLicenseText then
    local veh = getPlayerVehicle and getPlayerVehicle(0)
    if veh then
      local ok, text = pcall(core_vehicles.getVehicleLicenseText, veh)
      if ok and text then plate = tostring(text) end
    end
  end
  local place = ''
  local okLoc, loc = pcall(function() return require('beamngEarth/statusbar').location() end)
  if okLoc and type(loc) == 'table' then
    place = tostring(loc.place ~= '' and loc.place or loc.street or '')
  end
  pcall(require('beamngEarth/mandat').show, {
    source = 'fotoradar',
    speed = speed * 3.6,
    limit = limit * 3.6,
    plate = plate,
    place = place,
  })
  -- Offscreen still + BeamMP Discord upload (settings.speedcamPhotos kill switch: diagnosing FMOD freezes).
  local photos = true
  pcall(function()
    local s = require('beamngEarth/settings')
    if s.get and s.get('speedcamPhotos') == false then photos = false end
  end)
  if photos then
    local liveE = M.findLive(data.triggerName)
    local core = rawget(_G, 'beamngEarth_core')
    local st = core and core._state and core._state()
    if liveE and st and st.anchor then ensureEye(liveE, st.anchor) end
    local eye = M.eyeOf(liveE) or M.eyeFromData(data)
    local vehPos
    local veh = getPlayerVehicle and getPlayerVehicle(0)
    if veh and veh.getPosition then
      local p = veh:getPosition()
      if p then vehPos = { x = p.x, y = p.y, z = p.z } end
    end
    -- Along the road into the lane (shallow), not straight down at the car in the box.
    local target = (liveE and M.lookTarget(liveE, vehPos)) or vehPos
    if target and vehPos and not target.z then target.z = vehPos.z + 0.5 end
    local camshot = require('beamngEarth/camshot')
    if eye and target then
      local speedKmh = math.floor(speed * 3.6 + 0.5)
      local limitKmh = math.floor(limit * 3.6 + 0.5)
      local tag = string.format('%s_%dkmh', tostring(data.triggerName or 'cam'), speedKmh)
      local meta = {
        speed = speedKmh, limit = limitKmh, plate = plate, place = place,
        time = os.date('%H:%M:%S'), date = os.date('%d.%m.%Y'),
        photo = tostring(data.triggerName or 'CAM'):gsub('^bneCamT', ''):sub(1, 6),
      }
      local ok, err = pcall(camshot.fromTrap, data, eye, target, tag, meta)
      if not ok and log then
        log('W', 'beamngEarth', 'camshot fromTrap: ' .. tostring(err))
      end
    elseif log then
      log('W', 'beamngEarth', string.format(
        'camshot skipped (eye=%s target=%s trigger=%s)',
        eye and 'ok' or 'nil', target and 'ok' or 'nil', tostring(data.triggerName)))
    end
  end
  if Engine and Engine.Audio and Engine.Audio.playOnce then
    pcall(Engine.Audio.playOnce, 'AudioGui', 'event:>UI>Main>Photo')
  end
end

-- Drop a live trap so the next update() rebuilds it with the current spawn().
function M.respawn(osmId)
  local id = tonumber(osmId) or osmId
  if live[id] then M.drop(live[id]); live[id] = nil end
  retire('bneCamT' .. tostring(id))
  retire('bneCamL' .. tostring(id))
  return true
end

-- Console: require('beamngEarth/speedtraps').status()
-- Near Sztum: require('beamngEarth/speedtraps').status(6145374818)
function M.status(osmId)
  local nLive = 0
  for _ in pairs(live) do nLive = nLive + 1 end
  local out = { cameras = #cameras, live = nLive }
  if osmId ~= nil then
    local id = tonumber(osmId) or osmId
    local cam, e = nil, live[id]
    for _, c in ipairs(cameras) do if c.id == id then cam = c break end end
    out.id, out.listed, out.spawned = id, cam ~= nil, e ~= nil
    if cam then out.lat, out.lon, out.dir, out.limit = cam.lat, cam.lon, cam.dir, cam.limit end
    local name = 'bneCamT' .. tostring(id)
    local o = scenetree and scenetree.findObject and scenetree.findObject(name)
    out.object = o ~= nil
    if o then
      out.speedTrapType = o.speedTrapType
      out.speedLimit = o.speedLimit
      out.pos = { o:getPosition().x, o:getPosition().y, o:getPosition().z }
    end
  end
  return out
end

return M
