-- luajit mod/tests/test_camshot.lua  (offscreen speed-camera stills, camshot.lua)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local camshot = require('beamngEarth/camshot')

camshot.reset()

local dirv = camshot.lookDir({ x = 0, y = 0, z = 2 }, { x = 0, y = 10, z = 1 })
T.check(dirv and math.abs(dirv.y - 1) < 0.05 and dirv.dist > 9, 'lookDir along +Y')
T.check(camshot.lookDir({ x = 0, y = 0, z = 0 }, { x = 0.1, y = 0, z = 0 }) == nil, 'too close → nil')
T.check(camshot.lookDir(nil, { x = 1, y = 0, z = 0 }) == nil, 'missing from → nil')

local path = camshot.basePath('20261111_120000', 'bneCamT1_90kmh')
T.check(path == 'beamngEarth/speedcams/speedcam_20261111_120000_bneCamT1_90kmh.png', 'basePath')
T.check(camshot.basePath('t', 'a/b c!'):find('a_b_c', 1, true), 'tag sanitized')

local opts = camshot.renderOptions({ x = 1, y = 2, z = 3 }, { x = 1, y = 12, z = 3 }, path)
T.check(opts and opts.filename == 'beamngEarth/speedcams/speedcam_20261111_120000_bneCamT1_90kmh'
  and opts.fov == camshot.FOV, 'renderOptions stem (no .png)')
T.check(opts.resolution.x == 640 and opts.resolution.y == 360, '640x360')
T.check(opts.screenshotDelay == camshot.DELAY, 'short delay')
T.check(camshot.renderOptions({ x = 0, y = 0, z = 0 }, { x = 0, y = 0.1, z = 0 }, path) == nil, 'bad look → nil options')
T.check(type(camshot.status().dir) == 'string', 'status()')

-- capture without jobsystem / RVM: fails cleanly, not busy-stuck
T.check(camshot.capture({ x = 0, y = 0, z = 2 }, { x = 0, y = 10, z = 1 }, 't') == false, 'no engine: capture false')
T.check(camshot.lastError ~= nil and not camshot.BUSY, 'error set, not busy')

local got = {}
camshot.fromTrap({ triggerName = 'bneCamT9' }, { x = 0, y = 0, z = 3 }, { x = 5, y = 5, z = 1 }, 'tag')
T.check(camshot.lastError ~= nil, 'fromTrap still needs engine')

-- busy lock
camshot.BUSY = true
T.check(camshot.capture({ x = 0, y = 0, z = 2 }, { x = 0, y = 10, z = 1 }, 'x') == false, 'busy rejects')
T.check(camshot.lastError == 'busy', 'busy error')
camshot.reset()
T.check(not camshot.BUSY and camshot.lastPath == nil, 'reset clears')

T.done('test_camshot')
