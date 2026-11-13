-- Low-res offscreen photo from a speed camera's POV (speedtraps.lua). Separate RenderView so
-- the player's camera is not moved. Cheap: 640x360, short settle, no supersampling.
-- Files go under beamngEarth/speedcams/ in the user folder (same place as beamngEarth/reports/).
local M = {}

-- No leading slash: same style as report.lua → <userPath>/beamngEarth/speedcams/
M.DIR = 'beamngEarth/speedcams/'
M.WIDTH = 640
M.HEIGHT = 360
M.FOV = 55            -- a touch wider so the car fits while still looking from the housing
M.NEAR = 0.2
M.FAR = 400
M.DELAY = 0.02          -- capture ASAP so the car is still in the same spot (~0.7 m at 130 km/h)
M.VIEW = 'bngEarthSpeedcam'
M.BUSY = false
M.lastPath = nil
M.lastAbs = nil
M.lastError = nil
M.saves = 0

local function finite(v)
  return type(v) == 'number' and v == v and v > -math.huge and v < math.huge
end

local function note(msg)
  M.lastError = msg
  local logf = rawget(_G, 'log')
  if logf then pcall(logf, 'W', 'beamngEarth', 'camshot: ' .. tostring(msg)) end
end

local function info(msg)
  local logf = rawget(_G, 'log')
  if logf then pcall(logf, 'I', 'beamngEarth', 'camshot: ' .. tostring(msg)) end
end

-- Absolute OS path for a VFS-relative file under the user folder.
function M.absPath(rel)
  local FS = rawget(_G, 'FS')
  if not (FS and FS.getUserPath) or type(rel) ~= 'string' then return rel end
  local ok, root = pcall(function() return FS:getUserPath() end)
  if not ok or type(root) ~= 'string' or root == '' then return rel end
  return (root .. rel):gsub('\\', '/')
end

-- Pure: unit look direction from eye to target, or nil if too close / bad.
function M.lookDir(from, to)
  if not from or not to then return nil end
  local dx = (to.x or 0) - (from.x or 0)
  local dy = (to.y or 0) - (from.y or 0)
  local dz = (to.z or 0) - (from.z or 0)
  local len = math.sqrt(dx * dx + dy * dy + dz * dz)
  if not finite(len) or len < 0.5 then return nil end
  return { x = dx / len, y = dy / len, z = dz / len, dist = len }
end

-- Pure: VFS path for a capture (with .png).
function M.basePath(stamp, tag)
  stamp = tostring(stamp or 'shot')
  local safe = tostring(tag or 'cam'):gsub('[^%w%-_]+', '_'):sub(1, 40)
  return M.DIR .. 'speedcam_' .. stamp .. '_' .. safe .. '.png'
end

local function stampNow()
  return os.date('%Y%m%d_%H%M%S')
end

function M.ensureDir()
  local FS = rawget(_G, 'FS')
  if not FS then return true end
  if FS.directoryExists and FS:directoryExists(M.DIR) then return true end
  if FS.directoryCreate then
    local ok = pcall(function() FS:directoryCreate(M.DIR, true) end)
    if ok and FS.directoryExists and FS:directoryExists(M.DIR) then return true end
  end
  -- some builds only create leaf dirs if parents exist
  if FS.directoryCreate then
    pcall(function() FS:directoryCreate('beamngEarth/', true) end)
    pcall(function() FS:directoryCreate(M.DIR, true) end)
  end
  return not FS.directoryExists or FS:directoryExists(M.DIR)
end

local function quatLook(dir)
  local qfd = rawget(_G, 'quatFromDir')
  local up = vec3(0, 0, 1)
  if math.abs(dir.z) > 0.95 then up = vec3(0, 1, 0) end
  if qfd then return qfd(vec3(dir.x, dir.y, dir.z), up) end
  return quat(0, 0, 0, 1)
end

function M.renderOptions(from, to, filepath)
  local dir = M.lookDir(from, to)
  if not dir or type(filepath) ~= 'string' or filepath == '' then return nil end
  local rot = quatLook(dir)
  -- stock saveToFileJob writes to `filename` as given (and uses filename..".tmp.png")
  local stem = filepath:gsub('%.png$', '')
  return {
    renderViewName = M.VIEW,
    screenshotDelay = M.DELAY,
    resolution = vec3(M.WIDTH, M.HEIGHT, 0),
    copyMainViewExposure = true,
    pos = vec3(from.x, from.y, from.z),
    rot = rot,
    fov = M.FOV,
    nearPlane = M.NEAR,
    farClip = M.FAR,
    filename = stem,
    allowAntialiasing = false,
  }, filepath
end

local function loadRenderViews()
  local rv = rawget(_G, 'render_renderViews')
  if rv and rv.takeScreenshot then return rv end
  local ext = rawget(_G, 'extensions')
  if ext and ext.load then pcall(ext.load, 'render_renderViews') end
  rv = rawget(_G, 'render_renderViews')
  if rv and rv.takeScreenshot then return rv end
  return nil
end

local function onDone(success, filepath, cb, meta)
  meta = meta or {}
  if success and filepath then
    local stamp = require('beamngEarth/camstamp')
    local okS, okApply, aerr = pcall(function()
      return stamp.apply(filepath, meta)
    end)
    if not okS then
      note('stamp error: ' .. tostring(okApply))
    elseif not okApply then
      note('stamp failed: ' .. tostring(aerr))
    else
      info('stamped header')
    end
  end
  M.BUSY = false
  if success then
    M.lastPath = filepath
    M.lastAbs = M.absPath(filepath)
    M.lastError = nil
    M.saves = M.saves + 1
    info('saved ' .. tostring(M.lastAbs or filepath))
    local write = rawget(_G, 'writeFile')
    if write then
      pcall(write, filepath:gsub('%.png$', '.txt'),
        string.format('BeamNG.Earth speedcam\n%s\nspeed=%s limit=%s plate=%s place=%s\n',
          tostring(M.lastAbs or filepath), tostring(meta.speed), tostring(meta.limit),
          tostring(meta.plate or ''), tostring(meta.place or '')))
    end
    -- On BeamMP: Discord webhook via the server plugin (same speedcamPhotos kill switch as capture).
    pcall(function()
      local mp = require('beamngEarth/beammp')
      local sc = require('beamngEarth/speedcam_mp')
      if mp.inSession() and sc.enabled() then sc.send(filepath, meta) end
    end)
  else
    note('capture failed for ' .. tostring(filepath))
  end
  if cb then pcall(cb, success and filepath or nil) end
end

-- settings.speedcamPhotos (default on): kill switch for RenderView capture / Discord upload.
function M.photosEnabled()
  local ok, s = pcall(require, 'beamngEarth/settings')
  if ok and s and s.get and s.get('speedcamPhotos') == false then return false end
  return true
end

-- from/to: {x,y,z}. tag: filename fragment. meta: stamp fields. cb(path|nil).
function M.capture(from, to, tag, meta, cb)
  if type(meta) == 'function' then cb, meta = meta, nil end
  meta = meta or {}
  if not M.photosEnabled() then
    note('disabled (speedcamPhotos off)')
    if cb then pcall(cb, nil) end
    return false
  end
  if M.BUSY then
    note('busy')
    if cb then pcall(cb, nil) end
    return false
  end
  if not M.ensureDir() then
    note('mkdir failed: ' .. M.DIR .. ' (user ' .. tostring(M.absPath('')) .. ')')
    if cb then pcall(cb, nil) end
    return false
  end
  local filepath = M.basePath(stampNow(), tag)
  local opts = M.renderOptions(from, to, filepath)
  if not opts then
    note('bad camera (eye/target too close?)')
    if cb then pcall(cb, nil) end
    return false
  end
  local rv = loadRenderViews()
  if not rv then
    note('render_renderViews unavailable')
    if cb then pcall(cb, nil) end
    return false
  end
  M.BUSY = true
  M.lastError = nil
  info('capturing → ' .. M.absPath(filepath))
  local ok, err = pcall(rv.takeScreenshot, opts, function(success)
    -- stock saveToFileJob copies temp → opts.filename (often without .png)
    local FS = rawget(_G, 'FS')
    local stem = opts.filename
    local final = filepath
    if FS and FS.fileExists then
      if FS:fileExists(filepath) then
        final = filepath
      elseif FS:fileExists(stem) then
        if FS.renameFile then pcall(function() FS:renameFile(stem, filepath) end) end
        if FS:fileExists(filepath) then
          final = filepath
        else
          final = stem
          success = true
        end
      elseif FS:fileExists(stem .. '.png') then
        final = stem .. '.png'
      else
        success = false
      end
    end
    onDone(success == true, final, cb, meta)
  end)
  if not ok then
    M.BUSY = false
    note('takeScreenshot error: ' .. tostring(err))
    if cb then pcall(cb, nil) end
    return false
  end
  return true
end

function M.fromTrap(data, eye, target, tag, meta)
  if type(eye) ~= 'table' or not finite(eye.x) or not finite(eye.y) or not finite(eye.z) then
    note('no eye')
    return false
  end
  if type(target) ~= 'table' or not finite(target.x) or not finite(target.y) or not finite(target.z) then
    note('no target')
    return false
  end
  local id = tag
  if (not id or id == '') and type(data) == 'table' then
    id = tostring(data.triggerName or data.triggerID or 'trap')
  end
  return M.capture(eye, target, id, meta)
end

-- Console: require('beamngEarth/camshot').probe() — shot from the game camera toward the car.
function M.probe()
  local cam = rawget(_G, 'core_camera')
  local gpv = rawget(_G, 'getPlayerVehicle')
  if not (cam and cam.getPosition and gpv) then
    note('probe: no camera/vehicle')
    return false
  end
  local okc, cpos = pcall(function() return cam.getPosition() end)
  local veh = gpv(0)
  if not okc or not cpos or not veh then
    note('probe: missing pos')
    return false
  end
  local p = veh:getPosition()
  local plate = ''
  if core_vehicles and core_vehicles.getVehicleLicenseText then
    local ok, text = pcall(core_vehicles.getVehicleLicenseText, veh)
    if ok and text then plate = tostring(text) end
  end
  local place = ''
  local okLoc, loc = pcall(function() return require('beamngEarth/statusbar').location() end)
  if okLoc and type(loc) == 'table' then
    place = tostring(loc.place ~= '' and loc.place or loc.street or '')
  end
  return M.capture(
    { x = cpos.x, y = cpos.y, z = cpos.z },
    { x = p.x, y = p.y, z = p.z + 0.6 },
    'probe',
    { speed = 0, limit = 50, plate = plate, place = place, photo = 'PROBE' })
end

function M.status()
  return {
    dir = M.DIR,
    absDir = M.absPath(M.DIR),
    lastPath = M.lastPath,
    lastAbs = M.lastAbs,
    lastError = M.lastError,
    busy = M.BUSY,
    saves = M.saves,
  }
end

function M.reset()
  M.BUSY, M.lastPath, M.lastAbs, M.lastError = false, nil, nil, nil
end

return M
