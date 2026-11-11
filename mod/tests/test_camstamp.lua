-- luajit mod/tests/test_camstamp.lua
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local stamp = require('beamngEarth/camstamp')

T.check(stamp.padSpeed(65) == '065 km/h' and stamp.padLimit(50) == '050km/h', 'pad speed/limit')
T.check(stamp.sanitize('ul. Kościuszki') == 'UL. KOSCIUSZKI' or stamp.sanitize('ul. Kosciuszki') == 'UL. KOSCIUSZKI',
  'sanitize place')
T.check(stamp.sanitize('WA 12345') == 'WA 12345', 'plate kept')

local labels, values = stamp.lines({
  speed = 123, limit = 50, plate = 'WA 12345', place = 'Sztum',
  time = '16:48:42', date = '11.11.2026', photo = '614537',
})
T.check(labels:find('SPeed', 1, true) and labels:find('Plate', 1, true) and labels:find('Limit', 1, true),
  'label row')
T.check(values:find('123 km/h', 1, true) and values:find('050km/h', 1, true)
  and values:find('WA 12345', 1, true) and values:find('SZTUM', 1, true), 'value row')
T.check(values:find('16:48:42', 1, true) and values:find('11.11.2026', 1, true), 'time/date')
local cols = stamp.columns({ speed = 124, limit = 50, plate = 'TDD-4168', place = 'Malbork' })
T.check(#cols >= 6 and cols[1].label == 'SPeed' and cols[1].value == '124 km/h', 'columns layout')
T.check(cols[4].value == 'TDD-4168' and cols[5].value == '050km/h', 'plate/limit cols')

-- no GBitmap in stubs: apply fails cleanly
local ok, err = stamp.apply('missing.png', {})
T.check(ok == false and type(err) == 'string', 'apply without GBitmap/file fails')

T.done('test_camstamp')
