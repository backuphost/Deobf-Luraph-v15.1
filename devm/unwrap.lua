-- Luraph devirtualizer: container probe (runs under the patched Luau CLI).
-- usage: luau unwrap.lua -a <shipped file> <out prefix>
--
-- Luraph's "compressed" output is a small VM object whose only job is to decode and decompress
-- two blobs (a Lua source: the real VM chunk, and that chunk's data buffer), compile the source
-- with loadstring(source, "Luraph") and call the result with the buffer.  This runs the shipped
-- file with a recording loadstring and stops at the first call of a loaded function:
--     <out prefix>.chunk<N>.lua        text handed to the N-th loadstring call
--     <out prefix>.chunk<N>.arg<i>.bin buffer / string arguments of the first call of that chunk
--     <out prefix>.info                one line per chunk: N <TAB> chunk name <TAB> size <TAB> argument types
-- The script only gets pure library functions: no file access, no Roblox objects.  A file that
-- touches anything else before it loads a chunk is not a container, and the probe says so.
-- (Grown out of the lead's unwrap experiment for the v15.2 sample.)
local inp, outp = ...
local src = readfile(inp)
local realLoad, realWrite, realEwrite = loadstring, writefile, ewrite
local DONE = "__LPH_UNWRAP_DONE__"
local captured = 0
local info = {}
local env = {}
for _, n in ipairs({ "assert", "error", "getmetatable", "ipairs", "next", "pairs", "pcall", "xpcall", "rawequal", "rawget",
	"rawlen", "rawset", "select", "setmetatable", "tonumber", "tostring", "type", "typeof", "unpack", "newproxy", "gcinfo",
	"string", "table", "math", "bit32", "buffer", "coroutine", "utf8", "os", "debug", "vector", "_VERSION", "print" }) do
	env[n] = getfenv()[n]
end
env._G = env
env.getfenv = function() return env end
env.getgenv = function() return env end
env.getrenv = function() return env end
env.loadstring = function(code, name, ...)
	if type(code) ~= "string" then return nil, "not a string" end
	local ok = realLoad(code, name or "=chunk")
	if not ok then return realLoad(code, name) end       -- not Lua: let the script see the compile error
	captured += 1
	local n = captured
	realWrite(outp .. ".chunk" .. n .. ".lua", code)
	info[n] = { name = tostring(name), size = #code, args = "" }
	return function(...)
		local a = table.pack(...)
		local kinds = {}
		for i = 1, a.n do
			local v = a[i]
			kinds[i] = typeof(v)
			if type(v) == "buffer" then
				realWrite(outp .. ".chunk" .. n .. ".arg" .. i .. ".bin", buffer.tostring(v))
				kinds[i] = "buffer:" .. buffer.len(v)
			elseif type(v) == "string" then
				realWrite(outp .. ".chunk" .. n .. ".arg" .. i .. ".bin", v)
				kinds[i] = "string:" .. #v
			end
		end
		info[n].args = table.concat(kinds, ",")
		error(DONE, 0)
	end
end
setmetatable(env, { __index = function(_, k)
	error("__LPH_UNWRAP_GLOBAL__ " .. tostring(k), 0)
end })
local f, err = realLoad(src, "=sample")
if not f then
	realEwrite("[unwrap] the file does not compile: " .. tostring(err) .. "\n")
	return
end
setfenv(f, env)
local ok, e = pcall(f)
local lines = {}
for n, c in ipairs(info) do
	lines[n] = string.format("%d\t%s\t%d\t%s", n, c.name, c.size, c.args)
end
realWrite(outp .. ".info", table.concat(lines, "\n") .. (#lines > 0 and "\n" or ""))
if e == DONE then
	realEwrite("[unwrap] stopped at the first call of a loaded chunk (" .. captured .. " loadstring call(s))\n")
elseif type(e) == "string" and string.find(e, "__LPH_UNWRAP_GLOBAL__", 1, true) then
	realEwrite("[unwrap] the file reads the global '" .. tostring(string.match(e, "__LPH_UNWRAP_GLOBAL__ (.*)$")) ..
		"' before it loads a chunk: not a plain container\n")
else
	realEwrite("[unwrap] the file ended without calling a loaded chunk: " .. tostring(ok) .. " " .. tostring(e) .. "\n")
end
