-- Stamp a TraffiStar-style header onto a speedcam PNG (GBitmap): grayscale + black bar
-- with speed, plate, limit, time/date and location. Used by camshot.lua after capture.
local M = {}

M.HEADER_H = 44

-- 5x7 glyphs as 7 strings of 5 chars (1 = on). Digits, A-Z, and a few symbols.
local G = {
  [' '] = { '00000', '00000', '00000', '00000', '00000', '00000', '00000' },
  ['0'] = { '01110', '10001', '10011', '10101', '11001', '10001', '01110' },
  ['1'] = { '00100', '01100', '00100', '00100', '00100', '00100', '01110' },
  ['2'] = { '01110', '10001', '00001', '00010', '00100', '01000', '11111' },
  ['3'] = { '01110', '10001', '00001', '00110', '00001', '10001', '01110' },
  ['4'] = { '00010', '00110', '01010', '10010', '11111', '00010', '00010' },
  ['5'] = { '11111', '10000', '11110', '00001', '00001', '10001', '01110' },
  ['6'] = { '00110', '01000', '10000', '11110', '10001', '10001', '01110' },
  ['7'] = { '11111', '00001', '00010', '00100', '01000', '01000', '01000' },
  ['8'] = { '01110', '10001', '10001', '01110', '10001', '10001', '01110' },
  ['9'] = { '01110', '10001', '10001', '01111', '00001', '00010', '01100' },
  ['A'] = { '01110', '10001', '10001', '11111', '10001', '10001', '10001' },
  ['B'] = { '11110', '10001', '10001', '11110', '10001', '10001', '11110' },
  ['C'] = { '01110', '10001', '10000', '10000', '10000', '10001', '01110' },
  ['D'] = { '11110', '10001', '10001', '10001', '10001', '10001', '11110' },
  ['E'] = { '11111', '10000', '10000', '11110', '10000', '10000', '11111' },
  ['F'] = { '11111', '10000', '10000', '11110', '10000', '10000', '10000' },
  ['G'] = { '01110', '10001', '10000', '10111', '10001', '10001', '01110' },
  ['H'] = { '10001', '10001', '10001', '11111', '10001', '10001', '10001' },
  ['I'] = { '01110', '00100', '00100', '00100', '00100', '00100', '01110' },
  ['J'] = { '00111', '00010', '00010', '00010', '00010', '10010', '01100' },
  ['K'] = { '10001', '10010', '10100', '11000', '10100', '10010', '10001' },
  ['L'] = { '10000', '10000', '10000', '10000', '10000', '10000', '11111' },
  ['M'] = { '10001', '11011', '10101', '10001', '10001', '10001', '10001' },
  ['N'] = { '10001', '11001', '10101', '10011', '10001', '10001', '10001' },
  ['O'] = { '01110', '10001', '10001', '10001', '10001', '10001', '01110' },
  ['P'] = { '11110', '10001', '10001', '11110', '10000', '10000', '10000' },
  ['Q'] = { '01110', '10001', '10001', '10001', '10101', '10010', '01101' },
  ['R'] = { '11110', '10001', '10001', '11110', '10100', '10010', '10001' },
  ['S'] = { '01111', '10000', '10000', '01110', '00001', '00001', '11110' },
  ['T'] = { '11111', '00100', '00100', '00100', '00100', '00100', '00100' },
  ['U'] = { '10001', '10001', '10001', '10001', '10001', '10001', '01110' },
  ['V'] = { '10001', '10001', '10001', '10001', '10001', '01010', '00100' },
  ['W'] = { '10001', '10001', '10001', '10101', '10101', '10101', '01010' },
  ['X'] = { '10001', '10001', '01010', '00100', '01010', '10001', '10001' },
  ['Y'] = { '10001', '10001', '01010', '00100', '00100', '00100', '00100' },
  ['Z'] = { '11111', '00001', '00010', '00100', '01000', '10000', '11111' },
  [':'] = { '00000', '00100', '00100', '00000', '00100', '00100', '00000' },
  ['.'] = { '00000', '00000', '00000', '00000', '00000', '01100', '01100' },
  ['-'] = { '00000', '00000', '00000', '11111', '00000', '00000', '00000' },
  ['/'] = { '00001', '00010', '00010', '00100', '01000', '01000', '10000' },
  ['='] = { '00000', '00000', '11111', '00000', '11111', '00000', '00000' },
}

function M.sanitize(s, maxLen)
  if s == nil then return '' end
  s = tostring(s):upper()
  local reps = {
    ['\196\133'] = 'A', ['\196\132'] = 'A', ['\196\135'] = 'C', ['\196\134'] = 'C',
    ['\196\153'] = 'E', ['\196\152'] = 'E', ['\197\130'] = 'L', ['\197\129'] = 'L',
    ['\197\132'] = 'N', ['\197\131'] = 'N', ['\195\179'] = 'O', ['\195\147'] = 'O',
    ['\197\155'] = 'S', ['\197\154'] = 'S', ['\197\186'] = 'Z', ['\197\185'] = 'Z',
    ['\197\188'] = 'Z', ['\197\187'] = 'Z',
  }
  for a, b in pairs(reps) do s = s:gsub(a, b) end
  s = s:gsub('[\128-\255]', '')
  s = s:gsub('[^%w%:%./%-%s=]', ' ')
  s = s:gsub('%s+', ' '):gsub('^%s+', ''):gsub('%s+$', '')
  maxLen = tonumber(maxLen) or 48
  if #s > maxLen then s = s:sub(1, maxLen) end
  return s
end

function M.padSpeed(kmh)
  kmh = math.floor(math.abs(tonumber(kmh) or 0) + 0.5)
  return string.format('%03d km/h', math.min(kmh, 999))
end

function M.padLimit(kmh)
  kmh = math.floor(math.abs(tonumber(kmh) or 0) + 0.5)
  return string.format('%03dkm/h', math.min(kmh, 999))
end

-- Pure: column layout for the TraffiStar-style bar. Each { label, value, widthChars }.
function M.columns(meta)
  meta = meta or {}
  local plate = M.sanitize(meta.plate ~= '' and meta.plate or '------', 10)
  local place = M.sanitize(meta.place ~= '' and meta.place or '---', 14)
  local tim = meta.time or os.date('%H:%M:%S')
  local date = meta.date or os.date('%d.%m.%Y')
  local photo = M.sanitize(meta.photo or '110A', 6)
  return {
    { label = 'SPeed', value = M.padSpeed(meta.speed), w = 10 },
    { label = 'Time', value = tim, w = 8 },
    { label = 'Date', value = date, w = 10 },
    { label = 'Plate', value = plate, w = 10 },
    { label = 'Limit', value = M.padLimit(meta.limit), w = 8 },
    { label = 'Loc', value = place, w = 14 },
    { label = 'Photo', value = photo, w = 6 },
    { label = 'TraffiStar', value = 'SR520', w = 10 },
  }
end

-- Back-compat for tests: two joined lines.
function M.lines(meta)
  local cols = M.columns(meta)
  local labels, values = {}, {}
  for _, c in ipairs(cols) do
    labels[#labels + 1] = c.label
    values[#values + 1] = c.value
  end
  return table.concat(labels, '  '), table.concat(values, '  ')
end

local function setPx(bmp, x, y, col)
  if x < 0 or y < 0 then return end
  if x >= bmp:getWidth() or y >= bmp:getHeight() then return end
  bmp:setColor(x, y, col)
end

local function drawChar(bmp, ch, ox, oy, scale, col)
  local g = G[ch] or G[' ']
  scale = scale or 1
  for row = 1, 7 do
    local bits = g[row]
    for colx = 1, 5 do
      if bits:sub(colx, colx) == '1' then
        for sy = 0, scale - 1 do
          for sx = 0, scale - 1 do
            setPx(bmp, ox + (colx - 1) * scale + sx, oy + (row - 1) * scale + sy, col)
          end
        end
      end
    end
  end
end

local function drawText(bmp, text, x, y, scale, col)
  local advance = 6 * scale
  for i = 1, #text do
    drawChar(bmp, text:sub(i, i), x + (i - 1) * advance, y, scale, col)
  end
  return #text * advance
end

local function fillRect(bmp, x0, y0, x1, y1, col)
  local w, h = bmp:getWidth(), bmp:getHeight()
  x0, y0 = math.max(0, x0), math.max(0, y0)
  x1, y1 = math.min(w - 1, x1), math.min(h - 1, y1)
  for y = y0, y1 do
    for x = x0, x1 do
      bmp:setColor(x, y, col)
    end
  end
end

local function toMono(bmp)
  local w, h = bmp:getWidth(), bmp:getHeight()
  local col = ColorI(0, 0, 0, 255)
  for y = 0, h - 1 do
    for x = 0, w - 1 do
      if bmp:getColor(x, y, col) then
        local yv = math.floor(0.299 * col.r + 0.587 * col.g + 0.114 * col.b + 0.5)
        yv = math.floor((yv - 128) * 1.15 + 128 + 0.5)
        if yv < 0 then yv = 0 elseif yv > 255 then yv = 255 end
        col.r, col.g, col.b, col.a = yv, yv, yv, 255
        bmp:setColor(x, y, col)
      end
    end
  end
end

-- Draw the column header into the top of the bitmap.
function M.drawHeader(bmp, meta)
  local black = ColorI(0, 0, 0, 255)
  local white = ColorI(240, 240, 240, 255)
  local dim = ColorI(160, 160, 160, 255)
  local rule = ColorI(70, 70, 70, 255)
  local w = bmp:getWidth()
  local hh = math.min(M.HEADER_H, bmp:getHeight())
  fillRect(bmp, 0, 0, w - 1, hh - 1, black)
  -- hairline under the bar
  fillRect(bmp, 0, hh - 1, w - 1, hh - 1, rule)

  local cols = M.columns(meta)
  local gap = 10
  local x = 8
  local labelY, valueY = 5, 20
  local labelScale, valueScale = 1, 1
  -- On wider shots, bump values one pixel-scale for readability.
  if w >= 700 then valueScale = 2; valueY = 18; hh = math.min(52, bmp:getHeight()) end

  for i, c in ipairs(cols) do
    local colW = math.max(c.w, #c.label, #c.value) * 6 * valueScale
    if i == 1 then
      -- Speed stands out a bit
      drawText(bmp, c.label, x, labelY, labelScale, dim)
      drawText(bmp, c.value, x, valueY, valueScale, white)
    else
      drawText(bmp, c.label, x, labelY, labelScale, dim)
      drawText(bmp, c.value, x, valueY, valueScale, white)
    end
    -- faint column divider
    if i < #cols then
      local divx = x + colW + math.floor(gap / 2) - 1
      if divx < w - 4 then
        fillRect(bmp, divx, 4, divx, hh - 4, rule)
      end
    end
    x = x + colW + gap
    if x > w - 20 then break end
  end
end

function M.apply(path, meta)
  if type(path) ~= 'string' or path == '' then return false, 'no path' end
  if not rawget(_G, 'GBitmap') or not rawget(_G, 'ColorI') then
    return false, 'no GBitmap'
  end
  local FS = rawget(_G, 'FS')
  if FS and FS.fileExists and not FS:fileExists(path) then
    return false, 'missing ' .. path
  end
  local bmp = GBitmap()
  if not bmp:loadFile(path) then return false, 'load failed' end
  toMono(bmp)
  M.drawHeader(bmp, meta or {})
  if not bmp:saveFile(path) then return false, 'save failed' end
  return true
end

return M
