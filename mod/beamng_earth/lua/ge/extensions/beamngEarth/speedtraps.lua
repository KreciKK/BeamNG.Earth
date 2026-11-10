-- Point speed cameras. The server sends the fixed cameras that have a bearing and a posted
-- limit (speedcams.fixed_rows). Near the car this arms the game's own speed trap: a BeamNGTrigger
-- whose +Y is the monitored direction, and a light the game flashes when the car is over the
-- limit plus the game's own tolerance. The yellow camera mesh stays scenery. Average-speed
-- gantries are not traps (speedsections.lua times those).
local M = {}

M.SPAWN_M = 160
M.DROP_M = 320
M.LANE_M = 8          -- the device stands on the shoulder; the box sits this far into the lanes
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
  local x, y, z0 = anchor:geodeticToWorld(cam.lat, cam.lon, 0)
  local tx, ty = M.travel(anchor, x, y, cam.dir)
  local len = math.sqrt(tx * tx + ty * ty)
  if len < 1e-6 then return nil end
  tx, ty = tx / len, ty / len
  -- left of travel, over the carriageway (right-hand traffic, the camera is on the right shoulder)
  x, y = x - ty * M.LANE_M, y + tx * M.LANE_M
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
  return { trig = trig:getID(), light = light:getID(), name = trigName, x = x, y = y, z = gz + 1.5, limit = cam.limit }
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

-- Stock freeroam/career only show the speeding toast in those game states. BeamMP
-- (and anything else) is not freeroam, so the trap fires and the light may blink
-- with no message. We always say it ourselves; freeroam may also say its own.
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
  local text
  if plate ~= '' then
    text = string.format('Speed camera: %s — %d km/h (limit %d)',
      plate, math.floor(speed * 3.6 + 0.5), math.floor(limit * 3.6 + 0.5))
  else
    text = string.format('Speed camera: %d km/h (limit %d)',
      math.floor(speed * 3.6 + 0.5), math.floor(limit * 3.6 + 0.5))
  end
  if ui_message then pcall(ui_message, text, 8, 'beamngEarthCam') end
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
