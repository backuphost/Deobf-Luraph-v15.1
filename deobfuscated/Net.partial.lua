-- Net (ModuleScript ReplicatedStorage.Packages.Net) -- PARTIAL recovery from a Luraph v15.1 build.
--
-- What this is: the script's own code lifted out of the Luraph VM after the bootstrap's
-- environment checks passed and the payload was decrypted with the right key.
--
-- What is solid: the overall structure and every string / number that appears, e.g. the
-- client/server split on RunService, the "EventServiceLTM" workspace attribute guard, the
-- GetFullName() check, the SHA-256 routine (v8 round constants, v11 padding, v14 compression,
-- v15 hex digest), the remote-name scrambling keyed on game.JobId (v23 / v24), and the
-- integrity checks in v27 (pcall / error / debug.info(getfenv) / debug.getmemorycategory / tick).
--
-- What is NOT recovered (do not treat this file as runnable source):
--   * `getfenv()[nil][nil]`, `nil` operands and "LIFT ERROR" lines are constants and instructions
--     of code paths that did not execute during analysis; Luraph decrypts them only as they run.
--   * "for-loop without visible setup" / "for-loop setup" comments mark numeric for loops whose
--     bounds the lifter did not reattach (this interpreter encodes them differently).
--   * "tamper response (never taken ...)" marks Luraph's own key checks, not script logic.
--   * the module body stops early: the run ended inside the first remote-name computation, so
--     the rest of the module (the returned RemoteEvent / RemoteFunction API) is missing.
--   * `table.move(table.pack(f(...)), 1, n, r, ({}))` is the VM's way of writing "call f and keep
--     n results"; `local v = table.pack(x) if v then return unpack(v, 1, v.n) end` is `return x`.

local v1 = table.pack(select(1, nil --[[multi_args]]))
local RunService = game:GetService("RunService")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
if RunService:IsClient() then
	if workspace:GetAttribute("EventServiceLTM") then
		local v28 = table.pack({
			RemoteFunction = function()

		end,
			RemoteEvent = function()

		end,
		})
		if v28 then
			return unpack(v28, 1, v28.n)
		end
		return
	end
	table.move(table.pack(workspace:SetAttribute("EventServiceLTM", true)), 1, 0, 36, ({}))
	if script:GetFullName() ~= "ReplicatedStorage.Packages.Net" then
		-- LIFT ERROR: call of None
		return
	end
else
	getfenv()[nil] = ReplicatedStorage[nil][nil]
end
local v4 = string.rep("%08x", 8)
local v5 = tick()
local v6 = buffer.create(256)
local v7 = ipairs
local v8 = {
	1116352408,
	1899447441,
	3049323471,
	3921009573,
	961987163,
	1508970993,
	2453635748,
	2870763221,
	3624381080,
	310598401,
	607225278,
	1426881987,
	1925078388,
	2162078206,
	2614888103,
	3248222580,
	3835390401,
	4022224774,
	264347078,
	604807628,
	770255983,
	1249150122,
	1555081692,
	1996064986,
	2554220882,
	2821834349,
	2952996808,
	3210313671,
	3336571891,
	3584528711,
	113926993,
	338241895,
	666307205,
	773529912,
	1294757372,
	1396182291,
	1695183700,
	1986661051,
	2177026350,
	2456956037,
	2730485921,
	2820302411,
	3259730800,
	3345764771,
	3516065817,
	3600352804,
	4094571909,
	275423344,
	430227734,
	506948616,
	659060556,
	883997877,
	958139571,
	1322822218,
	1537002063,
	1747873779,
	1955562222,
	2024104815,
	2227730452,
	2361852424,
	2428436474,
	2756734187,
	3204031479,
	3329325298,
}
table.move(table.pack(v7(v8)), 1, 3, 39, ({}))
for key2, value2 in v7, v8, 1116352408 do
	table.move(table.pack(buffer.writeu32(v6, (key2 - 1) * 4, value2)), 1, 0, 8, ({}))
	-- for-loop without visible setup
	for key2, value2 in nil --[[?]] do
	end
	break
end
local v11 = function(p1)
	local v29 = buffer.len(p1)
	local v30 = -(v29 + 9) % 64
	local v31 = v29 + 1 + v30 + 8
	local v32 = getfenv()[nil][nil](v31)
	table.move(table.pack(getfenv()[nil][nil](v32, 0, p1)), 1, 0, 14, ({}))
	table.move(table.pack(getfenv()[nil][nil](v32, v29, 128)), 1, 0, 14, ({}))
	local v33 = v29 * 8
	for i2 = nil, nil, v30 do
		local v36 = v33 % 256
		table.move(table.pack(getfenv()[nil][nil](v32, i2 + v29 + 1 + v30, v36)), 1, 0, 14, ({}))
		v33 = (v33 - v36) / 256
		-- for-loop without visible setup
		for i2 = nil --[[?]] do
		end
		break
	end
	local v37 = table.pack(v32, v31)
	if v37 then
		return unpack(v37, 1, v37.n)
	end
end
local v12 = buffer.create(256)
local v13 = 156595870
local v14 = function(p2, p3)
	if v13 == 211083613 then
		-- tamper response (never taken in the traced run)
		return
	end
	if v13 == 507832368 then
		-- tamper response (never taken in the traced run)
		return
	end
	local v38 = p3 - 1
	if bit32.bxor(v13, 39800618) == 504094197 then
		-- tamper response (never taken in the traced run)
		return
	end
	for i3 = v38, 64, nil do
		-- LIFT ERROR: evaluator failure: ValueError: min() iterable argument is empty
		return
	end
	return 1779033703, 3144134277, 1013904242, 2773480762, 1359893119, 2600822924, 528734635, 1541459225
end
v13 = bit32.bxor(v13, 83906104)
local v15 = function(p4, p5)
	if p5 and 0 < buffer.len(p5) then
		local v41 = getfenv()[nil][nil](getfenv()[nil][nil](p4) + getfenv()[nil][nil](p5))
		table.move(table.pack(getfenv()[nil][nil](v41, 0, p4)), 1, 0, 12, ({}))
		table.move(table.pack(getfenv()[nil][nil](v41, getfenv()[nil][nil](p4), p5)), 1, 0, 12, ({}))
		p4 = v41
	end
	local v42 = v11
	local v43 = p4
	table.move(table.pack(v42(v43)), 1, 2, 14, ({}))
	local v44 = v14
	local v45 = v42
	local v46 = v43
	table.move(table.pack(v44(v45, v46)), 1, 8, 13, ({}))
	return getfenv()[nil][nil](v4, v44, v45, v46, nil, nil, nil, p4, p5)
end
local v16 = {}
local isServer = RunService:IsServer()
local v18
if RunService:IsStudio() then
	v18 = nil
else
	v18 = game.JobId
end
local v19 = v18
local v21 = nil
local v22 = 74383903
local v23 = function(p6, p7)
	local v50 = (p6 - 32 + p7) % 95
	if bit32.bxor(v22, 210676092) ~= 114950760 then
		-- tamper response (never taken in the traced run)
		return
	end
	local v51 = table.pack(v50 + 32)
	if v51 then
		return unpack(v51, 1, v51.n)
	end
end
v22 = 173320980
local v24 = function(p8)
	local pack = table.pack
	local v53 = table.pack(string.byte(v19, 1, -1))
	local v54 = pack(unpack(v53, 1, v53.n))
	local v55 = ""
	local v56 = 1
	for i5 = #p8, 1, -1 do
		local v58 = getfenv()[nil][nil]
		local v59 = getfenv()[nil][nil]
		table.move(table.pack(v59(p8, i5)), 1, 1, 21, ({}))
		local v60 = table.pack(v23(v59, v54[v56]))
		v55 ..= v58(unpack(v60, 1, v60.n))
		v56 = v56 % v54[nil] + 1
		-- for-loop without visible setup
		for i5 = nil --[[?]] do
		end
		break
	end
	local v61 = table.pack(v55)
	if v61 then
		return unpack(v61, 1, v61.n)
	end
end
if isServer then
	v16[nil] = v24
end
local v25 = pcall
local v26 = debug.info(getfenv, "slfna")
local v27 = function(p9, p10, p11)
	local v62 = 504912923
	if v25 ~= pcall then
		local v95 = getfenv()[nil]
		local v96 = v95[nil]
		table.move(table.pack(v96(v95, nil .. v21, 10)), 1, 1, 37, ({}))
		table.move(table.pack(v96[nil](v96, v24(nil) .. getfenv()[nil][nil](nil), 1)), 1, 0, 38, ({}))
		local v97 = table.pack(nil)
		if v97 then
			return unpack(v97, 1, v97.n)
		end
		return
	end
	local v63 = pcall
	local v64 = error
	table.move(table.pack(v63(v64, "bahah")), 1, 2, 2, ({}))
	if v63 or not v64 or v64 ~= "bahah" then
		local v92 = getfenv()[nil]
		local v93 = v92[nil]
		table.move(table.pack(v93(v92, nil .. v21, 10)), 1, 1, 46, ({}))
		table.move(table.pack(v93[nil](v93, v24(nil) .. getfenv()[nil][nil](nil), 1)), 1, 0, 47, ({}))
		local v94 = table.pack(nil)
		if v94 then
			return unpack(v94, 1, v94.n)
		end
		return
	end
	local info = debug.info
	if v62 == 26304884 then
		-- tamper response (never taken in the traced run)
		return
	end
	if info(getfenv, "slfna") ~= v26 then
		local v88 = getfenv()[nil]
		local v89 = v88[nil]
		table.move(table.pack(v89(v88, nil .. v21, 10)), 1, 1, 38, ({}))
		local v90 = bit32.bxor(v62, 3180603190)
		table.move(table.pack(v89[nil](v89, v24(nil) .. getfenv()[nil][nil](nil), 1)), 1, 0, 39, ({}))
		local v91 = table.pack(nil)
		if v91 then
			return unpack(v91, 1, v91.n)
		end
		return
	end
	if debug.getmemorycategory() == "" or debug.getmemorycategory() == "--" then
		local v66 = getfenv()[nil]
		local v67 = v66[nil]
		table.move(table.pack(v67(v66, nil .. v21, 10)), 1, 1, 45, ({}))
		table.move(table.pack(v67[nil](v67, v24(nil) .. getfenv()[nil][nil](nil), 1)), 1, 0, 46, ({}))
		local v68 = table.pack(nil)
		if v68 then
			return unpack(v68, 1, v68.n)
		end
		return
	end
	local v69 = tick() - v5
	local v77
	if v69 > 10 then
		if v69 == v69 then
			-- LIFT ERROR: evaluator failure: ValueError: min() iterable argument is empty
			return
		end
	else
		local v70 = v62
		if not nil then
		end
		local pack2 = table.pack
		local v72 = -1
		local v73 = table.pack(string.byte(p10, 1, -1))
		if v70 + 377516702 == 140351176 then
			local v74 = bit32.bxor(v70, nil)
		end
		local v75 = pack2(unpack(v73, 1, v73.n))
		local v76 = v75[nil]
		v77 = nil
		local v78 = 1
		-- for-loop setup: #p9, 1, v72
		local v79 = v70
		-- for-loop without visible setup
		for i8 = nil --[[?]] do
			while true do
				local v81 = getfenv()[nil][nil]
				table.move(table.pack(v81(p9, i8, i8)), 1, 1, 2, ({}))
				if v81 == nil then
					v77 ..= v81
					-- for-loop without visible setup
					local exit2
					for i8 = nil --[[?]] do
						exit2 = true
						break
					end
					if exit2 then
						continue
					end
					break
				end
				local v82 = getfenv()[nil][nil]
				local v83 = bit32.bxor(v79, 2232809704)
				local v84 = v82(v81)
				local v85 = getfenv()[nil][nil]
				local v86 = table.pack(v23(v84, v75[v78]))
				v77 ..= v85(unpack(v86, 1, v86.n))
				v78 = v78 % v76 + 1
				v79 = bit32.bxor(v83, nil)
				-- for-loop without visible setup
				local exit1
				for i8 = nil --[[?]] do
					exit1 = true
					break
				end
				if exit1 then
					continue
				end
				break
			end
			break
		end
	end
	local v87 = table.pack(v77)
	if v87 then
		return unpack(v87, 1, v87.n)
	end
end
getfenv().UGCClothesStorage = "2b857474-f9cc-4ddd-a4f1-28be8b6c1b38"
v21 = (function(p12)
	-- LIFT ERROR: evaluator failure: ValueError: min() iterable argument is empty
end)("InventoryService/Sort")
-- LIFT ERROR: call of None
